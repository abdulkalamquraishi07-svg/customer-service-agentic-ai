"""Bedrock Guardrails at the trust boundary (ApplyGuardrail API).

Checked once on the customer's message (INPUT) and once on the final reply (OUTPUT), not on every
internal model call: internal calls carry our own order data and policy text, which would cost
~10x more text units and invite false positives. See ADR 0005.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Source = Literal["INPUT", "OUTPUT"]


class GuardrailUnavailable(Exception):
    """ApplyGuardrail failed. Callers fail closed: no unchecked text crosses the boundary."""


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    text: str  # original text, or the masked version when PII was anonymized
    reasons: list[str] = field(default_factory=list)  # policy types only, never the matched text
    masked: bool = False


class Checker(Protocol):
    def check(self, text: str, source: Source) -> Verdict: ...


def _actions(node: Any, out: list[tuple[str, str]]) -> None:
    """Collect (action, type) pairs from any depth of an assessment."""
    if isinstance(node, dict):
        if "action" in node and isinstance(node["action"], str):
            out.append((node["action"], str(node.get("type") or node.get("name") or "unknown")))
        for value in node.values():
            _actions(value, out)
    elif isinstance(node, list):
        for item in node:
            _actions(item, out)


def parse_response(text: str, response: dict[str, Any]) -> Verdict:
    if response.get("action") != "GUARDRAIL_INTERVENED":
        return Verdict(blocked=False, text=text)
    pairs: list[tuple[str, str]] = []
    _actions(response.get("assessments", []), pairs)
    blocked = sorted({kind for action, kind in pairs if action == "BLOCKED"})
    if blocked:
        return Verdict(blocked=True, text="", reasons=blocked)
    outputs = response.get("outputs") or []
    masked_text = "".join(o.get("text", "") for o in outputs) or text
    anonymized = sorted({kind for action, kind in pairs if action == "ANONYMIZED"})
    return Verdict(blocked=False, text=masked_text, reasons=anonymized, masked=bool(anonymized))


class BedrockGuardrail:
    def __init__(self, client: Any, guardrail_id: str, version: str) -> None:
        self._client = client
        self._id = guardrail_id
        self._version = version

    def check(self, text: str, source: Source) -> Verdict:
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._id,
                guardrailVersion=self._version,
                source=source,
                content=[{"text": {"text": text}}],
            )
        except Exception as exc:
            raise GuardrailUnavailable(type(exc).__name__) from exc
        return parse_response(text, response)


class NoGuardrail:
    """Used only when no guardrail is configured (local development)."""

    def check(self, text: str, source: Source) -> Verdict:
        return Verdict(blocked=False, text=text)


# ---------------- contextual grounding (hallucination check) ----------------

MAX_SOURCE_CHARS = 100_000  # ApplyGuardrail contextual grounding limits (AWS docs)
MAX_QUERY_CHARS = 1_000
MAX_RESPONSE_CHARS = 5_000


@dataclass(frozen=True)
class GroundingVerdict:
    score: float | None  # None: the service returned no grounding score
    grounded: bool


class GroundingChecker(Protocol):
    def check(self, source: str, query: str, response: str) -> GroundingVerdict: ...


def grounding_score(response: dict[str, Any]) -> float | None:
    for assessment in response.get("assessments", []):
        for f in assessment.get("contextualGroundingPolicy", {}).get("filters", []):
            if f.get("type") == "GROUNDING" and "score" in f:
                return float(f["score"])
    return None


class BedrockGrounding:
    """Scores how well a reply is supported by this turn's tool results (Bedrock Guardrails
    contextual grounding). The block decision is made here, from a calibrated threshold."""

    def __init__(self, client: Any, guardrail_id: str, version: str, threshold: float) -> None:
        if not 0 < threshold < 1:
            raise ValueError("grounding threshold must be between 0 and 1")
        self._client, self._id, self._version = client, guardrail_id, version
        self.threshold = threshold

    def check(self, source: str, query: str, response: str) -> GroundingVerdict:
        content = [
            {"text": {"text": source[-MAX_SOURCE_CHARS:], "qualifiers": ["grounding_source"]}},
            {"text": {"text": query[:MAX_QUERY_CHARS], "qualifiers": ["query"]}},
            {"text": {"text": response[:MAX_RESPONSE_CHARS]}},
        ]
        try:
            result = self._client.apply_guardrail(
                guardrailIdentifier=self._id,
                guardrailVersion=self._version,
                source="OUTPUT",
                content=content,
            )
        except Exception as exc:
            raise GuardrailUnavailable(type(exc).__name__) from exc
        score = grounding_score(result)
        # No score means the check did not run: fail closed, like every other guardrail here.
        if score is None:
            raise GuardrailUnavailable("no grounding score returned")
        return GroundingVerdict(score=score, grounded=score >= self.threshold)


class NoGrounding:
    """Local development only (no grounding guardrail configured)."""

    def check(self, source: str, query: str, response: str) -> GroundingVerdict:
        return GroundingVerdict(score=None, grounded=True)
