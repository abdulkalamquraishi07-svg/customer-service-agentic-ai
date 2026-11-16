import json

import pytest
from bizapi import customer_api, ops_api, refund_worker, scheduler
from bizapi.common import parse_groups
from conftest import make_event
from csagent_core.context import CallerContext
from csagent_core.errors import DependencyFailure
from csagent_core.models import OrderStatus

KEY = "idem-key-0001"
APPROVER = {"sub": "appr-1", "groups": "approvers"}


def call(module, event):
    resp = module.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"]), resp


def ret(world, **kw):
    return call(
        customer_api,
        make_event("POST", "/v1/returns", body={"order_id": "ORD-1", "reason": "Defective"},
                   key=kw.pop("key", KEY), **kw),
    )


# ---------- auth & routing ----------


def test_unauthenticated_request_is_rejected(world):
    status, body, _ = call(customer_api, make_event("GET", "/v1/orders", sub=None))
    assert status == 403 and body["error"]["code"] == "forbidden"


def test_unknown_route_is_404(world):
    assert call(customer_api, make_event("GET", "/v1/nope"))[0] == 404


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("customers", {"customers"}), ("[customers approvers]", {"customers", "approvers"}),
     ("a,b", {"a", "b"}), ("", set()), (None, set())],
)
def test_group_claim_formats(raw, expected):
    assert parse_groups(raw) == expected


def test_security_headers_and_json_content_type(world):
    _, _, resp = call(customer_api, make_event("GET", "/v1/orders"))
    h = resp["headers"]
    assert h["Content-Type"] == "application/json" and h["Cache-Control"] == "no-store"
    assert "Strict-Transport-Security" in h and h["X-Content-Type-Options"] == "nosniff"


