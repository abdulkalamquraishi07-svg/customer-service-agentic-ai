"""Turn handling: session binding, input checks, limits, result shaping.

Framework-independent so it can be tested and reused by the AgentCore entrypoint.
"""

import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from strands import Agent

from csagent_agents.agents import build_supervisor, make_model
from csagent_agents.api_client import BusinessApiClient
from csagent_agents.config import AgentConfig
from csagent_agents.context import GROUNDING_SOURCE_CHARS, RequestContext, subject_from_token
from csagent_agents.guard import (
    BedrockGrounding,
    BedrockGuardrail,
    Checker,
    GroundingChecker,
    NoGrounding,
    NoGuardrail,
)
from csagent_agents.metrics import CloudWatchMetrics, Metrics, NoMetrics, turn_metrics
from csagent_agents.ratelimit import (
    DynamoRateLimiter,
    Limiter,
    Limits,
    NoLimiter,
    RateLimited,
)
from csagent_agents.service_errors import RequestRejected

__all__ = ["AgentService", "RequestRejected", "clean_reply"]

BLOCKED_INPUT_REPLY = (
    "I'm sorry, I can't help with that request. I can help with your orders, deliveries, "
    "returns and refunds, or connect you with a human representative."
)
BLOCKED_OUTPUT_REPLY = (
    "I'm sorry, I wasn't able to give you a proper answer to that. Would you like me to connect "
    "you with a human representative?"
)
FALLBACK_UNGROUNDED = (
    "I'm sorry, I couldn't confirm that from our records and policies, and I don't want to "
    "guess. Would you like me to connect you with a human representative who can check?"
)
EVIDENCE_TURNS = 3

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MAX_SESSIONS = 200
SESSION_TTL_SECONDS = 3600

FALLBACK_LIMIT = (
    "I'm sorry, this is taking longer than expected, so I'm stopping here rather than guessing. "
    "I can hand this to a human representative who can finish it. Would you like me to do that?"
)


_REASONING_BLOCK = re.compile(r"<(thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_REASONING = re.compile(r"<(thinking|reasoning)>.*\Z", re.DOTALL | re.IGNORECASE)
FALLBACK_EMPTY = (
    "I'm sorry, I couldn't put together an answer just now. "
    "Would you like me to connect you with a human representative?"
)


_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?\s*\)")
_BARE_URL = re.compile(r"(?:https?://|www\.)[^\s)\]>]+", re.IGNORECASE)
# Domains the assistant may link to. Empty by default: no tool or document supplies URLs, so
# any URL in a reply was invented by the model.
ALLOWED_LINK_DOMAINS: frozenset[str] = frozenset()


def _domain(url: str) -> str:
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    return url.split("/", 1)[0].split(":", 1)[0].lower().removeprefix("www.")


def _allowed(url: str) -> bool:
    domain = _domain(url)
    return any(domain == d or domain.endswith("." + d) for d in ALLOWED_LINK_DOMAINS)


def strip_untrusted_links(text: str) -> str:
    """Keep link text, drop URLs not on the allowlist (models invent plausible URLs)."""
    text = _MD_LINK.sub(lambda m: m.group(0) if _allowed(m.group(2)) else m.group(1), text)
    return _BARE_URL.sub(lambda m: m.group(0) if _allowed(m.group(0)) else "", text)


def clean_reply(text: str) -> str:
    """Make a model reply safe to show: no reasoning blocks, no invented links."""
    text = _REASONING_BLOCK.sub("", text)
    text = _UNCLOSED_REASONING.sub("", text)  # truncated block: drop everything after it
    text = strip_untrusted_links(text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)  # tidy spacing left by removed URLs
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or FALLBACK_EMPTY


_ACTION_CLAIM = re.compile(
    r"\b(?:i(?:'ve| have)\s+(?:escalated|created|opened|raised|logged|forwarded|passed)"
    r"|i(?:'ll| will)\s+(?:escalate|create|open|raise|log|forward|pass)"
    r"|(?:has|have)\s+been\s+escalated"
    r"|ticket\s+(?:id|number)\b)",
    re.IGNORECASE,
)
_ACTION_TOOLS = frozenset({"create_support_ticket", "escalate_to_human"})
FALLBACK_UNVERIFIED = (
    "I'm sorry, I haven't been able to open a support ticket for this yet. "
    "Would you like me to connect you with a human representative now?"
)


def unverified_action_claim(reply: str, trace: list[dict[str, Any]]) -> bool:
    """True if the reply claims an escalation/ticket that no tool in this turn performed."""
    if not _ACTION_CLAIM.search(reply):
        return False
    return not any(t.get("tool") in _ACTION_TOOLS and t.get("status") == "ok" for t in trace)


@dataclass
class _Session:
    customer_id: str
    supervisor: Agent
    last_used: float = field(default_factory=time.monotonic)
    # Tool results of the last few turns: follow-ups ("yes, go ahead") restate earlier facts.
    evidence: deque[str] = field(default_factory=lambda: deque(maxlen=EVIDENCE_TURNS))


