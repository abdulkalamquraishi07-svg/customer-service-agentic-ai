import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
import pytest
from bizapi.container import Container, set_container
from csagent_core.adapters.dynamodb import (
    DynamoAudit,
    DynamoIdempotency,
    DynamoOrders,
    DynamoReturns,
    DynamoTickets,
    to_item,
)
from csagent_core.adapters.simulated_payments import SimulatedPaymentGateway
from csagent_core.models import Order, OrderStatus
from csagent_core.order_service import OrderService
from csagent_core.return_service import ReturnService
from csagent_core.ticket_service import TicketService
from moto import mock_aws

T0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class FakeQueue:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.fail = False

    def send(self, customer_id: str, return_id: str) -> None:
        if self.fail:
            raise RuntimeError("sqs down")
        self.messages.append({"customer_id": customer_id, "return_id": return_id})

    def sqs_event(self) -> dict:
        return {
            "Records": [
                {"messageId": f"m{i}", "body": json.dumps(m)} for i, m in enumerate(self.messages)
            ]
        }


def _tables(ddb):
    def make(name, pk, sk=None, gsi=False):
        attrs = [{"AttributeName": pk, "AttributeType": "S"}]
        schema = [{"AttributeName": pk, "KeyType": "HASH"}]
        if sk:
            attrs.append({"AttributeName": sk, "AttributeType": "S"})
            schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        kw = {}
        if gsi:
            attrs += [
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ]
            kw["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ]
        return ddb.create_table(
            TableName=name,
            AttributeDefinitions=attrs,
            KeySchema=schema,
            BillingMode="PAY_PER_REQUEST",
            **kw,
        )

    return {
        "orders": make("orders", "customer_id", "order_id"),
        "returns": make("returns", "customer_id", "return_id", gsi=True),
        "tickets": make("tickets", "customer_id", "ticket_id", gsi=True),
        "idempotency": make("idempotency", "idempotency_key"),
        "audit": make("audit", "entity_id", "event_ts"),
        "ledger": make("ledger", "idempotency_key"),
    }


class World:
    """Everything a handler test needs."""

    def __init__(self, tables, clock: Clock, queue: FakeQueue) -> None:
        self.tables = tables
        self.clock = clock
        self.queue = queue
        self.orders = DynamoOrders(tables["orders"])
        self.returns_repo = DynamoReturns(tables["returns"])
        self.payments = SimulatedPaymentGateway(tables["ledger"])
        self.idem = DynamoIdempotency(tables["idempotency"])
        self.audit = DynamoAudit(tables["audit"])
        self.install(self.payments)

    def install(self, payments) -> None:
        tickets = DynamoTickets(self.tables["tickets"])
        self.container = Container(
            orders=OrderService(self.orders, self.clock),
            returns=ReturnService(
                self.orders, self.returns_repo, self.idem, self.audit, payments, self.clock
            ),
            tickets=TicketService(tickets, self.orders, self.idem, self.audit, self.clock),
            queue=self.queue,
            clock=self.clock,
        )
        set_container(self.container)

    def add_order(self, customer_id="cust-1", order_id="ORD-1", **kw) -> Order:
        ordered_at = T0 - timedelta(days=kw.pop("ordered_days_ago", 8))
        status = kw.pop("status", OrderStatus.DELIVERED)
        delivered = kw.pop("delivered_days_ago", 2)
        order = Order(
            customer_id=customer_id,
            order_id=order_id,
            status=status,
            ordered_at=ordered_at,
            promised_delivery_at=ordered_at + timedelta(days=kw.pop("promised_days", 5)),
            delivered_at=(
                T0 - timedelta(days=delivered) if status == OrderStatus.DELIVERED else None
            ),
            total_amount=Decimal(kw.pop("amount", "2499")),
            category=kw.pop("category", "accessories"),
        )
        self.tables["orders"].put_item(Item=to_item(order))
        return order


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        yield World(_tables(ddb), Clock(), FakeQueue())
    set_container(None)


def make_event(
    method: str,
    resource: str,
    *,
    sub: str | None = "cust-1",
    groups: str = "customers",
    body: dict | None = None,
    path: dict | None = None,
    key: str | None = None,
) -> dict:
    claims = {} if sub is None else {"sub": sub, "cognito:groups": groups}
    path_values = path or {}
    return {
        "httpMethod": method,
        "resource": resource,
        "path": resource.format(**path_values) if path_values else resource,
        "pathParameters": path_values or None,
        "headers": {"Idempotency-Key": key} if key else {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"requestId": "req-test", "authorizer": {"claims": claims}},
    }
