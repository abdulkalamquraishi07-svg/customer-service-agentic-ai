"""Ops console API for human reps. Authorization is enforced in the domain services
(approvers / support-agents groups), not just at the edge."""

import logging
from typing import Any

from bizapi.common import dispatch, log, parse_body, path_param
from bizapi.container import get_container
from csagent_core.context import CallerContext
from csagent_core.errors import ValidationFailed
from csagent_core.models import TicketStatus
from pydantic import BaseModel, ConfigDict, Field


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    note: str = Field(default="", max_length=500)


def list_approvals(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    records = get_container().returns.list_awaiting_approval(ctx)
    return 200, {"approvals": [r.model_dump(mode="json", exclude={"version"}) for r in records]}


def decide_return(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    c = get_container()
    customer_id = path_param(event, "customer_id")
    return_id = path_param(event, "return_id")
    body: DecisionBody = parse_body(event, DecisionBody)
    record = c.returns.decide(ctx, customer_id, return_id, body.approve, body.note)
    if body.approve:
        try:
            c.queue.send(record.customer_id, record.return_id)
        except Exception:
            log(logging.WARNING, "enqueue_failed", return_id=record.return_id)
    return 200, record.model_dump(mode="json", exclude={"version"})


def list_tickets(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    raw = ((event.get("queryStringParameters") or {}).get("status") or "ESCALATED").upper()
    try:
        status = TicketStatus(raw)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in TicketStatus)
        raise ValidationFailed(f"status must be one of {allowed}") from exc
    tickets = get_container().tickets.list_queue(ctx, status)
    return 200, {"tickets": [t.model_dump(mode="json", exclude={"version"}) for t in tickets]}


def resolve_ticket(ctx: CallerContext, event: dict[str, Any]) -> tuple[int, Any]:
    ticket = get_container().tickets.resolve(
        ctx, path_param(event, "customer_id"), path_param(event, "ticket_id")
    )
    return 200, ticket.model_dump(mode="json", exclude={"version"})


ROUTES = {
    ("GET", "/v1/ops/approvals"): list_approvals,
    ("GET", "/v1/ops/tickets"): list_tickets,
    ("POST", "/v1/ops/returns/{customer_id}/{return_id}/decision"): decide_return,
    ("POST", "/v1/ops/tickets/{customer_id}/{ticket_id}/resolve"): resolve_ticket,
}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return dispatch(event, ROUTES)
