"""Returns & refunds: eligibility, request, approval, execution, expiry.

Safeguards implemented here:
- Eligibility is computed by the rules engine from backend data (never by the model).
- Idempotency keys make client retries safe; reuse with a different payload is rejected.
- A per-order guard prevents duplicate returns even under concurrent requests.
- Optimistic locking (version + conditional write) on every state change.
- Approval expiry, separation of duties (approver cannot be the requesting customer).
- Refund execution is idempotent at the payment gateway (key = return_id) and capped per
  customer per day.
- Every state change is written to the append-only audit log.
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from csagent_core.context import APPROVERS, SYSTEM, CallerContext
from csagent_core.errors import (
    ConcurrentModification,
    DependencyFailure,
    Forbidden,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from csagent_core.models import ReturnRecord, ReturnStatus, assert_transition
from csagent_core.policy import (
    DEFAULT_POLICY,
    EligibilityDecision,
    Reason,
    ReturnPolicy,
    evaluate_eligibility,
)
from csagent_core.ports import (
    AuditLog,
    Clock,
    IdempotencyStore,
    OrderRepository,
    PaymentGateway,
    ReturnRepository,
)


def _guard_key(customer_id: str, order_id: str) -> str:
    return f"{customer_id}#order-return-guard#{order_id}"


def _new_return_id() -> str:
    return f"RET-{uuid.uuid4().hex[:12].upper()}"


class ReturnService:
    def __init__(
        self,
        orders: OrderRepository,
        returns: ReturnRepository,
        idempotency: IdempotencyStore,
        audit: AuditLog,
        payments: PaymentGateway,
        clock: Clock,
        policy: ReturnPolicy = DEFAULT_POLICY,
        id_factory: Callable[[], str] = _new_return_id,
    ) -> None:
        self._orders = orders
        self._returns = returns
        self._idem = idempotency
        self._audit = audit
        self._payments = payments
        self._clock = clock
        self._policy = policy
        self._new_id = id_factory

    # ---------- customer-facing ----------

    def check_eligibility(self, ctx: CallerContext, order_id: str) -> EligibilityDecision:
        order = self._orders.get(ctx.customer_id, order_id)
        if order is None:
            raise NotFound("order not found")
        existing = self._returns.list_for_customer(ctx.customer_id)
        return evaluate_eligibility(order, existing, self._clock(), self._policy)

    def get_return(self, ctx: CallerContext, return_id: str) -> ReturnRecord:
        record = self._returns.get(ctx.customer_id, return_id)
        if record is None:
            raise NotFound("return not found")
        return record

    def request_return(
        self, ctx: CallerContext, order_id: str, reason: str, idempotency_key: str
    ) -> ReturnRecord:
        reason = (reason or "").strip()
        if not 1 <= len(reason) <= 1000:
            raise ValidationFailed("reason must be 1-1000 characters")
        if not 8 <= len(idempotency_key or "") <= 128:
            raise ValidationFailed("idempotency_key must be 8-128 characters")

        customer_id = ctx.customer_id
        now = self._clock()
        client_key = f"{customer_id}#return#{idempotency_key}"
        request_hash = hashlib.sha256(f"{order_id}|{reason}".encode()).hexdigest()

        prior = self._idem.begin(client_key, request_hash, now)
        if prior is not None:
            return self.get_return(ctx, prior)

        guard_key = _guard_key(customer_id, order_id)
        guard_claimed = False
        try:
            order = self._orders.get(customer_id, order_id)
            if order is None:
                raise NotFound("order not found")

            decision = evaluate_eligibility(
                order, self._returns.list_for_customer(customer_id), now, self._policy
            )
            if not decision.eligible:
                reasons = ", ".join(r.value for r in decision.reasons)
                raise PolicyViolation(f"return not allowed: {reasons}")

            # Concurrency guard: only one active return per order, even for parallel requests.
            if self._idem.begin(guard_key, "order-guard", now) is not None:
                raise PolicyViolation(f"return not allowed: {Reason.ALREADY_RETURNED.value}")
            guard_claimed = True

            needs_approval = decision.requires_approval
            record = ReturnRecord(
                customer_id=customer_id,
                return_id=self._new_id(),
                order_id=order_id,
                status=ReturnStatus.PENDING_APPROVAL if needs_approval else ReturnStatus.APPROVED,
                reason=reason,
                refund_amount=decision.refund_amount,
                currency=decision.currency,
                created_at=now,
                updated_at=now,
                requires_approval=needs_approval,
                approval_expires_at=now + self._policy.approval_ttl if needs_approval else None,
            )
            self._returns.create(record)
            self._idem.complete(guard_key, record.return_id, now)
            self._idem.complete(client_key, record.return_id, now)
        except Exception:
            if guard_claimed:
                self._idem.release(guard_key)
            self._idem.release(client_key)
            raise

        self._audit.append(
            record.return_id,
            "return.pending_approval" if record.requires_approval else "return.auto_approved",
            ctx.subject,
            {
                "order_id": order_id,
                "amount": str(record.refund_amount),
                "currency": record.currency,
                "request_id": ctx.request_id,
            },
        )
        return record

    # ---------- staff / workflow ----------

    def decide(
        self,
        ctx: CallerContext,
        customer_id: str,
        return_id: str,
        approve: bool,
        note: str = "",
    ) -> ReturnRecord:
        if not ctx.has_group(APPROVERS):
            raise Forbidden("approver role required")
        if ctx.subject == customer_id:
            raise Forbidden("approvers cannot decide their own requests")

        record = self._get_for_staff(customer_id, return_id)
        now = self._clock()
        target = ReturnStatus.APPROVED if approve else ReturnStatus.REJECTED
        assert_transition(record.status, target)

        if record.approval_expires_at is not None and now > record.approval_expires_at:
            self._expire(record, now)
            raise PolicyViolation("approval window has expired")

        updated = self._transition(
            record,
            target,
            now,
            decided_by=ctx.subject,
            decision_note=(note or "")[:500] or None,
        )
        if target == ReturnStatus.REJECTED:
            self._idem.release(_guard_key(customer_id, record.order_id))
        self._audit.append(
            return_id,
            "return.approved" if approve else "return.rejected",
            ctx.subject,
            {"request_id": ctx.request_id},
        )
        return updated

    def execute_refund(
        self, ctx: CallerContext, customer_id: str, return_id: str
    ) -> ReturnRecord:
        if not (ctx.has_group(SYSTEM) or ctx.has_group(APPROVERS)):
            raise Forbidden("system or approver role required")

        record = self._get_for_staff(customer_id, return_id)
        if record.status == ReturnStatus.EXECUTED:
            return record  # idempotent: already refunded
        assert_transition(record.status, ReturnStatus.EXECUTED)

        now = self._clock()
        self._enforce_daily_cap(customer_id, record.refund_amount, now)

        try:
            # Idempotency key is the return_id: retries (or a racing worker) can never
            # produce a second payout at the gateway.
            reference = self._payments.refund(
                record.return_id, customer_id, record.refund_amount, record.currency
            )
        except DependencyFailure as exc:
            self._transition(record, ReturnStatus.FAILED, now, failure_reason=exc.code)
            self._audit.append(
                return_id, "refund.failed", ctx.subject, {"request_id": ctx.request_id}
            )
            raise

        updated = self._transition(
            record, ReturnStatus.EXECUTED, now, refund_reference=reference, failure_reason=None
        )
        self._audit.append(
            return_id,
            "refund.executed",
            ctx.subject,
            {"reference": reference, "amount": str(record.refund_amount)},
        )
        return updated

    def list_awaiting_approval(self, ctx: CallerContext) -> list[ReturnRecord]:
        if not ctx.has_group(APPROVERS):
            raise Forbidden("approver role required")
        return sorted(
            self._returns.list_by_status(ReturnStatus.PENDING_APPROVAL),
            key=lambda r: r.created_at,
        )

    def list_unexecuted(self, min_age: timedelta) -> list[ReturnRecord]:
        """APPROVED/FAILED returns that have waited at least `min_age`. Used by the
        reconciler to re-enqueue executions whose queue message was lost."""
        cutoff = self._clock() - min_age
        stuck = self._returns.list_by_status(
            ReturnStatus.APPROVED
        ) + self._returns.list_by_status(ReturnStatus.FAILED)
        return [r for r in stuck if r.updated_at <= cutoff]

    def expire_pending(self) -> int:
        """Sweep approvals past their deadline. Safe to run on a schedule."""
        now = self._clock()
        expired = 0
        for record in self._returns.list_by_status(ReturnStatus.PENDING_APPROVAL):
            if record.approval_expires_at and now > record.approval_expires_at:
                try:
                    self._expire(record, now)
                    expired += 1
                except ConcurrentModification:
                    continue  # someone decided it concurrently; leave it
        return expired

    # ---------- internals ----------

    def _get_for_staff(self, customer_id: str, return_id: str) -> ReturnRecord:
        record = self._returns.get(customer_id, return_id)
        if record is None:
            raise NotFound("return not found")
        return record

    def _transition(
        self, record: ReturnRecord, target: ReturnStatus, now: datetime, **changes: object
    ) -> ReturnRecord:
        assert_transition(record.status, target)
        candidate = record.model_copy(
            update={"status": target, "updated_at": now, **changes}
        )
        return self._returns.update(candidate, expected_version=record.version)

    def _expire(self, record: ReturnRecord, now: datetime) -> None:
        self._transition(record, ReturnStatus.EXPIRED, now)
        self._idem.release(_guard_key(record.customer_id, record.order_id))
        self._audit.append(record.return_id, "return.expired", "system", {})

    def _enforce_daily_cap(self, customer_id: str, amount: Decimal, now: datetime) -> None:
        refunded_today = sum(
            (
                r.refund_amount
                for r in self._returns.list_for_customer(customer_id)
                if r.status == ReturnStatus.EXECUTED and r.updated_at.date() == now.date()
            ),
            Decimal("0"),
        )
        if refunded_today + amount > self._policy.daily_refund_cap:
            raise PolicyViolation("daily refund limit exceeded; requires manual review")
