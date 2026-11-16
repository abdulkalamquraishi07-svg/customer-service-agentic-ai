import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent))

from csagent_core.adapters.dynamodb import (  # noqa: E402
    DynamoAudit,
    DynamoIdempotency,
    DynamoOrders,
    DynamoReturns,
    DynamoTickets,
)
from csagent_core.adapters.memory import (  # noqa: E402
    InMemoryAudit,
    InMemoryIdempotency,
    InMemoryOrders,
    InMemoryReturns,
    InMemoryTickets,
)
from support import FakeClock, FakePayments  # noqa: E402


def _create_tables(ddb):
    def make(name, pk, sk=None, gsi=False):
        attrs = [{"AttributeName": pk, "AttributeType": "S"}]
        schema = [{"AttributeName": pk, "KeyType": "HASH"}]
        if sk:
            attrs.append({"AttributeName": sk, "AttributeType": "S"})
            schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        kwargs = {}
        if gsi:
            attrs += [
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
            ]
            kwargs["GlobalSecondaryIndexes"] = [
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
            **kwargs,
        )

    return {
        "orders": make("orders", "customer_id", "order_id"),
        "returns": make("returns", "customer_id", "return_id", gsi=True),
        "tickets": make("tickets", "customer_id", "ticket_id", gsi=True),
        "idempotency": make("idempotency", "idempotency_key"),
        "audit": make("audit", "entity_id", "event_ts"),
    }


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def payments():
    return FakePayments()


@pytest.fixture(params=["memory", "dynamodb"])
def stores(request, monkeypatch):
    """Adapter bundle; every test using it runs against both backends."""
    if request.param == "memory":
        yield {
            "orders": InMemoryOrders(),
            "returns": InMemoryReturns(),
            "tickets": InMemoryTickets(),
            "idempotency": InMemoryIdempotency(),
            "audit": InMemoryAudit(),
        }
        return

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        t = _create_tables(ddb)
        yield {
            "orders": DynamoOrders(t["orders"]),
            "returns": DynamoReturns(t["returns"]),
            "tickets": DynamoTickets(t["tickets"]),
            "idempotency": DynamoIdempotency(t["idempotency"]),
            "audit": DynamoAudit(t["audit"]),
        }


def put_order(stores, order):
    """Insert an order into whichever backend is active."""
    from csagent_core.adapters.dynamodb import to_item

    orders = stores["orders"]
    if isinstance(orders, InMemoryOrders):
        orders.put(order)
    else:
        orders._t.put_item(Item=to_item(order))
