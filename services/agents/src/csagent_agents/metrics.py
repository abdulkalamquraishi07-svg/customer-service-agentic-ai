"""Operational metrics (CloudWatch). No customer identifiers or text: only counts and timings,
dimensioned by environment, so cardinality and privacy exposure stay minimal."""

import logging
from typing import Any, Protocol

logger = logging.getLogger("csagent.agent")

NAMESPACE = "CSAgent"

Metric = tuple[str, float, str]  # (name, value, unit)


class Metrics(Protocol):
    def emit(self, metrics: list[Metric]) -> None: ...


class NoMetrics:
    def emit(self, metrics: list[Metric]) -> None:
        return None


class CloudWatchMetrics:
    def __init__(self, client: Any, environment: str, namespace: str = NAMESPACE) -> None:
        self._client = client
        self._env = environment
        self._namespace = namespace

    def emit(self, metrics: list[Metric]) -> None:
        """Best effort: observability must never break a customer's turn."""
        if not metrics:
            return
        data = [
            {
                "MetricName": name,
                "Value": float(value),
                "Unit": unit,
                "Dimensions": [{"Name": "Environment", "Value": self._env}],
            }
            for name, value, unit in metrics
        ]
        try:
            self._client.put_metric_data(Namespace=self._namespace, MetricData=data)
        except Exception as exc:
            logger.warning("metrics_emit_failed %s", type(exc).__name__)


def _grounding(trace: list[dict[str, Any]], status: str) -> int:
    return 1 if any(t.get("tool") == "grounding_check" and t.get("status") == status
                    for t in trace) else 0


def turn_metrics(turn: dict[str, Any], latency_ms: float) -> list[Metric]:
    """Metrics for one completed turn (including guardrail-blocked ones)."""
    trace = turn.get("trace", [])
    stop = str(turn.get("stop_reason", ""))
    usage = turn.get("usage", {})
    return [
        ("Turns", 1, "Count"),
        ("LatencyMs", latency_ms, "Milliseconds"),
        ("InputTokens", usage.get("input_tokens", 0), "Count"),
        ("OutputTokens", usage.get("output_tokens", 0), "Count"),
        ("ToolErrors", sum(1 for t in trace if t.get("status") == "error"), "Count"),
        ("GuardrailInputBlocked", 1 if stop == "guardrail_input" else 0, "Count"),
        (
            "GuardrailOutputBlocked",
            1 if any(t.get("tool") == "guardrail:output" for t in trace) else 0,
            "Count",
        ),
        ("GroundingBlocked", _grounding(trace, "blocked"), "Count"),
        ("GroundingShadowBlocked", _grounding(trace, "shadow_blocked"), "Count"),
        ("GroundingChecked", 1 if any(t.get("tool") == "grounding_check" for t in trace) else 0,
         "Count"),
        # Score distribution for review and re-calibration (dashboard shows p10 / p50).
        *[
            ("GroundingScore", float(t["score"]), "None")
            for t in trace
            if t.get("tool") == "grounding_check" and t.get("score") is not None
        ],
        (
            "ClaimCheckTriggered",
            1 if any(t.get("tool") == "claim_check" for t in trace) else 0,
            "Count",
        ),
        ("EscalationSuggested", 1 if turn.get("escalation_suggested") else 0, "Count"),
        ("LimitHit", 1 if stop.startswith("limit_") else 0, "Count"),
    ]
