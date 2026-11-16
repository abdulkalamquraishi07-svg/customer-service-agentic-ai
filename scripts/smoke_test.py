"""End-to-end smoke test against a DEPLOYED environment (dev/staging only).

Creates two Cognito users (a customer and an approver), seeds demo orders, then drives the
real API: orders, shipment status, eligibility, a high-value return, ops approval, async refund
execution through SQS + worker, idempotency replay and cross-customer isolation.

    python scripts/smoke_test.py --env dev
Passwords are random per run and never printed.
"""

import argparse
import atexit
import contextlib
import json
import secrets
import string
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from pycognito.aws_srp import AWSSRP

sys.path.insert(0, "services/core/src")
from csagent_core.adapters.dynamodb import to_item  # noqa: E402
from csagent_core.models import Order, OrderStatus  # noqa: E402

sys.path.insert(0, "scripts")
from seed_data import build_orders  # noqa: E402

REGION = "us-east-1"
CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))


def random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Aa1!" + "".join(secrets.choice(alphabet) for _ in range(20))


def outputs(env: str) -> dict[str, str]:
    cf = boto3.client("cloudformation", region_name=REGION)
    out: dict[str, str] = {}
    for stack in (f"csagent-{env}-auth", f"csagent-{env}-api"):
        for o in cf.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", []):
            out[o["OutputKey"]] = o["OutputValue"]
    return out


