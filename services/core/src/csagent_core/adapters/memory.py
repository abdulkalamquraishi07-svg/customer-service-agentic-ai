"""In-memory adapters with the same semantics as the DynamoDB ones (used in tests/local dev)."""

from datetime import datetime, timedelta
from typing import Any

from csagent_core.errors import ConcurrentModification, Conflict, IdempotencyConflict
from csagent_core.models import Order, ReturnRecord, ReturnStatus, Ticket, TicketStatus

LOCK_SECONDS = 300


class InMemoryOrders:
    def __init__(self, orders: list[Order] | None = None) -> None:
        self._data = {(o.customer_id, o.order_id): o for o in orders or []}

    def put(self, order: Order) -> None:
        self._data[(order.customer_id, order.order_id)] = order

    def get(self, customer_id: str, order_id: str) -> Order | None:
        return self._data.get((customer_id, order_id))

    def list_for_customer(self, customer_id: str) -> list[Order]:
        return [o for (c, _), o in self._data.items() if c == customer_id]


class InMemoryReturns:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], ReturnRecord] = {}

    def get(self, customer_id: str, return_id: str) -> ReturnRecord | None:
        return self._data.get((customer_id, return_id))

    def list_for_customer(self, customer_id: str) -> list[ReturnRecord]:
        return [r for (c, _), r in self._data.items() if c == customer_id]

    def list_by_status(self, status: ReturnStatus) -> list[ReturnRecord]:
        return [r for r in self._data.values() if r.status == status]

    def create(self, record: ReturnRecord) -> None:
        key = (record.customer_id, record.return_id)
        if key in self._data:
            raise Conflict("return already exists")
        self._data[key] = record

    def update(self, record: ReturnRecord, expected_version: int) -> ReturnRecord:
        key = (record.customer_id, record.return_id)
        current = self._data.get(key)
        if current is None or current.version != expected_version:
            raise ConcurrentModification("return was modified by another request")
        stored = record.model_copy(update={"version": expected_version + 1})
        self._data[key] = stored
        return stored


class InMemoryTickets:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], Ticket] = {}

    def get(self, customer_id: str, ticket_id: str) -> Ticket | None:
        return self._data.get((customer_id, ticket_id))

    def list_by_status(self, status: TicketStatus) -> list[Ticket]:
        return [t for t in self._data.values() if t.status == status]

    def create(self, ticket: Ticket) -> None:
        key = (ticket.customer_id, ticket.ticket_id)
        if key in self._data:
            raise Conflict("ticket already exists")
        self._data[key] = ticket

    def update(self, ticket: Ticket, expected_version: int) -> Ticket:
        key = (ticket.customer_id, ticket.ticket_id)
        current = self._data.get(key)
        if current is None or current.version != expected_version:
            raise ConcurrentModification("ticket was modified by another request")
        stored = ticket.model_copy(update={"version": expected_version + 1})
        self._data[key] = stored
        return stored


class InMemoryIdempotency:
    def __init__(self) -> None:
        # key -> (request_hash, result_ref | None, locked_until)
        self._data: dict[str, tuple[str, str | None, datetime]] = {}

    def begin(self, key: str, request_hash: str, now: datetime) -> str | None:
        existing = self._data.get(key)
        if existing is None:
            self._data[key] = (request_hash, None, now + timedelta(seconds=LOCK_SECONDS))
            return None
        stored_hash, result_ref, locked_until = existing
        if stored_hash != request_hash:
            raise IdempotencyConflict("idempotency key reused with a different request")
        if result_ref is not None:
            return result_ref
        if now < locked_until:
            raise Conflict("an identical request is still in progress")
        self._data[key] = (request_hash, None, now + timedelta(seconds=LOCK_SECONDS))
        return None

    def complete(self, key: str, result_ref: str, now: datetime) -> None:
        request_hash, _, locked_until = self._data[key]
        self._data[key] = (request_hash, result_ref, locked_until)

    def release(self, key: str) -> None:
        self._data.pop(key, None)


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append(self, entity_id: str, event: str, actor: str, details: dict[str, Any]) -> None:
        self.events.append(
            {"entity_id": entity_id, "event": event, "actor": actor, "details": details}
        )
