"""Runtime configuration, read from environment variables."""

import os
from dataclasses import dataclass

# Chosen by live comparison in dev (see docs/adr/0003-model-selection.md). Override with MODEL_ID.
DEFAULT_MODEL_ID = "us.amazon.nova-pro-v1:0"


@dataclass(frozen=True)
class AgentConfig:
    api_base_url: str
    model_id: str = DEFAULT_MODEL_ID
    region: str = "us-east-1"
    knowledge_base_id: str | None = None
    # With the Aurora/pgvector store, Bedrock returns cosine DISTANCE as `score` (lower is
    # better). Verified live; guarded by evals/kb_canary.py. See ADR 0004.
    kb_score_mode: str = "distance"
    # Calibrated on labeled queries (on-topic >= 0.114, off-topic <= 0.093); safe-side cutoff.
    kb_min_similarity: float = 0.15
    guardrail_id: str | None = None
    guardrail_version: str | None = None
    # Hallucination check: replies must be supported by the tool results of recent turns.
    grounding_guardrail_id: str | None = None
    grounding_guardrail_version: str | None = None
    grounding_threshold: float = 0.5  # calibrated by evals/grounding_canary.py
    # "shadow": score and record, never block (new check, threshold not yet calibrated).
    # "enforce": replace ungrounded replies with an honest fallback.
    grounding_mode: str = "shadow"
    # Governance: hard caps for ONE customer request, including all nested agents.
    max_turns: int = 14
    max_total_tokens: int = 80_000
    # API client behavior
    http_timeout_seconds: float = 10.0
    max_prompt_chars: int = 2000
    # Per-customer abuse / spend protection (the browser calls this runtime directly).
    # No table configured = no limiting: local development only.
    rate_limit_table: str | None = None
    limit_per_minute: int = 6
    limit_per_hour: int = 60
    limit_per_day: int = 250
    limit_daily_tokens: int = 600_000
    environment: str = "unknown"

    @classmethod
    def from_env(cls) -> "AgentConfig":
        env = os.environ
        return cls(
            api_base_url=env["API_BASE_URL"].rstrip("/") + "/",
            model_id=env.get("MODEL_ID", DEFAULT_MODEL_ID),
            region=env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", "us-east-1")),
            knowledge_base_id=env.get("KNOWLEDGE_BASE_ID") or None,
            kb_score_mode=env.get("KB_SCORE_MODE", "distance"),
            kb_min_similarity=float(env.get("KB_MIN_SIMILARITY", "0.15")),
            guardrail_id=env.get("GUARDRAIL_ID") or None,
            guardrail_version=env.get("GUARDRAIL_VERSION") or None,
            grounding_guardrail_id=env.get("GROUNDING_GUARDRAIL_ID") or None,
            grounding_guardrail_version=env.get("GROUNDING_GUARDRAIL_VERSION") or None,
            grounding_threshold=float(env.get("GROUNDING_THRESHOLD", "0.5")),
            grounding_mode=env.get("GROUNDING_MODE", "shadow"),
            max_turns=int(env.get("MAX_TURNS", "14")),
            max_total_tokens=int(env.get("MAX_TOTAL_TOKENS", "80000")),
            rate_limit_table=env.get("RATE_LIMIT_TABLE") or None,
            limit_per_minute=int(env.get("LIMIT_PER_MINUTE", "6")),
            limit_per_hour=int(env.get("LIMIT_PER_HOUR", "60")),
            limit_per_day=int(env.get("LIMIT_PER_DAY", "250")),
            limit_daily_tokens=int(env.get("LIMIT_DAILY_TOKENS", "600000")),
            environment=env.get("APP_ENV", "unknown"),
        )
