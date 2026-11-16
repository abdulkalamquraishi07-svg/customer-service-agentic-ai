"""Support tickets: create, escalate to a human, resolve."""

import hashlib
import uuid

from csagent_core.context import SUPPORT_AGENTS, CallerContext
from csagent_core.errors import Forbidden, NotFound, ValidationFailed
from csagent_core.models import Ticket, TicketStatus
from csagent_core.ports import (
    AuditLog,
    Clock,
    IdempotencyStore,
    OrderRepository,
    TicketRepository,
)

ALLOWED_CATEGORIES = frozenset({"complaint", "delivery", "refund", "technical", "other"})


class TicketService:
    def __init__(
        self,
        tickets: TicketRepository,
        orders: OrderRepository,
        idempotency: IdempotencyStore,
        audit: AuditLog,
        clock: Clock,
    ) -> None:
        self._tickets = tickets
        self._orders = orders
        self._idem = idempotency
        self._audit = audit
        self._clock = clock

    def create_ticket(
        self,
        ctx: CallerContext,
        category: str,
        subject: str,
        description: str,
        idempotency_key: str,
        related_order_id: str | None = None,
    ) -> Ticket:
        category = (category or "").lower()
        if category not in ALLOWED_CATEGORIES:
            raise ValidationFailed(f"category must be one of {sorted(ALLOWED_CATEGORIES)}")
        if not 1 <= len((subject or "").strip()) <= 200:
            raise ValidationFailed("subject must be 1-200 characters")
        if not 1 <= len((description or "").strip()) <= 4000:
            raise ValidationFailed("description must be 1-4000 characters")
        if not 8 <= len(idempotency_key or "") <= 128:
            raise ValidationFailed("idempotency_key must be 8-128 characters")
        if related_order_id and self._orders.get(ctx.customer_id, related_order_id) is None:
            raise NotFound("order not found")

        now = self._clock()
        key = f"{ctx.customer_id}#ticket#{idempotency_key}"
        payload = f"{category}|{subject}|{description}|{related_order_id}"
        request_hash = hashlib.sha256(payload.encode()).hexdigest()

        prior = self._idem.begin(key, request_hash, now)
        if prior is not None:
            return self.get_ticket(ctx, prior)

        try:
            ticket = Ticket(
                customer_id=ctx.customer_id,
                ticket_id=f"TKT-{uuid.uuid4().hex[:12].upper()}",
                status=TicketStatus.OPEN,
                category=category,
                subject=subject.strip(),
                description=description.strip(),
                related_order_id=related_order_id,
                created_at=now,
                updated_at=now,
            )
            self._tickets.create(ticket)
            self._idem.complete(key, ticket.ticket_id, now)
        except Exception:
            self._idem.release(key)
            raise

        self._audit.append(
            ticket.ticket_id, "ticket.created", ctx.subject, {"request_id": ctx.request_id}
        )
        return ticket

    def get_ticket(self, ctx: CallerContext, ticket_id: str) -> Ticket:
        ticket = self._tickets.get(ctx.customer_id, ticket_id)
        if ticket is None:
            raise NotFound("ticket not found")
        return ticket

    def escalate(self, ctx: CallerContext, ticket_id: str, summary: str) -> Ticket:
        """Hand the case to a human with a summary of conversation and tool evidence."""
        summary = (summary or "").strip()
        if not 1 <= len(summary) <= 4000:
            raise ValidationFailed("summary must be 1-4000 characters")
        ticket = self.get_ticket(ctx, ticket_id)
        updated = self._tickets.update(
            ticket.model_copy(
                update={
                    "status": TicketStatus.ESCALATED,
                    "escalation_summary": summary,
                    "updated_at": self._clock(),
                }
            ),
            expected_version=ticket.version,
        )
        self._audit.append(
            ticket_id, "ticket.escalated", ctx.subject, {"request_id": ctx.request_id}
        )
        return updated

    def list_queue(self, ctx: CallerContext, status: TicketStatus) -> list[Ticket]:
        """Staff work queue (oldest first). Customers list their own tickets elsewhere."""
        if not ctx.has_group(SUPPORT_AGENTS):
            raise Forbidden("support agent role required")
        return sorted(self._tickets.list_by_status(status), key=lambda t: t.created_at)

    def resolve(self, ctx: CallerContext, customer_id: str, ticket_id: str) -> Ticket:
        if not ctx.has_group(SUPPORT_AGENTS):
            raise Forbidden("support agent role required")
        ticket = self._tickets.get(customer_id, ticket_id)
        if ticket is None:
            raise NotFound("ticket not found")
        updated = self._tickets.update(
            ticket.model_copy(
                update={"status": TicketStatus.RESOLVED, "updated_at": self._clock()}
            ),
            expected_version=ticket.version,
        )
        self._audit.append(ticket_id, "ticket.resolved", ctx.subject, {})
        return updated
