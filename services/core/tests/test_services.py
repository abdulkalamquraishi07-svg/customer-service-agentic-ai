"""Service-level tests. Each runs against both the in-memory and DynamoDB (moto) backends."""

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import put_order
from csagent_core.adapters.memory import InMemoryAudit
from csagent_core.context import APPROVERS, CUSTOMERS, SUPPORT_AGENTS, SYSTEM, CallerContext
from csagent_core.errors import (
    ConcurrentModification,
    Conflict,
    DependencyFailure,
    Forbidden,
    IdempotencyConflict,
    InvalidTransition,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from csagent_core.models import OrderStatus, ReturnStatus, TicketStatus
from csagent_core.order_service import OrderService
from csagent_core.policy import ReturnPolicy
from csagent_core.return_service import ReturnService
from csagent_core.ticket_service import TicketService
from support import make_order

CUST = CallerContext("cust-1", frozenset({CUSTOMERS}), "req-1")
OTHER = CallerContext("cust-2", frozenset({CUSTOMERS}), "req-2")
APPROVER = CallerContext("appr-1", frozenset({APPROVERS}), "req-3")
SYSTEM_CTX = CallerContext("workflow", frozenset({SYSTEM}), "req-4")
AGENT = CallerContext("agent-1", frozenset({SUPPORT_AGENTS}), "req-5")

KEY1 = "idem-key-0001"
KEY2 = "idem-key-0002"


def audit_events(stores, entity_id):
    audit = stores["audit"]
    if isinstance(audit, InMemoryAudit):
        return [e["event"] for e in audit.events if e["entity_id"] == entity_id]
    return [i["event"] for i in audit.list_for_entity(entity_id)]


@pytest.fixture
def order_svc(stores, clock):
    return OrderService(stores["orders"], clock)


@pytest.fixture
def returns_svc(stores, clock, payments):
    return ReturnService(
        stores["orders"],
        stores["returns"],
        stores["idempotency"],
        stores["audit"],
        payments,
        clock,
    )


@pytest.fixture
def tickets_svc(stores, clock):
    return TicketService(
        stores["tickets"], stores["orders"], stores["idempotency"], stores["audit"], clock
    )


# ---------------- orders ----------------


def test_customer_sees_only_own_orders(stores, order_svc):
    put_order(stores, make_order("cust-1", "ORD-1"))
    put_order(stores, make_order("cust-2", "ORD-2"))
    assert [o.order_id for o in order_svc.list_orders(CUST)] == ["ORD-1"]
    assert order_svc.get_order(CUST, "ORD-1").order_id == "ORD-1"


def test_foreign_order_looks_like_missing_order(stores, order_svc):
    put_order(stores, make_order("cust-2", "ORD-2"))
    with pytest.raises(NotFound):
        order_svc.get_order(CUST, "ORD-2")
    with pytest.raises(NotFound):
        order_svc.get_order(CUST, "ORD-DOES-NOT-EXIST")


def test_overdue_shipment_is_computed_from_backend_dates(stores, order_svc):
    put_order(
        stores,
        make_order(
            status=OrderStatus.SHIPPED,
            delivered_days_ago=None,
            ordered_days_ago=5,
            promised_days_after_order=3,
        ),
    )
    view = order_svc.get_shipment_status(CUST, "ORD-1")
    assert view.is_overdue and view.days_overdue == 2


def test_order_list_includes_what_was_bought(stores, order_svc):
    from decimal import Decimal as D

    from csagent_core.models import OrderItem

    order = make_order(category="electronics").model_copy(
        update={"items": [OrderItem(sku="LAP", name="ProBook 15 Laptop", qty=1, price=D("1"))]}
    )
    put_order(stores, order)
    view = order_svc.list_orders(CUST)[0]
    assert view.category == "electronics" and view.item_names == ["ProBook 15 Laptop"]


def test_delivered_order_is_never_overdue(stores, order_svc):
    put_order(stores, make_order(ordered_days_ago=20, promised_days_after_order=3))
    assert not order_svc.get_shipment_status(CUST, "ORD-1").is_overdue


# ---------------- return requests ----------------


def test_low_value_return_is_auto_approved(stores, returns_svc):
    put_order(stores, make_order(amount="2499"))
    rec = returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    assert rec.status == ReturnStatus.APPROVED
    assert not rec.requires_approval and rec.approval_expires_at is None
    assert audit_events(stores, rec.return_id) == ["return.auto_approved"]


def test_high_value_return_needs_approval_with_expiry(stores, returns_svc, clock):
    put_order(stores, make_order(amount="74999", category="electronics"))
    rec = returns_svc.request_return(CUST, "ORD-1", "Not as described", KEY1)
    assert rec.status == ReturnStatus.PENDING_APPROVAL
    assert rec.approval_expires_at == clock() + timedelta(hours=48)


def test_ineligible_return_is_rejected_with_reason(stores, returns_svc):
    put_order(stores, make_order(category="electronics", delivered_days_ago=40))
    with pytest.raises(PolicyViolation, match="RETURN_WINDOW_EXPIRED"):
        returns_svc.request_return(CUST, "ORD-1", "Changed mind", KEY1)


def test_cannot_return_another_customers_order(stores, returns_svc):
    put_order(stores, make_order("cust-2", "ORD-2"))
    with pytest.raises(NotFound):
        returns_svc.request_return(CUST, "ORD-2", "Not mine", KEY1)


def test_input_validation(stores, returns_svc):
    put_order(stores, make_order())
    with pytest.raises(ValidationFailed):
        returns_svc.request_return(CUST, "ORD-1", "  ", KEY1)
    with pytest.raises(ValidationFailed):
        returns_svc.request_return(CUST, "ORD-1", "ok", "short")


def test_retry_with_same_key_returns_same_record_and_creates_one(stores, returns_svc):
    put_order(stores, make_order())
    first = returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    second = returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    assert first.return_id == second.return_id
    assert len(stores["returns"].list_for_customer("cust-1")) == 1


def test_same_key_different_payload_is_rejected(stores, returns_svc):
    put_order(stores, make_order())
    put_order(stores, make_order(order_id="ORD-2"))
    returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    with pytest.raises(IdempotencyConflict):
        returns_svc.request_return(CUST, "ORD-2", "Defective", KEY1)


def test_second_return_for_same_order_with_new_key_is_blocked(stores, returns_svc):
    put_order(stores, make_order())
    returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    with pytest.raises(PolicyViolation, match="ALREADY_RETURNED"):
        returns_svc.request_return(CUST, "ORD-1", "Defective again", KEY2)


def test_order_guard_blocks_even_if_eligibility_snapshot_is_stale(stores, returns_svc, clock):
    """Simulates the race: another request already holds the per-order guard."""
    put_order(stores, make_order())
    stores["idempotency"].begin("cust-1#order-return-guard#ORD-1", "order-guard", clock())
    stores["idempotency"].complete("cust-1#order-return-guard#ORD-1", "RET-OTHER", clock())
    with pytest.raises(PolicyViolation, match="ALREADY_RETURNED"):
        returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    # failed request must not leave a stuck idempotency claim
    stores["idempotency"].begin("cust-1#return#" + KEY1, "different", clock())


def test_in_progress_duplicate_is_rejected_then_failure_allows_retry(stores, returns_svc, clock):
    put_order(stores, make_order())
    import hashlib

    h = hashlib.sha256(b"ORD-1|Defective").hexdigest()
    stores["idempotency"].begin("cust-1#return#" + KEY1, h, clock())  # concurrent worker
    with pytest.raises(Conflict):
        returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)
    clock.advance(minutes=10)  # worker died; lock expired -> safe takeover
    assert returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1).status == (
        ReturnStatus.APPROVED
    )


