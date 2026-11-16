"""Return/refund business rules. Pure functions: no I/O, fully unit-testable.

Eligibility and refund amounts are computed here from backend data. The LLM never decides
them; it only relays the structured decision.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from csagent_core.models import ACTIVE_STATES, Order, OrderStatus, ReturnRecord


class Reason(StrEnum):
    ORDER_NOT_DELIVERED = "ORDER_NOT_DELIVERED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    RETURN_WINDOW_EXPIRED = "RETURN_WINDOW_EXPIRED"
    ALREADY_RETURNED = "ALREADY_RETURNED"


@dataclass(frozen=True)
class ReturnPolicy:
    window_days_by_category: dict[str, int] = field(
        default_factory=lambda: {"electronics": 10, "accessories": 30}
    )
    default_window_days: int = 15
    # Refunds above this amount need human approval.
    approval_threshold: Decimal = Decimal("10000")
    # Max total refunds executed per customer per UTC day.
    daily_refund_cap: Decimal = Decimal("100000")
    approval_ttl: timedelta = timedelta(hours=48)

    def window_days(self, category: str) -> int:
        return self.window_days_by_category.get(category.lower(), self.default_window_days)


DEFAULT_POLICY = ReturnPolicy()


@dataclass(frozen=True)
class EligibilityDecision:
    order_id: str
    eligible: bool
    reasons: tuple[Reason, ...]
    refund_amount: Decimal
    currency: str
    requires_approval: bool
    window_ends_at: datetime | None


def evaluate_eligibility(
    order: Order,
    existing_returns: Sequence[ReturnRecord],
    now: datetime,
    policy: ReturnPolicy = DEFAULT_POLICY,
) -> EligibilityDecision:
    reasons: list[Reason] = []
    window_ends_at: datetime | None = None

    if order.status == OrderStatus.CANCELLED:
        reasons.append(Reason.ORDER_CANCELLED)
    elif order.status != OrderStatus.DELIVERED or order.delivered_at is None:
        reasons.append(Reason.ORDER_NOT_DELIVERED)
    else:
        window_ends_at = order.delivered_at + timedelta(days=policy.window_days(order.category))
        if now > window_ends_at:
            reasons.append(Reason.RETURN_WINDOW_EXPIRED)

    if any(r.order_id == order.order_id and r.status in ACTIVE_STATES for r in existing_returns):
        reasons.append(Reason.ALREADY_RETURNED)

    eligible = not reasons
    amount = order.total_amount if eligible else Decimal("0")
    return EligibilityDecision(
        order_id=order.order_id,
        eligible=eligible,
        reasons=tuple(reasons),
        refund_amount=amount,
        currency=order.currency,
        requires_approval=eligible and amount > policy.approval_threshold,
        window_ends_at=window_ends_at,
    )
