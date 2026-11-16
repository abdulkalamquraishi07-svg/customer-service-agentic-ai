"""Domain models and the refund/return state machine."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from csagent_core.errors import InvalidTransition


class OrderStatus(StrEnum):
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class OrderItem(BaseModel):
    sku: str
    name: str
    qty: int = Field(ge=1)
    price: Decimal = Field(ge=0)


class Order(BaseModel):
    model_config = ConfigDict(extra="ignore")

    customer_id: str
    order_id: str
    status: OrderStatus
    ordered_at: datetime
    promised_delivery_at: datetime
    delivered_at: datetime | None = None
    carrier: str | None = None
    tracking_number: str | None = None
    shipment_status: str = "NOT_SHIPPED"
    last_scan: str | None = None
    total_amount: Decimal = Field(ge=0)
    currency: str = "INR"
    category: str
    items: list[OrderItem] = Field(default_factory=list)
    version: int = 1


class ShipmentView(BaseModel):
    """What the Order agent may see/say. Backend-verified facts only."""

    order_id: str
    status: OrderStatus
    shipment_status: str
    carrier: str | None
    tracking_number: str | None
    last_scan: str | None
    ordered_at: datetime
    promised_delivery_at: datetime
    delivered_at: datetime | None
    is_overdue: bool
    days_overdue: int
    # What was bought, so "my laptop" can be matched to an order without guessing.
    category: str
    item_names: list[str]


class ReturnStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


# Legal transitions. Anything else raises InvalidTransition.
TRANSITIONS: dict[ReturnStatus, frozenset[ReturnStatus]] = {
    ReturnStatus.PENDING_APPROVAL: frozenset(
        {ReturnStatus.APPROVED, ReturnStatus.REJECTED, ReturnStatus.EXPIRED}
    ),
    ReturnStatus.APPROVED: frozenset({ReturnStatus.EXECUTED, ReturnStatus.FAILED}),
    ReturnStatus.FAILED: frozenset({ReturnStatus.EXECUTED, ReturnStatus.FAILED}),  # retry
    ReturnStatus.EXECUTED: frozenset({ReturnStatus.REVERSED}),
    ReturnStatus.REJECTED: frozenset(),
    ReturnStatus.EXPIRED: frozenset(),
    ReturnStatus.REVERSED: frozenset(),
}

# States in which a return still "occupies" the order (blocks duplicates).
ACTIVE_STATES = frozenset(
    {
        ReturnStatus.PENDING_APPROVAL,
        ReturnStatus.APPROVED,
        ReturnStatus.FAILED,
        ReturnStatus.EXECUTED,
    }
)


def assert_transition(current: ReturnStatus, new: ReturnStatus) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"Cannot move return from {current} to {new}")


class ReturnRecord(BaseModel):
    customer_id: str
    return_id: str
    order_id: str
    status: ReturnStatus
    reason: str
    refund_amount: Decimal = Field(ge=0)
    currency: str
    created_at: datetime
    updated_at: datetime
    requires_approval: bool
    approval_expires_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None
    refund_reference: str | None = None
    failure_reason: str | None = None
    version: int = 1


class TicketStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"


class Ticket(BaseModel):
    customer_id: str
    ticket_id: str
    status: TicketStatus
    category: str
    subject: str = Field(max_length=200)
    description: str = Field(max_length=4000)
    related_order_id: str | None = None
    escalation_summary: str | None = None
    created_at: datetime
    updated_at: datetime
    version: int = 1
