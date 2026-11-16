"""Dependency wiring. Built once per Lambda container; overridable in tests."""

import os
from dataclasses import dataclass
from datetime import UTC, datetime

import boto3
from csagent_core.adapters.dynamodb import (
    DynamoAudit,
    DynamoIdempotency,
    DynamoOrders,
    DynamoReturns,
    DynamoTickets,
)
from csagent_core.adapters.simulated_payments import SimulatedPaymentGateway
from csagent_core.adapters.sqs import SqsRefundQueue
from csagent_core.order_service import OrderService
from csagent_core.ports import Clock, RefundQueue
from csagent_core.return_service import ReturnService
from csagent_core.ticket_service import TicketService


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Container:
    orders: OrderService
    returns: ReturnService
    tickets: TicketService
    queue: RefundQueue
    clock: Clock


_container: Container | None = None


def build_from_env() -> Container:
    ddb = boto3.resource("dynamodb")
    env = os.environ
    orders = DynamoOrders(ddb.Table(env["ORDERS_TABLE"]))
    returns = DynamoReturns(ddb.Table(env["RETURNS_TABLE"]))
    tickets = DynamoTickets(ddb.Table(env["TICKETS_TABLE"]))
    idempotency = DynamoIdempotency(ddb.Table(env["IDEMPOTENCY_TABLE"]))
    audit = DynamoAudit(ddb.Table(env["AUDIT_TABLE"]))
    payments = SimulatedPaymentGateway(ddb.Table(env["LEDGER_TABLE"]))
    queue = SqsRefundQueue(boto3.client("sqs"), env["REFUND_QUEUE_URL"])
    return Container(
        orders=OrderService(orders, utc_now),
        returns=ReturnService(orders, returns, idempotency, audit, payments, utc_now),
        tickets=TicketService(tickets, orders, idempotency, audit, utc_now),
        queue=queue,
        clock=utc_now,
    )


def get_container() -> Container:
    global _container
    if _container is None:
        _container = build_from_env()
    return _container


def set_container(container: Container | None) -> None:
    """Test hook."""
    global _container
    _container = container
