"""AgentCore Runtime invocation handling (framework glue kept thin and testable).

Identity: AgentCore's Cognito JWT authorizer validates the bearer token BEFORE this code runs and
forwards the Authorization header (allowlisted). The same token is then used for every business
API call, where API Gateway validates it again. The model never sees or chooses identity.
"""

import hashlib
import json
import logging
import sys
import time
from collections.abc import Mapping
from typing import Any, Protocol

from csagent_agents.ratelimit import RateLimited
from csagent_agents.service_errors import RequestRejected

logger = logging.getLogger("csagent.agent")


def configure_logging() -> None:
    """Send `csagent.*` INFO logs to stdout (-> CloudWatch). Without a handler, Python's
    last-resort handler drops everything below WARNING: turn logs were silently lost."""
    root = logging.getLogger("csagent")
    if not any(getattr(h, "_csagent", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        handler._csagent = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = False

GENERIC_FAILURE = (
    "I'm sorry, something went wrong on our side. Please try again in a moment, or ask me to "
    "connect you with a human representative."
)


class TurnHandler(Protocol):
    def handle_turn(self, *, prompt: str, session_id: str, id_token: str) -> dict[str, Any]: ...


def bearer_token(headers: Mapping[str, str]) -> str | None:
    for name, value in headers.items():
        if name.lower() == "authorization" and value:
            scheme, _, token = value.strip().partition(" ")
            return token.strip() if scheme.lower() == "bearer" and token else value.strip()
    return None


def _fingerprint(value: str) -> str:
    """Correlate logs per session without logging the raw id."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def handle_invocation(
    payload: Any,
    session_id: str | None,
    headers: Mapping[str, str],
    service: TurnHandler,
) -> dict[str, Any]:
    started = time.monotonic()
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    token = bearer_token(headers)
    if not isinstance(prompt, str):
        return {"error": {"code": "invalid_request", "message": "'prompt' must be a string."}}
    if not token:
        return {"error": {"code": "unauthenticated", "message": "Authentication is required."}}
    if not session_id:
        return {"error": {"code": "invalid_request", "message": "A session id is required."}}

    try:
        turn = service.handle_turn(prompt=prompt, session_id=session_id, id_token=token)
    except RateLimited as exc:
        logger.info(json.dumps({"event": "turn_rate_limited", "scope": exc.scope}))
        return {
            "error": {
                "code": "rate_limited",
                "message": str(exc),
                "retry_after_seconds": exc.retry_after,
            }
        }
    except RequestRejected as exc:
        logger.info(json.dumps({"event": "turn_rejected", "reason": str(exc)}))
        return {"error": {"code": "rejected", "message": str(exc)}}
    except Exception:
        logger.exception("turn_failed session=%s", _fingerprint(session_id))
        return {"error": {"code": "internal_error", "message": GENERIC_FAILURE}}

    # Operational record: no prompt/response text (PII), only structure, cost and latency.
    logger.info(
        json.dumps(
            {
                "event": "turn_completed",
                "request_id": turn["request_id"],
                "session": _fingerprint(session_id),
                "stop_reason": turn["stop_reason"],
                "escalation_suggested": turn["escalation_suggested"],
                "tools": [
                    [t["tool"], t["status"], t.get("ms")]
                    + ([round(t["score"], 3)] if t.get("score") is not None else [])
                    for t in turn["trace"]
                ],
                "usage": turn["usage"],
                "latency_ms": int((time.monotonic() - started) * 1000),
            },
            default=str,
        )
    )
    return {
        "response": turn["response"],
        "session_id": session_id,
        "request_id": turn["request_id"],
        "escalation_suggested": turn["escalation_suggested"],
    }
