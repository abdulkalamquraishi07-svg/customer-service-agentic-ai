"""DynamoDB adapters. Conditional writes provide create-once, optimistic locking and
idempotency claims; customer_id is always the partition key for customer-owned data."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from csagent_core.errors import ConcurrentModification, Conflict, IdempotencyConflict
from csagent_core.models import Order, ReturnRecord, ReturnStatus, Ticket, TicketStatus

LOCK_SECONDS = 300
COMPLETED_RETENTION = timedelta(days=90)
_CONDITION_FAILED = "ConditionalCheckFailedException"


def _is_condition_failed(exc: ClientError) -> bool:
    return bool(exc.response.get("Error", {}).get("Code") == _CONDITION_FAILED)


def _clean(value: Any) -> Any:
    """Convert a pydantic dump into DynamoDB-safe types (no None, no floats, ISO dates)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, float):
        return Decimal(str(value))
    return value


def to_item(model: Any) -> dict[str, Any]:
    return _clean(model.model_dump(mode="python"))  # type: ignore[no-any-return]


def _query_all(table: Any, **kwargs: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


class DynamoOrders:
    def __init__(self, table: Any) -> None:
        self._t = table

    def get(self, customer_id: str, order_id: str) -> Order | None:
        item = self._t.get_item(Key={"customer_id": customer_id, "order_id": order_id}).get("Item")
        return Order(**item) if item else None

    def list_for_customer(self, customer_id: str) -> list[Order]:
        items = _query_all(self._t, KeyConditionExpression=Key("customer_id").eq(customer_id))
        return [Order(**i) for i in items]


class DynamoReturns:
    def __init__(self, table: Any) -> None:
        self._t = table

    def get(self, customer_id: str, return_id: str) -> ReturnRecord | None:
        item = self._t.get_item(Key={"customer_id": customer_id, "return_id": return_id}).get(
            "Item"
        )
        return ReturnRecord(**item) if item else None

    def list_for_customer(self, customer_id: str) -> list[ReturnRecord]:
        items = _query_all(self._t, KeyConditionExpression=Key("customer_id").eq(customer_id))
        return [ReturnRecord(**i) for i in items]

    def list_by_status(self, status: ReturnStatus) -> list[ReturnRecord]:
        items = _query_all(
            self._t,
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq(status.value),
        )
        return [ReturnRecord(**i) for i in items]

    def create(self, record: ReturnRecord) -> None:
        try:
            self._t.put_item(
                Item=to_item(record), ConditionExpression="attribute_not_exists(customer_id)"
            )
        except ClientError as exc:
            if _is_condition_failed(exc):
                raise Conflict("return already exists") from exc
            raise

    def update(self, record: ReturnRecord, expected_version: int) -> ReturnRecord:
        stored = record.model_copy(update={"version": expected_version + 1})
        try:
            self._t.put_item(
                Item=to_item(stored),
                ConditionExpression="#v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": expected_version},
            )
        except ClientError as exc:
            if _is_condition_failed(exc):
                raise ConcurrentModification("return was modified by another request") from exc
            raise
        return stored


class DynamoTickets:
    def __init__(self, table: Any) -> None:
        self._t = table

    def get(self, customer_id: str, ticket_id: str) -> Ticket | None:
        item = self._t.get_item(Key={"customer_id": customer_id, "ticket_id": ticket_id}).get(
            "Item"
        )
        return Ticket(**item) if item else None

    def list_by_status(self, status: TicketStatus) -> list[Ticket]:
        items = _query_all(
            self._t,
            IndexName="status-index",
            KeyConditionExpression=Key("status").eq(status.value),
        )
        return [Ticket(**i) for i in items]

    def create(self, ticket: Ticket) -> None:
        try:
            self._t.put_item(
                Item=to_item(ticket), ConditionExpression="attribute_not_exists(customer_id)"
            )
        except ClientError as exc:
            if _is_condition_failed(exc):
                raise Conflict("ticket already exists") from exc
            raise

    def update(self, ticket: Ticket, expected_version: int) -> Ticket:
        stored = ticket.model_copy(update={"version": expected_version + 1})
        try:
            self._t.put_item(
                Item=to_item(stored),
                ConditionExpression="#v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": expected_version},
            )
        except ClientError as exc:
            if _is_condition_failed(exc):
                raise ConcurrentModification("ticket was modified by another request") from exc
            raise
        return stored


class DynamoIdempotency:
    def __init__(self, table: Any) -> None:
        self._t = table

    @staticmethod
    def _lock_until(now: datetime) -> datetime:
        return now + timedelta(seconds=LOCK_SECONDS)

    def _new_item(self, key: str, request_hash: str, now: datetime) -> dict[str, Any]:
        locked_until = self._lock_until(now)
        return {
            "idempotency_key": key,
            "request_hash": request_hash,
            "locked_until": locked_until.isoformat(),
            # Abandoned in-progress claims are garbage-collected by TTL.
            "expires_at": int((locked_until + timedelta(days=1)).timestamp()),
        }

    def begin(self, key: str, request_hash: str, now: datetime) -> str | None:
        try:
            self._t.put_item(
                Item=self._new_item(key, request_hash, now),
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
            return None
        except ClientError as exc:
            if not _is_condition_failed(exc):
                raise

        existing = self._t.get_item(Key={"idempotency_key": key}, ConsistentRead=True).get("Item")
        if existing is None:  # released between our put and get; claim again
            return self.begin(key, request_hash, now)
        if existing["request_hash"] != request_hash:
            raise IdempotencyConflict("idempotency key reused with a different request")
        if "result_ref" in existing:
            return str(existing["result_ref"])
        if now.isoformat() < existing["locked_until"]:
            raise Conflict("an identical request is still in progress")

        # Stale lock from a crashed worker: take over atomically.
        try:
            self._t.put_item(
                Item=self._new_item(key, request_hash, now),
                ConditionExpression="locked_until = :old AND attribute_not_exists(result_ref)",
                ExpressionAttributeValues={":old": existing["locked_until"]},
            )
        except ClientError as exc:
            if _is_condition_failed(exc):
                raise Conflict("an identical request is still in progress") from exc
            raise
        return None

    def complete(self, key: str, result_ref: str, now: datetime) -> None:
        self._t.update_item(
            Key={"idempotency_key": key},
            UpdateExpression="SET result_ref = :r, expires_at = :e",
            ExpressionAttributeValues={
                ":r": result_ref,
                ":e": int((now + COMPLETED_RETENTION).timestamp()),
            },
        )

    def release(self, key: str) -> None:
        self._t.delete_item(Key={"idempotency_key": key})


class DynamoAudit:
    """Append-only: items are only ever created, never updated (enforced by IAM + condition)."""

    def __init__(self, table: Any) -> None:
        self._t = table

    def append(self, entity_id: str, event: str, actor: str, details: dict[str, Any]) -> None:
        event_ts = f"{datetime.now(UTC).isoformat()}#{uuid.uuid4().hex[:8]}"
        self._t.put_item(
            Item={
                "entity_id": entity_id,
                "event_ts": event_ts,
                "event": event,
                "actor": actor,
                "details": json.dumps(details, default=str, sort_keys=True),
            },
            ConditionExpression="attribute_not_exists(entity_id)",
        )

    def list_for_entity(self, entity_id: str) -> list[dict[str, Any]]:
        return _query_all(self._t, KeyConditionExpression=Key("entity_id").eq(entity_id))
