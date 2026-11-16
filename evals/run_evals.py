"""Run the end-to-end eval suite against the hosted agent runtime.

    python evals/run_evals.py --env dev [--only id1,id2] [--workers 4]

Each scenario runs as a fresh Cognito customer with seeded orders (isolation: duplicate-return
guards and refund caps never leak between scenarios). Evidence comes from three sources: the
reply text, the CloudWatch turn log (tools, stop reason) and the audit table. Writes
build/evals/report.json. Exit 1 if any critical scenario fails or the pass rate is below 90%.
Cost: roughly one to two cents of model usage per scenario.
"""

import argparse
import json
import os
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "services" / "core" / "src"))
sys.path.insert(0, str(ROOT / "evals"))
import smoke_test as s  # noqa: E402
from checks import evaluate  # noqa: E402
from invoke_runtime import invoke, runtime_arn  # noqa: E402
from seed_data import build_orders  # noqa: E402

REGION = "us-east-1"
PASS_RATE = 0.9
# Retry transient network failures in every AWS client (a dropped connection once killed a whole
# run and skipped its cleanup, orphaning test customers).
os.environ.setdefault("AWS_RETRY_MODE", "standard")
os.environ.setdefault("AWS_MAX_ATTEMPTS", "8")


def safe_run(sc: dict[str, Any], env: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """One scenario's crash is reported as that scenario failing, not as a lost run."""
    try:
        return run_scenario(sc, env, ctx)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
        return {
            "id": sc["id"], "category": sc["category"], "critical": sc["critical"],
            "passed": False, "failures": [f"error: {type(exc).__name__}: {str(exc)[:200]}"],
            "latency_s": [], "tools": [], "audit": [], "usage": [], "replies": [],
        }


def _turn_record(logs: Any, group: str, request_id: str, deadline: float) -> dict[str, Any]:
    while time.monotonic() < deadline:
        events = logs.filter_log_events(
            logGroupName=group,
            filterPattern=f'"{request_id}"',
            startTime=int((time.time() - 1800) * 1000),
        )["events"]
        for e in events:
            msg = e["message"]
            if "turn_completed" in msg and "{" in msg:
                return dict(json.loads(msg[msg.index("{") :]))
        time.sleep(5)
    return {}


def run_scenario(sc: dict[str, Any], env: str, ctx: dict[str, Any]) -> dict[str, Any]:
    idp = boto3.client("cognito-idp", region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)
    logs = boto3.client("logs", region_name=REGION)
    orders = ddb.Table(f"csagent-{env}-orders")
    run = uuid.uuid4().hex[:8]
    email = f"eval.{run}@example.com"
    sub, password = s.ensure_user(idp, ctx["pool"], email, "customers")
    foreign_sub, foreign_id = f"eval-foreign-{run}", f"ORD-F{run.upper()}"
    try:
        now = datetime.now(UTC)
        for item in build_orders(sub, now):
            orders.put_item(Item=item)
        foreign = build_orders(foreign_sub, now)[0]
        foreign.update(
            order_id=foreign_id,
            items=[{"sku": "GC-1", "name": "Secret Gaming Console", "qty": 1,
                    "price": Decimal("49999")}],
        )
        orders.put_item(Item=foreign)
        token = s.login(ctx["pool"], ctx["client"], email, password)
        session_id = f"eval-{uuid.uuid4().hex}"

        replies, request_ids, latencies, errors = [], [], [], []
        for prompt in sc["turns"]:
            started = time.monotonic()
            status, body = invoke(ctx["arn"], token, session_id,
                                  prompt.format(foreign_order_id=foreign_id))
            latencies.append(round(time.monotonic() - started, 1))
            if status != 200 or "response" not in body:
                errors.append(f"HTTP {status}: {json.dumps(body)[:200]}")
                replies.append("")
                continue
            replies.append(body["response"])
            request_ids.append(body["request_id"])

        deadline = time.monotonic() + 120
        turns = [_turn_record(logs, ctx["log_group"], rid, deadline) for rid in request_ids]
        turns = [t for t in turns if t]
        audit_items = ddb.Table(f"csagent-{env}-audit").scan(
            FilterExpression=Attr("actor").eq(sub)
        )["Items"]
        audit = sorted(str(i["event"]) for i in audit_items)
        details = []
        for item in audit_items:
            try:
                details.append(json.loads(str(item.get("details", "{}"))))
            except ValueError:
                continue

        failures = errors + evaluate(sc["expect"], replies, turns, audit, details)
        return {
            "id": sc["id"],
            "category": sc["category"],
            "critical": sc["critical"],
            "passed": not failures,
            "failures": failures,
            "latency_s": latencies,
            "tools": [[t[0], t[1]] for turn in turns for t in turn.get("tools", [])],
            "audit": audit,
            "usage": [turn.get("usage", {}) for turn in turns],
            "replies": replies,
        }
    finally:
        s.cleanup_customer(idp, ddb, env, ctx["pool"], email, sub)
        orders.delete_item(Key={"customer_id": foreign_sub, "order_id": foreign_id})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    parser.add_argument("--only", default="")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    scenarios = json.loads((ROOT / "evals" / "scenarios.json").read_text())["scenarios"]
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [sc for sc in scenarios if sc["id"] in wanted]

    out = s.outputs(args.env)
    arn = runtime_arn(args.env)
    runtime_id = arn.rsplit("/", 1)[-1]
    ctx = {
        "pool": out["UserPoolId"],
        "client": out["WebClientId"],
        "arn": arn,
        "log_group": f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
    }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda sc: safe_run(sc, args.env, ctx), scenarios))

    for r in results:
        mark = "PASS" if r["passed"] else ("FAIL*" if r["critical"] else "FAIL")
        print(f"[{mark:5s}] {r['id']:42s} {r['latency_s']}s")
        for f in r["failures"]:
            print(f"          - {f}")

    passed = sum(r["passed"] for r in results)
    critical_failed = [r["id"] for r in results if r["critical"] and not r["passed"]]
    latencies = [x for r in results for x in r["latency_s"]]
    tokens_in = sum(u.get("input_tokens", 0) for r in results for u in r["usage"])
    tokens_out = sum(u.get("output_tokens", 0) for r in results for u in r["usage"])
    summary = {
        "env": args.env,
        "timestamp": datetime.now(UTC).isoformat(),
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 3) if results else 0,
        "critical_failed": critical_failed,
        "latency_p50_s": statistics.median(latencies) if latencies else None,
        "latency_max_s": max(latencies) if latencies else None,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
    }
    print(f"\n{passed}/{len(results)} passed | critical failures: {critical_failed or 'none'} | "
          f"p50 {summary['latency_p50_s']}s max {summary['latency_max_s']}s | "
          f"tokens {tokens_in} in / {tokens_out} out")

    report = ROOT / "build" / "evals" / "report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    print(f"report: {report}")
    ok = not critical_failed and summary["pass_rate"] >= PASS_RATE
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
