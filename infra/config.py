"""Per-environment configuration. Select with: cdk deploy -c env=dev|staging|prod."""

from dataclasses import dataclass

APP_NAME = "csagent"


@dataclass(frozen=True)
class EnvConfig:
    name: str
    # Safety: never destroy data stores in staging/prod.
    retain_data: bool
    deletion_protection: bool
    # Interface VPC endpoints cost ~USD 7/month per AZ each; keep off in dev.
    interface_endpoints: bool
    max_azs: int
    # API protection
    api_rate_limit: int  # steady-state requests/second per stage
    api_burst_limit: int
    waf_rate_limit_per_5min: int  # per source IP
    log_retention_days: int
    # Lambdas in the VPC need interface endpoints (cost); dev runs them outside the VPC.
    lambdas_in_vpc: bool
    # Knowledge base vector store (Aurora Serverless v2). min 0 = auto-pause when idle.
    kb_min_acu: float
    kb_max_acu: float
    kb_auto_pause_seconds: int
    # The local Vite dev server may call the API (CORS). Never enable outside dev.
    allow_localhost_origin: bool = False
    # Agent path abuse/spend protection, per customer (see csagent_agents/ratelimit.py).
    agent_limit_per_minute: int = 6
    agent_limit_per_hour: int = 60
    agent_limit_per_day: int = 250
    agent_limit_daily_tokens: int = 600_000
    # Alarm when the whole environment burns more tokens than this in an hour. Starting values:
    # tune after a week of real traffic.
    agent_token_alarm_per_hour: int = 2_000_000
    # Minimum contextual-grounding score for a reply to be shown (Classic tier; see ADR 0007).
    # Calibrated in dev: real, correct replies from a 15/15 eval run scored 0.10-0.99 (ticket and
    # escalation replies lowest); clear inventions scored 0.00-0.03. 0.05 blocks those inventions
    # and blocked no correct reply in the sample. It does NOT catch subtler errors (a wrong
    # carrier scored 0.19). Revisit with the Standard tier once CloudFormation supports it.
    agent_grounding_threshold: float = 0.05
    # "enforce" = replace ungrounded replies; "shadow" = score and record only.
    agent_grounding_mode: str = "enforce"
    # Public sign-up (email + verification code). New users join the `customers` group only.
    # Off by default: before enabling it beyond dev, add bot protection and SES email sending
    # (see docs/production-readiness.md).
    self_sign_up: bool = False
    # Give each self-registered user sample orders. Demo data: never in prod.
    seed_demo_orders_on_signup: bool = False

    def __post_init__(self) -> None:
        if self.name == "prod" and self.seed_demo_orders_on_signup:
            raise ValueError("Demo order seeding must not be enabled in prod")


ENVIRONMENTS: dict[str, EnvConfig] = {
    "dev": EnvConfig(
        name="dev",
        retain_data=False,
        deletion_protection=False,
        interface_endpoints=False,
        max_azs=2,
        api_rate_limit=20,
        api_burst_limit=40,
        waf_rate_limit_per_5min=1000,
        log_retention_days=14,
        lambdas_in_vpc=False,
        kb_min_acu=0,
        kb_max_acu=4,
        kb_auto_pause_seconds=600,
        allow_localhost_origin=True,
        self_sign_up=True,
        seed_demo_orders_on_signup=True,
    ),
    "staging": EnvConfig(
        name="staging",
        retain_data=True,
        deletion_protection=True,
        interface_endpoints=True,
        max_azs=2,
        api_rate_limit=50,
        api_burst_limit=100,
        waf_rate_limit_per_5min=1500,
        log_retention_days=30,
        lambdas_in_vpc=True,
        kb_min_acu=0,
        kb_max_acu=4,
        kb_auto_pause_seconds=1800,
    ),
    "prod": EnvConfig(
        name="prod",
        retain_data=True,
        deletion_protection=True,
        interface_endpoints=True,
        max_azs=3,
        api_rate_limit=200,
        api_burst_limit=400,
        waf_rate_limit_per_5min=2000,
        log_retention_days=365,
        lambdas_in_vpc=True,
        kb_min_acu=0.5,
        kb_max_acu=8,
        kb_auto_pause_seconds=3600,
    ),
}


def get_config(env_name: str) -> EnvConfig:
    try:
        return ENVIRONMENTS[env_name]
    except KeyError as exc:
        raise ValueError(f"Unknown env '{env_name}'. Use one of {sorted(ENVIRONMENTS)}") from exc


def resource_name(cfg: EnvConfig, suffix: str) -> str:
    return f"{APP_NAME}-{cfg.name}-{suffix}"


def client_param(cfg: EnvConfig, key: str) -> str:
    """SSM parameter holding a client-facing setting (read by the web content stack)."""
    return f"/{APP_NAME}/{cfg.name}/client/{key}"
