"""Tools the specialist agents may call.

Rules enforced here:
- No tool accepts a customer id. Identity comes from `RequestContext` (invocation_state).
- Tools return JSON strings with backend-verified data, or {"error": ...} the agent must relay
  honestly (never guess around a failure).
- Write tools use deterministic idempotency keys.
"""

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.config import Config
from strands import tool
from strands.types.tools import ToolContext

from csagent_agents.api_client import ToolFailure
from csagent_agents.context import RequestContext

logger = logging.getLogger("csagent.agent")


def _request(tool_context: ToolContext) -> RequestContext:
    request = tool_context.invocation_state.get("request")
    if not isinstance(request, RequestContext):
        raise ToolFailure("No authenticated customer context is available.")
    return request


def _run(
    tool_context: ToolContext, name: str, fn: Callable[[RequestContext], Any], **trace_extra: Any
) -> str:
    """Execute a tool body, record a PII-free trace entry, and return a JSON string."""
    started = time.monotonic()
    try:
        request = _request(tool_context)
        result = fn(request)
        request.record(name, "ok", started, **trace_extra)
        text = json.dumps(result, default=str)
        request.add_evidence(name, text)  # what the final reply may be grounded in
        return text
    except ToolFailure as exc:
        current = tool_context.invocation_state.get("request")
        text = json.dumps({"error": str(exc), "retryable": exc.retryable})
        if isinstance(current, RequestContext):
            current.record(name, "error", started, retryable=exc.retryable, **trace_extra)
            current.add_evidence(name, text)
        return text


# ---------------- order tools ----------------


@tool(context=True)
def list_my_orders(tool_context: ToolContext) -> str:
    """List the signed-in customer's orders (newest first) with shipment status."""
    return _run(tool_context, "list_my_orders", lambda r: r.api.list_orders())


@tool(context=True)
def get_order_details(order_id: str, tool_context: ToolContext) -> str:
    """Get full details of one of the customer's orders.

    Args:
        order_id: The order id, e.g. ORD-1001.
    """
    return _run(tool_context, "get_order_details", lambda r: r.api.get_order(order_id))


@tool(context=True)
def get_shipment_status(order_id: str, tool_context: ToolContext) -> str:
    """Get verified shipment/delivery status, including whether the delivery is overdue.

    Args:
        order_id: The order id, e.g. ORD-1001.
    """
    return _run(tool_context, "get_shipment_status", lambda r: r.api.get_shipment(order_id))


# ---------------- returns tools ----------------


@tool(context=True)
def check_return_eligibility(order_id: str, tool_context: ToolContext) -> str:
    """Check, using company rules, whether an order can be returned and the refund amount.

    Args:
        order_id: The order id, e.g. ORD-1001.
    """
    return _run(
        tool_context, "check_return_eligibility", lambda r: r.api.return_eligibility(order_id)
    )


@tool(context=True)
def request_return(order_id: str, reason: str, tool_context: ToolContext) -> str:
    """Submit a return/refund request. Only call after the customer clearly asked for it and
    check_return_eligibility said the order is eligible. Large refunds need human approval; the
    result says whether the request is APPROVED or PENDING_APPROVAL.

    Args:
        order_id: The order id, e.g. ORD-1001.
        reason: The customer's reason for the return, in their own words (max 1000 chars).
    """
    reason = (reason or "").strip()[:1000]
    return _run(
        tool_context,
        "request_return",
        lambda r: r.api.create_return(
            order_id, reason, r.idempotency_key("request_return", order_id, reason)
        ),
    )


@tool(context=True)
def get_return_status(return_id: str, tool_context: ToolContext) -> str:
    """Get the current status of a return request.

    Args:
        return_id: The return id, e.g. RET-AB12CD34EF56.
    """
    return _run(tool_context, "get_return_status", lambda r: r.api.get_return(return_id))


# ---------------- support tools ----------------


@tool(context=True)
def create_support_ticket(
    category: str,
    subject: str,
    description: str,
    tool_context: ToolContext,
    related_order_id: str | None = None,
) -> str:
    """Create a support ticket for a complaint or an issue that needs follow-up.

    Args:
        category: One of: complaint, delivery, refund, technical, other.
        subject: Short title (max 200 chars).
        description: What happened, with the facts gathered so far (max 4000 chars).
        related_order_id: Optional order id the ticket is about.
    """
    body: dict[str, Any] = {
        "category": (category or "").strip().lower(),
        "subject": (subject or "").strip()[:200],
        "description": (description or "").strip()[:4000],
    }
    if related_order_id:
        body["related_order_id"] = related_order_id.strip()
    def create(r: RequestContext) -> dict[str, Any]:
        ticket = r.api.create_ticket(
            body,
            r.idempotency_key(
                "create_ticket", body["category"], body["subject"], body.get("related_order_id", "")
            ),
        )
        if ticket.get("ticket_id"):
            r.created_tickets.append(str(ticket["ticket_id"]))
        return ticket

    return _run(tool_context, "create_support_ticket", create)