def build_checker(cfg: AgentConfig) -> Checker:
    if cfg.guardrail_id and cfg.guardrail_version:
        client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.region,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=10),
        )
        return BedrockGuardrail(client, cfg.guardrail_id, cfg.guardrail_version)
    return NoGuardrail()


def _guardrail_turn(session_id: str, reply: str, reasons: list[str], stage: str) -> dict[str, Any]:
    return {
        "response": reply,
        "session_id": session_id,
        "request_id": str(uuid.uuid4()),
        "stop_reason": f"guardrail_{stage}",
        "escalation_suggested": stage == "output",
        "trace": [{"tool": f"guardrail:{stage}", "status": "blocked", "ms": 0, "reasons": reasons}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


# Our own canned replies state no facts, so they are not grounding-checked.
FIXED_REPLIES = frozenset(
    {
        FALLBACK_LIMIT,
        FALLBACK_EMPTY,
        FALLBACK_UNVERIFIED,
        FALLBACK_UNGROUNDED,
        BLOCKED_INPUT_REPLY,
        BLOCKED_OUTPUT_REPLY,
    }
)


def build_grounding(cfg: AgentConfig) -> GroundingChecker:
    if cfg.grounding_guardrail_id and cfg.grounding_guardrail_version:
        client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.region,
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=15),
        )
        return BedrockGrounding(
            client, cfg.grounding_guardrail_id, cfg.grounding_guardrail_version,
            cfg.grounding_threshold,
        )
    return NoGrounding()


def build_limiter(cfg: AgentConfig) -> Limiter:
    if not cfg.rate_limit_table:
        return NoLimiter()
    client = boto3.client(
        "dynamodb",
        region_name=cfg.region,
        config=Config(retries={"max_attempts": 3, "mode": "standard"}, read_timeout=5),
    )
    limits = Limits(
        per_minute=cfg.limit_per_minute,
        per_hour=cfg.limit_per_hour,
        per_day=cfg.limit_per_day,
        daily_tokens=cfg.limit_daily_tokens,
    )
    return DynamoRateLimiter(client, cfg.rate_limit_table, limits)


def build_metrics(cfg: AgentConfig) -> Metrics:
    if cfg.environment == "unknown":
        return NoMetrics()
    client = boto3.client(
        "cloudwatch",
        region_name=cfg.region,
        config=Config(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=3,
                      connect_timeout=2),
    )
    return CloudWatchMetrics(client, cfg.environment)


