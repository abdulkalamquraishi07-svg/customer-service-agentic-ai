"""Per-request context passed to tools out of band (Strands `invocation_state`).

Identity lives here, never in the prompt or in any tool parameter the model can set.
"""

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from csagent_agents.api_client import BusinessApiClient

EVIDENCE_ITEM_CHARS = 6000
# ApplyGuardrail contextual grounding accepts a large source; we keep a conservative cap.
GROUNDING_SOURCE_CHARS = 50_000


def subject_from_token(token: str) -> str:
    """Read `sub` from a JWT payload WITHOUT verifying it.

    Used only to bind a session to one customer. Signature verification happens at API Gateway
    (Cognito authorizer) on every tool call, so a forged token cannot read or change any data.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        sub = claims["sub"]
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid token") from exc
    if not isinstance(sub, str) or not sub:
        raise ValueError("invalid token")
    return sub


@dataclass
class RequestContext:
    session_id: str
    customer_id: str
    api: BusinessApiClient
    request_id: str = "unknown"
    trace: list[dict[str, Any]] = field(default_factory=list)
    knowledge_base_id: str | None = None
    region: str = "us-east-1"
    kb_score_mode: str = "distance"
    kb_min_similarity: float = 0.15
    # Tickets created by tools in THIS turn (ids come from the API, never from the model).
    created_tickets: list[str] = field(default_factory=list)
    # Tool results of THIS turn (in memory only), used by the grounding check.
    evidence: list[str] = field(default_factory=list)

    def idempotency_key(self, action: str, *parts: str) -> str:
        """Deterministic: the same action+arguments in a session replays instead of repeating."""
        raw = "|".join([self.session_id, self.customer_id, action, *parts])
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    def add_evidence(self, tool: str, text: str) -> None:
        """Keep tool results for this turn (in memory only, never logged): the grounding check
        verifies the final reply against them."""
        self.evidence.append(f"[{tool}] {text[:EVIDENCE_ITEM_CHARS]}")

    def grounding_source(self) -> str:
        """All tool results of this turn, newest last, within the service's size limit."""
        text = "\n".join(self.evidence)
        return text[-GROUNDING_SOURCE_CHARS:]

    def record(self, tool: str, status: str, started: float, **extra: Any) -> None:
        self.trace.append(
            {
                "tool": tool,
                "status": status,
                "ms": int((time.monotonic() - started) * 1000),
                **extra,
            }
        )