# ---------------- approval ----------------


def _pending(stores, returns_svc):
    put_order(stores, make_order(amount="74999", category="electronics"))
    return returns_svc.request_return(CUST, "ORD-1", "Not as described", KEY1)


def test_only_approvers_can_decide(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    for ctx in (CUST, AGENT, SYSTEM_CTX):
        with pytest.raises(Forbidden):
            returns_svc.decide(ctx, "cust-1", rec.return_id, True)


def test_approver_cannot_approve_own_request(stores, returns_svc):
    put_order(stores, make_order("appr-1", "ORD-9", amount="74999", category="electronics"))
    rec = returns_svc.request_return(APPROVER, "ORD-9", "Mine", KEY1)
    with pytest.raises(Forbidden, match="own"):
        returns_svc.decide(APPROVER, "appr-1", rec.return_id, True)


def test_approve_records_decider_and_audit(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    done = returns_svc.decide(APPROVER, "cust-1", rec.return_id, True, "looks fine")
    assert done.status == ReturnStatus.APPROVED and done.decided_by == "appr-1"
    assert audit_events(stores, rec.return_id) == ["return.pending_approval", "return.approved"]


def test_cannot_decide_twice(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    returns_svc.decide(APPROVER, "cust-1", rec.return_id, True)
    with pytest.raises(InvalidTransition):
        returns_svc.decide(APPROVER, "cust-1", rec.return_id, False)


def test_rejection_frees_the_order_for_a_new_request(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    returns_svc.decide(APPROVER, "cust-1", rec.return_id, False, "damaged by customer")
    again = returns_svc.request_return(CUST, "ORD-1", "Different reason", KEY2)
    assert again.return_id != rec.return_id


def test_decision_after_expiry_is_refused_and_marks_expired(stores, returns_svc, clock):
    rec = _pending(stores, returns_svc)
    clock.advance(hours=49)
    with pytest.raises(PolicyViolation, match="expired"):
        returns_svc.decide(APPROVER, "cust-1", rec.return_id, True)
    assert returns_svc.get_return(CUST, rec.return_id).status == ReturnStatus.EXPIRED


def test_expiry_sweep(stores, returns_svc, clock):
    rec = _pending(stores, returns_svc)
    assert returns_svc.expire_pending() == 0
    clock.advance(hours=49)
    assert returns_svc.expire_pending() == 1
    assert returns_svc.get_return(CUST, rec.return_id).status == ReturnStatus.EXPIRED
    assert audit_events(stores, rec.return_id)[-1] == "return.expired"
    assert returns_svc.expire_pending() == 0


def test_optimistic_locking_rejects_stale_writes(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    stale = stores["returns"].get("cust-1", rec.return_id)
    returns_svc.decide(APPROVER, "cust-1", rec.return_id, True)  # bumps version
    with pytest.raises(ConcurrentModification):
        stores["returns"].update(
            stale.model_copy(update={"status": ReturnStatus.REJECTED}),
            expected_version=stale.version,
        )
    assert returns_svc.get_return(CUST, rec.return_id).status == ReturnStatus.APPROVED


# ---------------- refund execution ----------------


def _approved(stores, returns_svc):
    put_order(stores, make_order(amount="2499"))
    return returns_svc.request_return(CUST, "ORD-1", "Defective", KEY1)


def test_refund_requires_approved_state(stores, returns_svc):
    rec = _pending(stores, returns_svc)
    with pytest.raises(InvalidTransition):
        returns_svc.execute_refund(SYSTEM_CTX, "cust-1", rec.return_id)


def test_refund_requires_system_or_approver_role(stores, returns_svc):
    rec = _approved(stores, returns_svc)
    for ctx in (CUST, AGENT):
        with pytest.raises(Forbidden):
            returns_svc.execute_refund(ctx, "cust-1", rec.return_id)


def test_refund_executes_once_even_if_called_twice(stores, returns_svc, payments):
    rec = _approved(stores, returns_svc)
    first = returns_svc.execute_refund(SYSTEM_CTX, "cust-1", rec.return_id)
    second = returns_svc.execute_refund(SYSTEM_CTX, "cust-1", rec.return_id)
    assert first.status == second.status == ReturnStatus.EXECUTED
    assert first.refund_reference == f"RFD-{rec.return_id}"
    assert payments.calls == [rec.return_id]  # gateway hit exactly once
    assert audit_events(stores, rec.return_id).count("refund.executed") == 1


def test_gateway_failure_marks_failed_then_retry_pays_exactly_once(
    stores, returns_svc, payments
):
    rec = _approved(stores, returns_svc)
    payments.fail_next = 1
    with pytest.raises(DependencyFailure):
        returns_svc.execute_refund(SYSTEM_CTX, "cust-1", rec.return_id)
    failed = returns_svc.get_return(CUST, rec.return_id)
    assert failed.status == ReturnStatus.FAILED and failed.failure_reason

    done = returns_svc.execute_refund(SYSTEM_CTX, "cust-1", rec.return_id)
    assert done.status == ReturnStatus.EXECUTED and done.failure_reason is None
    assert list(payments.payouts) == [rec.return_id]  # one payout, same idempotency key


def test_daily_refund_cap(stores, clock, payments):
    returns_svc = ReturnService(
        stores["orders"],
        stores["returns"],
        stores["idempotency"],
        stores["audit"],
        payments,
        clock,
        policy=ReturnPolicy(daily_refund_cap=Decimal("3000")),
    )
    put_order(stores, make_order(order_id="ORD-A", amount="2499"))
    put_order(stores, make_order(order_id="ORD-B", amount="2499"))
    a = returns_svc.request_return(CUST, "ORD-A", "Defective", KEY1)
    b = returns_svc.request_return(CUST, "ORD-B", "Defective", KEY2)
    returns_svc.execute_refund(SYSTEM_CTX, "cust-1", a.return_id)
    with pytest.raises(PolicyViolation, match="daily"):
        returns_svc.execute_refund(SYSTEM_CTX, "cust-1", b.return_id)
    assert returns_svc.get_return(CUST, b.return_id).status == ReturnStatus.APPROVED
    clock.advance(days=1)  # next day the cap resets
    assert (
        returns_svc.execute_refund(SYSTEM_CTX, "cust-1", b.return_id).status
        == ReturnStatus.EXECUTED
    )


# ---------------- tickets ----------------


def test_create_ticket_and_idempotent_retry(stores, tickets_svc):
    first = tickets_svc.create_ticket(CUST, "delivery", "Late", "Laptop not here", KEY1)
    second = tickets_svc.create_ticket(CUST, "delivery", "Late", "Laptop not here", KEY1)
    assert first.ticket_id == second.ticket_id and first.status == TicketStatus.OPEN
    assert audit_events(stores, first.ticket_id) == ["ticket.created"]


def test_ticket_rejects_bad_category_and_foreign_order(stores, tickets_svc):
    put_order(stores, make_order("cust-2", "ORD-2"))
    with pytest.raises(ValidationFailed):
        tickets_svc.create_ticket(CUST, "bogus", "s", "d", KEY1)
    with pytest.raises(NotFound):
        tickets_svc.create_ticket(CUST, "delivery", "s", "d", KEY1, related_order_id="ORD-2")


def test_ticket_isolation_between_customers(stores, tickets_svc):
    t = tickets_svc.create_ticket(CUST, "complaint", "s", "d", KEY1)
    with pytest.raises(NotFound):
        tickets_svc.get_ticket(OTHER, t.ticket_id)
    with pytest.raises(NotFound):
        tickets_svc.escalate(OTHER, t.ticket_id, "summary")


def test_escalation_and_resolution_flow(stores, tickets_svc):
    t = tickets_svc.create_ticket(CUST, "refund", "s", "d", KEY1)
    esc = tickets_svc.escalate(CUST, t.ticket_id, "Customer asked for human; order ORD-1 late")
    assert esc.status == TicketStatus.ESCALATED and esc.escalation_summary
    assert [x.ticket_id for x in stores["tickets"].list_by_status(TicketStatus.ESCALATED)] == [
        t.ticket_id
    ]
    with pytest.raises(Forbidden):
        tickets_svc.resolve(CUST, "cust-1", t.ticket_id)
    assert tickets_svc.resolve(AGENT, "cust-1", t.ticket_id).status == TicketStatus.RESOLVED
