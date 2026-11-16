"""Test helpers: fake clock, fake payment gateway, order factory."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from csagent_core.errors import DependencyFailure
from csagent_core.models import Order, OrderStatus

T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakePayments:
    """Idempotent on key like a real gateway; can be told to fail."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.payouts: dict[str, Decimal] = {}
        self.fail_next = 0

    def refund(self, idempotency_key: str, customer_id: str, amount: Decimal, currency: str) -> str:
        self.calls.append(idempotency_key)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise DependencyFailure("gateway unavailable")
        self.payouts.setdefault(idempotency_key, amount)  # dedup: second call pays nothing
        return f"RFD-{idempotency_key}"


def make_order(
    customer_id: str = "cust-1",
    order_id: str = "ORD-1",
    *,
    status: OrderStatus = OrderStatus.DELIVERED,
    delivered_days_ago: int | None = 2,
    ordered_days_ago: int = 8,
    promised_days_after_order: int = 5,
    amount: str = "2499",
    category: str = "accessories",
) -> Order:
    ordered_at = T0 - timedelta(days=ordered_days_ago)
    delivered_at = (
        T0 - timedelta(days=delivered_days_ago) if delivered_days_ago is not None else None
    )
    return Order(
        customer_id=customer_id,
        order_id=order_id,
        status=status,
        ordered_at=ordered_at,
        promised_delivery_at=ordered_at + timedelta(days=promised_days_after_order),
        delivered_at=delivered_at if status == OrderStatus.DELIVERED else None,
        total_amount=Decimal(amount),
        category=category,
        items=[],
    )