def test_unexpected_errors_do_not_leak_internals(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret table name xyz")

    monkeypatch.setattr(world.container.orders, "list_orders", boom)
    status, body, _ = call(customer_api, make_event("GET", "/v1/orders"))
    assert status == 500 and "xyz" not in json.dumps(body)


# ---------- orders ----------


def test_orders_are_scoped_to_caller_and_hide_internal_fields(world):
    world.add_order("cust-1", "ORD-1")
    world.add_order("cust-2", "ORD-2")
    _, body, _ = call(customer_api, make_event("GET", "/v1/orders"))
    assert [o["order_id"] for o in body["orders"]] == ["ORD-1"]

    status, order, _ = call(
        customer_api, make_event("GET", "/v1/orders/{order_id}", path={"order_id": "ORD-1"})
    )
    assert status == 200 and "customer_id" not in order and "version" not in order
    assert order["total_amount"] == "2499"  # money serialized as string

    status, _, _ = call(
        customer_api, make_event("GET", "/v1/orders/{order_id}", path={"order_id": "ORD-2"})
    )
    assert status == 404


def test_shipment_endpoint_reports_overdue(world):
    world.add_order(status=OrderStatus.SHIPPED, ordered_days_ago=5, promised_days=3)
    _, body, _ = call(
        customer_api,
        make_event("GET", "/v1/orders/{order_id}/shipment", path={"order_id": "ORD-1"}),
    )
    assert body["is_overdue"] is True and body["days_overdue"] == 2


def test_eligibility_endpoint(world):
    world.add_order()
    status, body, _ = call(
        customer_api,
        make_event("GET", "/v1/orders/{order_id}/return-eligibility", path={"order_id": "ORD-1"}),
    )
    assert status == 200 and body["eligible"] is True and body["refund_amount"] == "2499"


# ---------- returns ----------


def test_return_requires_idempotency_key_and_rejects_unknown_fields(world):
    world.add_order()
    ev = make_event("POST", "/v1/returns", body={"order_id": "ORD-1", "reason": "x"})
    assert call(customer_api, ev)[0] == 400
    injected = make_event(
        "POST", "/v1/returns",
        body={"order_id": "ORD-1", "reason": "x", "customer_id": "cust-2"}, key=KEY,
    )
    status, body, _ = call(customer_api, injected)
    assert status == 400 and "customer_id" in body["error"]["message"]


def test_malformed_json_body_is_400(world):
    ev = make_event("POST", "/v1/returns", key=KEY)
    ev["body"] = "{not json"
    assert call(customer_api, ev)[0] == 400


def test_low_value_return_is_approved_and_enqueued(world):
    world.add_order()
    status, body, _ = ret(world)
    assert status == 201 and body["status"] == "APPROVED"
    assert world.queue.messages == [{"customer_id": "cust-1", "return_id": body["return_id"]}]


def test_replay_with_same_key_returns_same_return(world):
    world.add_order()
    first = ret(world)[1]
    second = ret(world)[1]
    assert first["return_id"] == second["return_id"]


def test_enqueue_failure_does_not_fail_the_request(world):
    world.add_order()
    world.queue.fail = True
    status, body, _ = ret(world)
    assert status == 201 and body["status"] == "APPROVED"


def test_ineligible_return_is_422(world):
    world.add_order(category="electronics", delivered_days_ago=40)
    status, body, _ = ret(world)
    assert status == 422 and "RETURN_WINDOW_EXPIRED" in body["error"]["message"]


def test_cannot_return_foreign_order(world):
    world.add_order("cust-2", "ORD-1")
    assert ret(world)[0] == 404


# ---------- full high-value refund flow ----------


def test_high_value_refund_end_to_end(world):
    world.add_order(amount="74999", category="electronics")
    _, created, _ = ret(world)
    rid = created["return_id"]
    assert created["status"] == "PENDING_APPROVAL" and world.queue.messages == []

    decision = {"customer_id": "cust-1", "return_id": rid}
    route = "/v1/ops/returns/{customer_id}/{return_id}/decision"

    # a customer cannot approve
    denied = make_event("POST", route, path=decision, body={"approve": True})
    assert call(ops_api, denied)[0] == 403

    # approver sees it in the queue
    _, listing, _ = call(ops_api, make_event("GET", "/v1/ops/approvals", **APPROVER))
    assert [a["return_id"] for a in listing["approvals"]] == [rid]

    # approver approves -> execution enqueued
    status, approved, _ = call(
        ops_api, make_event("POST", route, path=decision, body={"approve": True}, **APPROVER)
    )
    assert status == 200 and approved["status"] == "APPROVED"
    assert world.queue.messages == [{"customer_id": "cust-1", "return_id": rid}]

    # worker executes exactly once, even if the message is delivered twice
    world.queue.messages.append(world.queue.messages[0])
    result = refund_worker.handler(world.queue.sqs_event(), None)
    assert result == {"batchItemFailures": []}
    ledger = world.tables["ledger"].scan()["Items"]
    assert len(ledger) == 1 and ledger[0]["idempotency_key"] == rid

    _, final, _ = call(
        customer_api, make_event("GET", "/v1/returns/{return_id}", path={"return_id": rid})
    )
    assert final["status"] == "EXECUTED" and final["refund_reference"] == f"SIM-{rid}"


def test_approver_cannot_decide_own_request(world):
    world.add_order("appr-1", "ORD-1", amount="74999", category="electronics")
    ev = make_event(
        "POST", "/v1/returns", sub="appr-1", groups="approvers",
        body={"order_id": "ORD-1", "reason": "mine"}, key=KEY,
    )
    rid = call(customer_api, ev)[1]["return_id"]
    own = make_event(
        "POST", "/v1/ops/returns/{customer_id}/{return_id}/decision",
        path={"customer_id": "appr-1", "return_id": rid}, body={"approve": True}, **APPROVER,
    )
    assert call(ops_api, own)[0] == 403


def test_rejection_does_not_enqueue(world):
    world.add_order(amount="74999", category="electronics")
    rid = ret(world)[1]["return_id"]
    ev = make_event(
        "POST", "/v1/ops/returns/{customer_id}/{return_id}/decision",
        path={"customer_id": "cust-1", "return_id": rid},
        body={"approve": False, "note": "fraud suspicion"}, **APPROVER,
    )
    status, body, _ = call(ops_api, ev)
    assert status == 200 and body["status"] == "REJECTED" and world.queue.messages == []


# ---------- worker ----------


class FailingPayments:
    def refund(self, *a, **k):
        raise DependencyFailure("down")


def test_worker_reports_only_failed_messages_for_retry(world):
    world.add_order()
    rid = ret(world)[1]["return_id"]
    world.install(FailingPayments())
    event = world.queue.sqs_event()
    event["Records"].append({"messageId": "bad", "body": "not json"})
    stale = json.dumps({"customer_id": "cust-1", "return_id": "RET-NOPE"})
    event["Records"].append({"messageId": "stale", "body": stale})
    result = refund_worker.handler(event, None)
    failed = {f["itemIdentifier"] for f in result["batchItemFailures"]}
    assert failed == {"m0", "bad"}  # gateway failure retried; malformed -> DLQ; stale acked

    world.install(world.payments)  # gateway recovers; redelivery succeeds exactly once
    assert refund_worker.handler(world.queue.sqs_event(), None) == {"batchItemFailures": []}
    assert len(world.tables["ledger"].scan()["Items"]) == 1
    record = world.container.returns.get_return(CallerContext("cust-1"), rid)
    assert record.status.value == "EXECUTED"


# ---------- scheduler ----------


def test_scheduler_expires_stale_approvals(world):
    world.add_order(amount="74999", category="electronics")
    rid = ret(world)[1]["return_id"]
    world.clock.advance(hours=49)
    assert scheduler.handler({}, None)["expired"] == 1
    _, body, _ = call(
        customer_api, make_event("GET", "/v1/returns/{return_id}", path={"return_id": rid})
    )
    assert body["status"] == "EXPIRED"


def test_scheduler_requeues_lost_executions(world):
    world.add_order()
    world.queue.fail = True
    ret(world)  # approved, but the enqueue was lost
    world.queue.fail = False

    assert scheduler.handler({}, None)["requeued"] == 0  # too fresh
    world.clock.advance(minutes=11)
    assert scheduler.handler({}, None)["requeued"] == 1
    refund_worker.handler(world.queue.sqs_event(), None)
    assert len(world.tables["ledger"].scan()["Items"]) == 1
    world.clock.advance(minutes=30)
    assert scheduler.handler({}, None)["requeued"] == 0  # executed; nothing left


# ---------- tickets ----------


def test_ticket_create_and_escalate(world):
    world.add_order()
    status, ticket, _ = call(
        customer_api,
        make_event(
            "POST", "/v1/tickets", key=KEY,
            body={"category": "delivery", "subject": "Late", "description": "Not arrived",
                  "related_order_id": "ORD-1"},
        ),
    )
    assert status == 201 and ticket["status"] == "OPEN"
    status, esc, _ = call(
        customer_api,
        make_event("POST", "/v1/tickets/{ticket_id}/escalate",
                   path={"ticket_id": ticket["ticket_id"]}, body={"summary": "Wants a human"}),
    )
    assert status == 200 and esc["status"] == "ESCALATED"


def test_ticket_validation_and_isolation(world):
    bad = make_event("POST", "/v1/tickets", key=KEY,
                     body={"category": "bogus", "subject": "s", "description": "d"})
    assert call(customer_api, bad)[0] == 400
    ok = make_event("POST", "/v1/tickets", key=KEY,
                    body={"category": "other", "subject": "s", "description": "d"})
    tid = call(customer_api, ok)[1]["ticket_id"]
    other = make_event("POST", "/v1/tickets/{ticket_id}/escalate", sub="cust-2",
                       path={"ticket_id": tid}, body={"summary": "x"})
    assert call(customer_api, other)[0] == 404


def test_ops_can_resolve_ticket_but_customers_cannot(world):
    ok = make_event("POST", "/v1/tickets", key=KEY,
                    body={"category": "other", "subject": "s", "description": "d"})
    tid = call(customer_api, ok)[1]["ticket_id"]
    route = "/v1/ops/tickets/{customer_id}/{ticket_id}/resolve"
    p = {"customer_id": "cust-1", "ticket_id": tid}
    assert call(ops_api, make_event("POST", route, path=p))[0] == 403
    agent = make_event("POST", route, path=p, sub="agent-1", groups="support-agents")
    assert call(ops_api, agent)[1]["status"] == "RESOLVED"



# ---------- malformed paths must be client errors, never 500s ----------


@pytest.mark.parametrize(
    ("module", "method", "resource"),
    [
        (customer_api, "GET", "/v1/orders/{order_id}"),
        (customer_api, "GET", "/v1/orders/{order_id}/shipment"),
        (customer_api, "GET", "/v1/returns/{return_id}"),
        (ops_api, "POST", "/v1/ops/returns/{customer_id}/{return_id}/decision"),
        (ops_api, "POST", "/v1/ops/tickets/{customer_id}/{ticket_id}/resolve"),
    ],
)
@pytest.mark.parametrize("path", [None, {}, {"order_id": ""}, {"return_id": "x" * 500}])
def test_missing_or_bad_path_params_are_400_not_500(world, module, method, resource, path):
    event = make_event(method, resource, **APPROVER, body={"approve": True})
    event["pathParameters"] = path
    status, body, _ = call(module, event)
    assert status == 400 and body["error"]["code"] == "validation_failed"
