"""Agent monitoring: one dashboard and SLO-style alarms on the metrics the runtime publishes
(namespace CSAgent, dimension Environment). Alarms notify the environment's SNS topic.

Thresholds are starting values chosen from measured dev behavior (typical turn 5-16 s, worst 42 s
including a cold vector store); revisit them after a week of real traffic.
"""

from aws_cdk import Duration
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from config import EnvConfig, resource_name
from constructs import Construct

NAMESPACE = "CSAgent"


class AgentMonitoring(Construct):
    def __init__(
        self, scope: Construct, construct_id: str, *, cfg: EnvConfig, alarm_topic: sns.ITopic
    ) -> None:
        super().__init__(scope, construct_id)
        action = cw_actions.SnsAction(alarm_topic)

        def metric(name: str, stat: str = "Sum", minutes: int = 5, label: str | None = None):
            return cw.Metric(
                namespace=NAMESPACE,
                metric_name=name,
                dimensions_map={"Environment": cfg.name},
                statistic=stat,
                period=Duration.minutes(minutes),
                label=label or name,
            )

        def alarm(id_: str, m: cw.IMetric, threshold: float, description: str, periods: int = 1):
            a = cw.Alarm(
                self,
                id_,
                alarm_name=resource_name(cfg, f"agent-{id_.lower()}"),
                alarm_description=description,
                metric=m,
                threshold=threshold,
                evaluation_periods=periods,
                datapoints_to_alarm=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)
            return a

        self.alarms = [
            alarm(
                "LatencyP95",
                metric("LatencyMs", "p95"),
                60_000,
                "p95 turn latency at or above 60 s for 15 minutes",
                periods=3,
            ),
            alarm(
                "TurnFailures",
                metric("TurnFailed"),
                3,
                "Turns are crashing (guardrail, limiter or model errors): see the runtime log",
            ),
            alarm(
                "ToolErrors",
                metric("ToolErrors"),
                10,
                "Agent tools are failing (business API or knowledge base degraded)",
            ),
            alarm(
                "RateLimitedSurge",
                metric("RateLimited"),
                30,
                "Many customers hitting rate limits: possible scripted abuse",
            ),
            alarm(
                "GuardrailInputSurge",
                metric("GuardrailInputBlocked"),
                25,
                "Burst of blocked customer messages: possible prompt-injection campaign",
            ),
            alarm(
                "UnverifiedClaims",
                metric("ClaimCheckTriggered", minutes=60),
                5,
                "The model keeps promising actions it did not perform: prompt or model regression",
            ),
            alarm(
                "TokenSpend",
                cw.MathExpression(
                    expression="i + o",
                    using_metrics={
                        "i": metric("InputTokens", minutes=60),
                        "o": metric("OutputTokens", minutes=60),
                    },
                    period=Duration.minutes(60),
                    label="Tokens per hour",
                ),
                cfg.agent_token_alarm_per_hour,
                "Environment-wide token use per hour is above the configured threshold",
            ),
        ]

        dashboard = cw.Dashboard(
            self,
            "Dashboard",
            dashboard_name=resource_name(cfg, "agent"),
            default_interval=Duration.hours(6),
        )

        def graph(title: str, left: list[cw.IMetric], width: int = 8, **kw):
            return cw.GraphWidget(title=title, left=left, width=width, height=6, **kw)

        dashboard.add_widgets(
            graph("Turns and failures", [metric("Turns"), metric("TurnFailed")]),
            graph(
                "Turn latency (ms)",
                [metric("LatencyMs", "p50", label="p50"), metric("LatencyMs", "p95", label="p95")],
            ),
            graph("Tokens", [metric("InputTokens"), metric("OutputTokens")]),
        )
        dashboard.add_widgets(
            graph("Tool errors", [metric("ToolErrors")]),
            graph(
                "Safety interventions",
                [
                    metric("GuardrailInputBlocked"),
                    metric("GuardrailOutputBlocked"),
                    metric("ClaimCheckTriggered"),
                ],
            ),
            graph("Abuse controls", [metric("RateLimited"), metric("Rejected")]),
        )
        dashboard.add_widgets(
            graph(
                "Grounding (hallucination check)",
                [metric("GroundingBlocked"), metric("GroundingShadowBlocked")],
                right=[
                    metric("GroundingScore", "p10", label="score p10"),
                    metric("GroundingScore", "p50", label="score p50"),
                ],
            ),
            graph("Escalations suggested", [metric("EscalationSuggested")]),
            graph("Request limits hit", [metric("LimitHit")]),
            cw.AlarmStatusWidget(title="Agent alarms", alarms=self.alarms, width=8, height=6),
        )
