"""Ports (interfaces) the services depend on. Implemented by in-memory and DynamoDB adapters."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from csagent_core.models import Order, ReturnRecord, ReturnStatus, Ticket, TicketStatus


class OrderRepository(Protocol):
    def get(self, customer_id: str, order_id: str) -> Order | None: ...
    def list_for_customer(self, customer_id: str) -> list[Order]: ...


class ReturnRepository(Protocol):
    def get(self, customer_id: str, return_id: str) -> ReturnRecord | None: ...
    def list_for_customer(self, customer_id: str) -> list[ReturnRecord]: ...
    def list_by_status(self, status: ReturnStatus) -> list[ReturnRecord]: ...

    def create(self, record: ReturnRecord) -> None:
        """Raise Conflict if the return_id already exists."""

    def update(self, record: ReturnRecord, expected_version: int) -> ReturnRecord:
        """Conditional write. Raise ConcurrentModification if version != expected_version.
        Returns the stored record with version incremented."""


class TicketRepository(Protocol):
    def get(self, customer_id: str, ticket_id: str) -> Ticket | None: ...
    def list_by_status(self, status: TicketStatus) -> list[Ticket]: ...
    def create(self, ticket: Ticket) -> None: ...
    def update(self, ticket: Ticket, expected_version: int) -> Ticket: ...


class IdempotencyStore(Protocol):
    def begin(self, key: str, request_hash: str, now: datetime) -> str | None:
        """Claim a key. Returns None if newly claimed, or the stored result reference if a
        prior identical request completed. Raises IdempotencyConflict for a different payload
        and Conflict if an identical request is still in progress."""

    def complete(self, key: str, result_ref: str, now: datetime) -> None: ...
    def release(self, key: str) -> None:
        """Drop an in-progress claim after a failure so the client can retry."""


class AuditLog(Protocol):
    def append(self, entity_id: str, event: str, actor: str, details: dict[str, Any]) -> None: ...


class PaymentGateway(Protocol):
    def refund(
        self, idempotency_key: str, customer_id: str, amount: Decimal, currency: str
    ) -> str:
        """Issue a refund. MUST be idempotent on idempotency_key. Returns a refund reference.
        Raise DependencyFailure on transient/downstream errors."""


class RefundQueue(Protocol):
    def send(self, customer_id: str, return_id: str) -> None:
        """Enqueue a refund execution. Duplicates are safe: execution is idempotent."""


class Clock(Protocol):
    def __call__(self) -> datetime: ...
