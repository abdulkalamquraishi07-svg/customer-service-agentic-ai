"""Customer-facing API (API Gateway REST + Cognito). Identity = verified JWT claims only."""

import logging
from typing import Any

from bizapi.common import (
    dispatch,
    log,
    parse_body,
    path_param,
    require_idempotency_key,
)
from bizapi.container import get_container
from csagent_core.context import CallerContext
from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReturnBody(_Body):
    order_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)


class TicketBody(_Body):
    category: str = Field(min_length=1, max_length=32)
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=4000)
    related_order_id: str | None = Field(default=None, max_length=64)


class EscalateBody(_Body):
    summary: str = Field(min_length=1, max_length=4000)


_HIDDEN = {"version", "customer_id"}


def list_orders(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    return 200, {"orders": get_container().orders.list_orders(ctx)}


def get_order(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    order = get_container().orders.get_order(ctx, path_param(event, "order_id"))
    return 200, order.model_dump(mode="json", exclude=_HIDDEN)


def get_shipment(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    view = get_container().orders.get_shipment_status(ctx, path_param(event, "order_id"))
    return 200, view


def return_eligibility(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    return 200, get_container().returns.check_eligibility(ctx, path_param(event, "order_id"))


def create_return(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    c = get_container()
    body: ReturnBody = parse_body(event, ReturnBody)
    record = c.returns.request_return(
        ctx, body.order_id, body.reason, require_idempotency_key(event)
    )
    if record.status.value == "APPROVED":
        # Best effort: if this fails, the scheduled reconciler re-enqueues the execution.
        try:
            c.queue.send(record.customer_id, record.return_id)
        except Exception:
            log(logging.WARNING, "enqueue_failed", return_id=record.return_id)
    return 201, record.model_dump(mode="json", exclude=_HIDDEN)


def get_return(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    record = get_container().returns.get_return(ctx, path_param(event, "return_id"))
    return 200, record.model_dump(mode="json", exclude=_HIDDEN)


def create_ticket(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    body: TicketBody = parse_body(event, TicketBody)
    ticket = get_container().tickets.create_ticket(
        ctx,
        body.category,
        body.subject,
        body.description,
        require_idempotency_key(event),
        related_order_id=body.related_order_id,
    )
    return 201, ticket.model_dump(mode="json", exclude=_HIDDEN)


def escalate_ticket(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    body: EscalateBody = parse_body(event, EscalateBody)
    ticket = get_container().tickets.escalate(
        ctx, path_param(event, "ticket_id"), body.summary
    )
    return 200, ticket.model_dump(mode="json", exclude=_HIDDEN)


ROUTES = {
    ("GET", "/v1/orders"): list_orders,
    ("GET", "/v1/orders/{order_id}"): get_order,
    ("GET", "/v1/orders/{order_id}/shipment"): get_shipment,
    ("GET", "/v1/orders/{order_id}/return-eligibility"): return_eligibility,
    ("POST", "/v1/returns"): create_return,
    ("GET", "/v1/returns/{return_id}"): get_return,
    ("POST", "/v1/tickets"): create_ticket,
    ("POST", "/v1/tickets/{ticket_id}/escalate"): escalate_ticket,
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return dispatch(event, ROUTES)