def ensure_user(idp, pool_id: str, email: str, group: str) -> tuple[str, str]:
    """Create (or reuse) a user, set a fresh random password, return (sub, password)."""
    with contextlib.suppress(idp.exceptions.UsernameExistsException):
        idp.admin_create_user(
            UserPoolId=pool_id,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
    password = random_password()
    idp.admin_set_user_password(
        UserPoolId=pool_id, Username=email, Password=password, Permanent=True
    )
    idp.admin_add_user_to_group(UserPoolId=pool_id, Username=email, GroupName=group)
    user = idp.admin_get_user(UserPoolId=pool_id, Username=email)
    sub = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")
    return sub, password


def login(pool_id: str, client_id: str, email: str, password: str) -> str:
    srp = AWSSRP(
        username=email, password=password, pool_id=pool_id, client_id=client_id,
        pool_region=REGION,
    )
    return srp.authenticate_user()["AuthenticationResult"]["IdToken"]


def api(base: str, token: str | None, method: str, path: str, body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    if not base.startswith("https://"):
        raise ValueError("API URL must be https")
    req = urllib.request.Request(  # noqa: S310 - https enforced above
        base + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw.decode(errors="replace")[:200]}


def cleanup_customer(idp, ddb, env: str, pool_id: str, email: str, sub: str) -> None:
    """Remove a smoke customer and its mutable data (audit/ledger are append-only by design)."""
    keys = {"orders": "order_id", "returns": "return_id", "tickets": "ticket_id"}
    for table_name, sort_key in keys.items():
        table = ddb.Table(f"csagent-{env}-{table_name}")
        items = table.query(KeyConditionExpression=Key("customer_id").eq(sub))["Items"]
        for item in items:
            table.delete_item(Key={"customer_id": sub, sort_key: item[sort_key]})
    with contextlib.suppress(idp.exceptions.UserNotFoundException):
        idp.admin_delete_user(UserPoolId=pool_id, Username=email)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    args = parser.parse_args()
    env = args.env

    out = outputs(env)
    base = out["ApiUrl"]
    pool, client = out["UserPoolId"], out["WebClientId"]
    idp = boto3.client("cognito-idp", region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)

    run = uuid.uuid4().hex[:8]  # fresh customers per run: caps/guards never leak between runs
    cust_email = f"smoke.customer.{run}@example.com"
    appr_email = "smoke.approver@example.com"  # staff user is reusable
    other_email = f"smoke.other.{run}@example.com"
    cust_sub, cust_pw = ensure_user(idp, pool, cust_email, "customers")
    appr_sub, appr_pw = ensure_user(idp, pool, appr_email, "approvers")
    other_sub, other_pw = ensure_user(idp, pool, other_email, "customers")
    atexit.register(cleanup_customer, idp, ddb, env, pool, cust_email, cust_sub)
    atexit.register(cleanup_customer, idp, ddb, env, pool, other_email, other_sub)
    cust = login(pool, client, cust_email, cust_pw)
    appr = login(pool, client, appr_email, appr_pw)
    other = login(pool, client, other_email, other_pw)

    # --- seed: demo orders + one high-value delivered electronics order ---
    now = datetime.now(UTC)
    orders = ddb.Table(f"csagent-{env}-orders")
    for item in build_orders(cust_sub, now):
        orders.put_item(Item=item)
    big_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"  # fresh order per run => rerunnable
    big = Order(
        customer_id=cust_sub, order_id=big_id, status=OrderStatus.DELIVERED,
        ordered_at=now - timedelta(days=6), promised_delivery_at=now - timedelta(days=2),
        delivered_at=now - timedelta(days=1), total_amount=Decimal("74999"),
        category="electronics", carrier="BlueDart",
    )
    orders.put_item(Item=to_item(big))
    # Another customer's order that cust must never see.
    secret = big.model_copy(update={"customer_id": other_sub, "order_id": "ORD-OTHER"})
    orders.put_item(Item=to_item(secret))

    # --- auth ---
    check("unauthenticated request is rejected", api(base, None, "GET", "v1/orders")[0] == 401)
    status, who = api(base, cust, "GET", "v1/whoami")
    check("whoami returns the JWT subject", status == 200 and who.get("customer_id") == cust_sub)

    # --- orders ---
    status, body = api(base, cust, "GET", "v1/orders")
    ids = sorted(o["order_id"] for o in body.get("orders", []))
    check("customer lists only own orders", status == 200 and "ORD-OTHER" not in ids and
          "ORD-1001" in ids, str(ids))
    status, ship = api(base, cust, "GET", "v1/orders/ORD-1001/shipment")
    check("delayed laptop is flagged overdue by the backend",
          status == 200 and ship.get("is_overdue") is True, str(ship))
    check("another customer's order looks like 404",
          api(base, cust, "GET", "v1/orders/ORD-OTHER")[0] == 404)

    # --- validation ---
    check("unknown body field (customer_id injection) rejected at the edge",
          api(base, cust, "POST", "v1/returns",
              {"order_id": big_id, "reason": "x", "customer_id": other_sub},
              key=str(uuid.uuid4()))[0] == 400)
    check("missing Idempotency-Key rejected",
          api(base, cust, "POST", "v1/returns", {"order_id": big_id, "reason": "x"})[0] == 400)

    # --- high-value return: approval -> async refund ---
    key = str(uuid.uuid4())
    payload = {"order_id": big_id, "reason": "Screen flickers"}
    status, ret = api(base, cust, "POST", "v1/returns", payload, key=key)
    rid = ret.get("return_id", "")
    check("high-value return waits for human approval",
          status == 201 and ret.get("status") == "PENDING_APPROVAL", str(ret))
    status, replay = api(base, cust, "POST", "v1/returns", payload, key=key)
    check("same Idempotency-Key replays the same return",
          status == 201 and replay.get("return_id") == rid)
    status, dup = api(base, cust, "POST", "v1/returns", payload, key=str(uuid.uuid4()))
    check("second return for the same order is blocked", status == 422, str(dup))

    decision_path = f"v1/ops/returns/{cust_sub}/{rid}/decision"
    check("customer cannot approve",
          api(base, cust, "POST", decision_path, {"approve": True})[0] == 403)
    status, listing = api(base, appr, "GET", "v1/ops/approvals")
    check("approver sees the pending return",
          status == 200 and rid in [a["return_id"] for a in listing.get("approvals", [])])
    status, approved = api(base, appr, "POST", decision_path, {"approve": True, "note": "ok"})
    check("approver approves", status == 200 and approved.get("status") == "APPROVED",
          str(approved))

    final = {}
    for _ in range(30):  # SQS -> worker is asynchronous
        _, final = api(base, cust, "GET", f"v1/returns/{rid}")
        if final.get("status") == "EXECUTED":
            break
        time.sleep(2)
    check("worker executed the refund via SQS", final.get("status") == "EXECUTED", str(final))
    ledger = ddb.Table(f"csagent-{env}-ledger").scan()["Items"]
    check("exactly one payout recorded for the return",
          len([i for i in ledger if i["idempotency_key"] == rid]) == 1)

    # --- tickets ---
    status, ticket = api(base, cust, "POST", "v1/tickets",
                         {"category": "delivery", "subject": "Laptop late",
                          "description": "ORD-1001 not delivered", "related_order_id": "ORD-1001"},
                         key=str(uuid.uuid4()))
    check("ticket created", status == 201, str(ticket))
    status, esc = api(base, cust, "POST", f"v1/tickets/{ticket.get('ticket_id')}/escalate",
                      {"summary": "Customer wants a human; shipment overdue"})
    check("ticket escalated", status == 200 and esc.get("status") == "ESCALATED", str(esc))
    check("other customer cannot touch the ticket",
          api(base, other, "POST", f"v1/tickets/{ticket.get('ticket_id')}/escalate",
              {"summary": "x"})[0] == 404)

    failed = [n for n, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