class AgentService:
    def __init__(
        self,
        cfg: AgentConfig,
        checker: Checker | None = None,
        limiter: Limiter | None = None,
        metrics: Metrics | None = None,
        grounding: GroundingChecker | None = None,
    ) -> None:
        self._cfg = cfg
        self._model = make_model(cfg)
        self._checker = checker if checker is not None else build_checker(cfg)
        self._grounding = grounding if grounding is not None else build_grounding(cfg)
        self._limiter = limiter if limiter is not None else build_limiter(cfg)
        self._metrics = metrics if metrics is not None else build_metrics(cfg)
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def _warm_knowledge_base(self) -> None:
        """Start resuming a scale-to-zero vector store while the first turn does other work.

        Observed in dev: a resume can take ~75 s. Fire-and-forget; errors are ignored because
        the real search has its own resume handling and honest failure path."""
        kb_id, region = self._cfg.knowledge_base_id, self._cfg.region
        if not kb_id:
            return

        def warm() -> None:
            try:
                client = boto3.client("bedrock-agent-runtime", region_name=region)
                client.retrieve(
                    knowledgeBaseId=kb_id,
                    retrievalQuery={"text": "warm-up"},
                    retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 1}},
                )
            except Exception:  # noqa: S110 - best effort by design
                pass

        threading.Thread(target=warm, name="kb-warmup", daemon=True).start()

    def _session_for(self, session_id: str, customer_id: str) -> _Session:
        now = time.monotonic()
        with self._lock:
            # Evict expired sessions, then enforce a hard cap (oldest first).
            for sid in [
                s for s, v in self._sessions.items() if now - v.last_used > SESSION_TTL_SECONDS
            ]:
                del self._sessions[sid]
            while len(self._sessions) >= MAX_SESSIONS:
                del self._sessions[min(self._sessions, key=lambda s: self._sessions[s].last_used)]

            session = self._sessions.get(session_id)
            if session is None:
                session = _Session(customer_id, build_supervisor(self._model))
                self._sessions[session_id] = session
                self._warm_knowledge_base()
            elif session.customer_id != customer_id:
                # A session belongs to exactly one customer; never share conversation state.
                raise RequestRejected("This session belongs to a different customer.")
            session.last_used = now
            return session

    def handle_turn(self, *, prompt: str, session_id: str, id_token: str) -> dict[str, Any]:
        """One customer turn, with operational metrics for every outcome."""
        started = time.monotonic()
        try:
            turn = self._run_turn(prompt=prompt, session_id=session_id, id_token=id_token)
        except RateLimited:
            self._metrics.emit([("RateLimited", 1, "Count")])
            raise
        except RequestRejected:
            self._metrics.emit([("Rejected", 1, "Count")])
            raise
        except Exception:
            self._metrics.emit([("TurnFailed", 1, "Count")])
            raise
        self._metrics.emit(turn_metrics(turn, (time.monotonic() - started) * 1000))
        return turn

    def _run_turn(self, *, prompt: str, session_id: str, id_token: str) -> dict[str, Any]:
        prompt = _CONTROL_CHARS.sub("", prompt or "").strip()
        if not prompt:
            raise RequestRejected("Message is empty.")
        if len(prompt) > self._cfg.max_prompt_chars:
            raise RequestRejected("Message is too long.")
        if not session_id or len(session_id) > 128:
            raise RequestRejected("A valid session id is required.")
        try:
            customer_id = subject_from_token(id_token)
        except ValueError as exc:
            raise RequestRejected("Authentication is required.") from exc

        # Rate limit BEFORE any paid work (guardrail, model). Fails closed.
        self._limiter.acquire(customer_id)

        # Guardrail on the customer's message. Fails closed (GuardrailUnavailable propagates).
        verdict = self._checker.check(prompt, "INPUT")
        if verdict.blocked:
            return _guardrail_turn(session_id, BLOCKED_INPUT_REPLY, verdict.reasons, "input")
        prompt = verdict.text  # PII such as card numbers is masked before any model sees it

        session = self._session_for(session_id, customer_id)
        api = BusinessApiClient(
            self._cfg.api_base_url, id_token, timeout=self._cfg.http_timeout_seconds
        )
        request = RequestContext(
            session_id=session_id,
            customer_id=customer_id,
            api=api,
            request_id=str(uuid.uuid4()),
            knowledge_base_id=self._cfg.knowledge_base_id,
            region=self._cfg.region,
            kb_score_mode=self._cfg.kb_score_mode,
            kb_min_similarity=self._cfg.kb_min_similarity,
        )
        try:
            result = session.supervisor(
                prompt,
                invocation_state={"request": request, "model": self._model},
                limits={
                    "turns": self._cfg.max_turns,
                    "total_tokens": self._cfg.max_total_tokens,
                },
            )
        finally:
            api.close()

        stop = str(result.stop_reason)
        hit_limit = stop.startswith("limit_")
        # Per-request usage including nested specialist agents (they share the request's
        # invocation_context). agent.metrics would be cumulative over the whole session.
        context = getattr(result, "invocation_context", None)
        usage = dict(context.usage) if context is not None else {}
        reply = FALLBACK_LIMIT if hit_limit else clean_reply(str(result))
        escalate = hit_limit
        if not hit_limit and unverified_action_claim(reply, request.trace):
            # Observed live: "I will escalate this" with no ticket created. Never let a
            # promised action reach the customer unless a tool actually performed it.
            request.trace.append({"tool": "claim_check", "status": "unverified", "ms": 0})
            reply, escalate = FALLBACK_UNVERIFIED, True
        # Hallucination check: the reply must be supported by what the tools returned (this and
        # recent turns). Observed live: an invented "we offer gift wrapping at checkout" after the
        # knowledge base returned nothing. Turns with no tool results state no checked facts.
        if request.evidence:
            session.evidence.append(request.grounding_source())
        if not hit_limit and request.evidence and reply not in FIXED_REPLIES:
            source = "\n".join(session.evidence)[-GROUNDING_SOURCE_CHARS:]
            grounding = self._grounding.check(source, prompt, reply)
            enforce = self._cfg.grounding_mode == "enforce"
            status = "ok" if grounding.grounded else ("blocked" if enforce else "shadow_blocked")
            request.trace.append(
                {"tool": "grounding_check", "status": status, "ms": 0, "score": grounding.score}
            )
            if not grounding.grounded and enforce:
                reply, escalate = FALLBACK_UNGROUNDED, True
        if not hit_limit:
            out = self._checker.check(reply, "OUTPUT")
            if out.blocked:
                request.trace.append(
                    {
                        "tool": "guardrail:output",
                        "status": "blocked",
                        "ms": 0,
                        "reasons": out.reasons,
                    }
                )
                reply, escalate = BLOCKED_OUTPUT_REPLY, True
            else:
                reply = out.text
        input_tokens, output_tokens = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        self._limiter.record_tokens(customer_id, input_tokens + output_tokens)
        return {
            "response": reply,
            "session_id": session_id,
            "request_id": request.request_id,
            "stop_reason": stop,
            "escalation_suggested": escalate,
            "trace": request.trace,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