def resolve_ticket_id(requested: str, created_this_turn: list[str]) -> str:
    """The ticket to escalate. Models mis-copy ids (observed live: a 404 left a customer
    un-escalated), so a ticket created in this same turn wins over the id the model typed.
    Otherwise the typed id is used and the API enforces ownership."""
    requested = (requested or "").strip()
    if created_this_turn and requested not in created_this_turn:
        return created_this_turn[-1]
    return requested


@tool(context=True)
def escalate_to_human(ticket_id: str, summary: str, tool_context: ToolContext) -> str:
    """Hand a ticket to a human representative. Use when the customer asks for a human, the
    case needs an exception, evidence is missing or conflicting, or a system keeps failing.
    The summary must contain the facts verified so far so the customer need not repeat them.

    Args:
        ticket_id: The id of an existing ticket (create one first if needed).
        summary: Concise handoff summary (max 4000 chars).
    """
    return _run(
        tool_context,
        "escalate_to_human",
        lambda r: r.api.escalate_ticket(
            resolve_ticket_id(ticket_id, r.created_tickets), (summary or "").strip()[:4000]
        ),
    )


# ---------------- knowledge tool ----------------


# A paused Aurora Serverless v2 instance took 75 s to resume in dev (RDS events, 2026-11-14).
RESUME_WAIT_SECONDS = 90.0


def to_similarity(score: float, score_mode: str) -> float:
    """Normalize a Bedrock KB score to cosine similarity (higher = more relevant)."""
    if score_mode == "distance":
        return 1.0 - score
    if score_mode == "similarity":
        return score
    raise ValueError(f"unknown kb score mode: {score_mode}")


def rank_results(
    raw: list[dict[str, Any]], *, score_mode: str, min_similarity: float
) -> list[dict[str, Any]]:
    """Sort by similarity (best first) and drop passages below the calibrated cutoff."""
    ranked = []
    for item in raw:
        similarity = to_similarity(float(item.get("score", 0.0)), score_mode)
        if similarity < min_similarity:
            continue
        location = item.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        ranked.append(
            {
                "source": location.rsplit("/", 1)[-1],
                "relevance": round(similarity, 3),
                "text": item.get("content", {}).get("text", "")[:1500],
            }
        )
    return sorted(ranked, key=lambda r: r["relevance"], reverse=True)


def _retrieve_with_resume(
    client: Any, *, sleep: Callable[[float], None] = time.sleep, **kwargs: Any
) -> dict[str, Any]:
    """Call KB retrieve, waiting while a scale-to-zero vector store resumes.

    Other failures surface immediately as an honest, retryable ToolFailure."""
    deadline = time.monotonic() + RESUME_WAIT_SECONDS
    delay = 2.0
    while True:
        try:
            return dict(client.retrieve(**kwargs))
        except Exception as exc:
            resuming = "resum" in str(exc).lower()
            if resuming and time.monotonic() + delay < deadline:
                sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            # Operators need the real cause; the model and customer get a safe message.
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            logger.warning(
                json.dumps(
                    {
                        "event": "kb_retrieve_failed",
                        "error_type": type(exc).__name__,
                        "error_code": code,
                        "detail": str(exc)[:300],
                        "resuming": resuming,
                    }
                )
            )
            raise ToolFailure(
                "The policy search is temporarily unavailable.", retryable=True
            ) from exc


@tool(context=True)
def search_company_policies(query: str, tool_context: ToolContext) -> str:
    """Search official company policies, FAQs and support documentation.

    Returns passages with their source. Only state policy facts that appear in the results.

    Args:
        query: What to look up, e.g. "return window for electronics".
    """

    def search(request: RequestContext) -> dict[str, Any]:
        if not request.knowledge_base_id:
            raise ToolFailure("The policy knowledge base is not configured.")
        client = boto3.client(
            "bedrock-agent-runtime",
            region_name=request.region,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10),
        )
        resp = _retrieve_with_resume(
            client,
            knowledgeBaseId=request.knowledge_base_id,
            retrievalQuery={"text": (query or "").strip()[:500]},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 4,
                    # Hybrid scores are rank-fused (~0.5 for everything) and cannot be
                    # thresholded; semantic scores are calibrated (ADR 0004).
                    "overrideSearchType": "SEMANTIC",
                }
            },
        )
        results = rank_results(
            resp.get("retrievalResults", []),
            score_mode=request.kb_score_mode,
            min_similarity=request.kb_min_similarity,
        )
        if not results:
            return {"results": [], "note": "No relevant policy found. Do not guess; escalate."}
        return {"results": results}

    return _run(tool_context, "search_company_policies", search)


ORDER_TOOLS = [list_my_orders, get_order_details, get_shipment_status]
# list_my_orders (read-only) lets the specialist resolve "my headphones" to an order itself.
# Without it, it asked customers for order ids or guessed one (observed in evals).
RETURNS_TOOLS = [
    list_my_orders,
    check_return_eligibility,
    request_return,
    get_return_status,
    get_order_details,
]
SUPPORT_TOOLS = [create_support_ticket, escalate_to_human]
KNOWLEDGE_TOOLS = [search_company_policies]
