"""Agent runtime: Strands supervisor + specialists on Bedrock AgentCore Runtime (direct code zip).

Inbound auth: AgentCore validates the caller's Cognito ID token (custom JWT authorizer) before
the agent code runs, and forwards the Authorization header (allowlisted) so tools can call the
business API as that customer. The execution role can invoke only the configured model and
retrieve only from this environment's knowledge base; it has no database or table access.
"""

import hashlib
import json

from aws_cdk import CfnOutput, CfnResource, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_ssm as ssm
from config import EnvConfig, client_param, resource_name
from constructs import Construct

from stacks.agent_monitoring import AgentMonitoring

MODEL_ID = "us.amazon.nova-pro-v1:0"
# Regions the "us." cross-region inference profile can route to (from get-inference-profile).
MODEL_REGIONS = ("us-east-1", "us-east-2", "us-west-2")
FOUNDATION_MODEL = "amazon.nova-pro-v1:0"

# Guardrail policy (ADR 0005). Customer INPUT vs assistant OUTPUT strengths differ on purpose:
# angry customers are normal in support, so insults are not blocked on input (they reach the
# agents and get escalated) but the assistant must never produce them.
CONTENT_FILTERS = [
    # (type, input_strength, output_strength)
    ("PROMPT_ATTACK", "HIGH", "NONE"),  # output must be NONE for prompt attacks
    ("HATE", "HIGH", "HIGH"),
    ("SEXUAL", "HIGH", "HIGH"),
    ("VIOLENCE", "MEDIUM", "HIGH"),
    ("MISCONDUCT", "MEDIUM", "HIGH"),
    ("INSULTS", "NONE", "HIGH"),
]
# Masked, never blocked: a customer pasting a card number still gets help. Dates, amounts and
# PIN-like numbers are deliberately NOT listed (they collide with order data). No profanity word
# list: word policies apply to input too and would block frustrated customers.
PII_ANONYMIZE = [
    "CREDIT_DEBIT_CARD_NUMBER",
    "CREDIT_DEBIT_CARD_CVV",
    "PASSWORD",
    "AWS_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
]
POLICY_FINGERPRINT = hashlib.sha256(
    json.dumps([CONTENT_FILTERS, PII_ANONYMIZE, "pii-both-directions"]).encode()
).hexdigest()[:12]


class AgentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        package_zip: str,
        api_url: str,
        user_pool_id: str,
        web_client_id: str,
        knowledge_base_id: str,
        knowledge_base_arn: str,
        ratelimit_table: dynamodb.ITable,
        alarm_topic: sns.ITopic,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        runtime_name = f"csagent_{cfg.name}_supervisor"  # AgentCore: letters, digits, _
        AgentMonitoring(self, "Monitoring", cfg=cfg, alarm_topic=alarm_topic)

        code = s3_assets.Asset(self, "AgentCode", path=package_zip)

        guardrail = bedrock.CfnGuardrail(
            self,
            "Guardrail",
            name=resource_name(cfg, "customer-chat"),
            description="Customer service chat: prompt attacks, harmful content, payment PII",
            blocked_input_messaging="I'm sorry, I can't help with that request.",
            blocked_outputs_messaging="I'm sorry, I can't provide that response.",
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=kind, input_strength=inp, output_strength=out
                    )
                    for kind, inp, out in CONTENT_FILTERS
                ]
            ),
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(type=kind, action="ANONYMIZE")
                    for kind in PII_ANONYMIZE
                ]
            ),
        )
        # With only `Action`, the deployed guardrail masked PII on OUTPUT but never on INPUT
        # (verified live). Set both directions explicitly; this CDK version lacks the fields.
        guardrail.add_property_override(
            "SensitiveInformationPolicyConfig.PiiEntitiesConfig",
            [
                {
                    "Type": kind,
                    "Action": "ANONYMIZE",
                    "InputAction": "ANONYMIZE",
                    "InputEnabled": True,
                    "OutputAction": "ANONYMIZE",
                    "OutputEnabled": True,
                }
                for kind in PII_ANONYMIZE
            ],
        )
        # Separate guardrail for the hallucination check, so the content guardrail (and its
        # canary) is unaffected. Action NONE: it only scores; the agent decides from a
        # calibrated threshold (GROUNDING_THRESHOLD), so tuning needs no guardrail change.
        # Classic tier: CloudFormation cannot select the Standard tier yet.
        grounding = bedrock.CfnGuardrail(
            self,
            "GroundingGuardrail",
            name=resource_name(cfg, "reply-grounding"),
            description="Scores whether agent replies are supported by the turn's tool results",
            blocked_input_messaging="Not applicable: grounding is checked on replies only.",
            blocked_outputs_messaging="I couldn't confirm that from our records.",
        )
        grounding.add_property_override(
            "ContextualGroundingPolicyConfig",
            {
                "FiltersConfig": [
                    {
                        "Type": "GROUNDING",
                        "Threshold": cfg.agent_grounding_threshold,
                        "Action": "NONE",
                        "Enabled": True,
                    }
                ]
            },
        )
        grounding_version = bedrock.CfnGuardrailVersion(
            self,
            "GroundingGuardrailVersion",
            guardrail_identifier=grounding.attr_guardrail_id,
            description=f"grounding threshold {cfg.agent_grounding_threshold}",
        )
        self.grounding_guardrail = grounding
        # Immutable published version; the fingerprint forces a new version on policy changes.
        guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "GuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description=f"policy {POLICY_FINGERPRINT}",
        )

        role = iam.Role(
            self,
            "ExecutionRole",
            role_name=resource_name(cfg, "agent-runtime"),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                        )
                    },
                },
            ),
        )
        code.grant_read(role)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeConfiguredModelOnly",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{MODEL_ID}",
                    *(
                        f"arn:aws:bedrock:{r}::foundation-model/{FOUNDATION_MODEL}"
                        for r in MODEL_REGIONS
                    ),
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ApplyChatGuardrail",
                actions=["bedrock:ApplyGuardrail"],
                resources=[guardrail.attr_guardrail_arn, grounding.attr_guardrail_arn],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RetrievePolicyDocuments",
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base_arn],
            )
        )
        log_prefix = f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore"
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[f"{log_prefix}/runtimes/*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"{log_prefix}/runtimes/*:log-stream:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{self.region}:{self.account}:log-group:*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],  # X-Ray does not support resource-level permissions
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="PublishRuntimeAndAgentMetrics",
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],  # PutMetricData has no resource-level permissions
                conditions={
                    "StringEquals": {"cloudwatch:namespace": ["bedrock-agentcore", "CSAgent"]}
                },
            )
        )
        # The ONLY data store the runtime may touch: per-customer rate-limit counters.
        role.add_to_policy(
            iam.PolicyStatement(
                sid="RateLimitCounters",
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[ratelimit_table.table_arn],
            )
        )

        discovery_url = (
            f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}"
            "/.well-known/openid-configuration"
        )
        runtime = CfnResource(
            self,
            "Runtime",
            type="AWS::BedrockAgentCore::Runtime",
            properties={
                "AgentRuntimeName": runtime_name,
                "Description": "Customer service supervisor + specialist agents",
                "RoleArn": role.role_arn,
                "AgentRuntimeArtifact": {
                    "CodeConfiguration": {
                        "Code": {
                            "S3": {"Bucket": code.s3_bucket_name, "Prefix": code.s3_object_key}
                        },
                        "Runtime": "PYTHON_3_12",
                        "EntryPoint": ["main.py"],
                    }
                },
                # PUBLIC: the agent calls the public API Gateway endpoint (which enforces WAF,
                # throttling and Cognito) and Bedrock. VPC mode is a documented follow-up.
                "NetworkConfiguration": {"NetworkMode": "PUBLIC"},
                "AuthorizerConfiguration": {
                    "CustomJWTAuthorizer": {
                        "DiscoveryUrl": discovery_url,
                        # ID tokens carry the app client id in `aud`.
                        "AllowedAudience": [web_client_id],
                    }
                },
                "RequestHeaderConfiguration": {"RequestHeaderAllowlist": ["Authorization"]},
                "LifecycleConfiguration": {
                    "IdleRuntimeSessionTimeout": 900,
                    "MaxLifetime": 3600,
                },
                "EnvironmentVariables": {
                    "API_BASE_URL": api_url,
                    "MODEL_ID": MODEL_ID,
                    "KNOWLEDGE_BASE_ID": knowledge_base_id,
                    "GUARDRAIL_ID": guardrail.attr_guardrail_id,
                    "GUARDRAIL_VERSION": guardrail_version.attr_version,
                    "GROUNDING_GUARDRAIL_ID": grounding.attr_guardrail_id,
                    "GROUNDING_GUARDRAIL_VERSION": grounding_version.attr_version,
                    "GROUNDING_THRESHOLD": str(cfg.agent_grounding_threshold),
                    "GROUNDING_MODE": cfg.agent_grounding_mode,
                    "RATE_LIMIT_TABLE": ratelimit_table.table_name,
                    "LIMIT_PER_MINUTE": str(cfg.agent_limit_per_minute),
                    "LIMIT_PER_HOUR": str(cfg.agent_limit_per_hour),
                    "LIMIT_PER_DAY": str(cfg.agent_limit_per_day),
                    "LIMIT_DAILY_TOKENS": str(cfg.agent_limit_daily_tokens),
                    "APP_ENV": cfg.name,
                },
            },
        )
        runtime.node.add_dependency(role)
        # AgentCore creates its own log group named after the runtime *id* plus endpoint
        # suffix, so it cannot be pre-created here. Setting its retention is a follow-up.

        runtime_arn = runtime.get_att("AgentRuntimeArn").to_string()
        # Published to SSM for other stacks: a cross-stack GetAtt on a raw CfnResource is not
        # auto-exported, and BucketDeployment JSON rejects Fn::ImportValue (both failed live).
        ssm.StringParameter(
            self,
            "RuntimeArnParameter",
            parameter_name=client_param(cfg, "agentRuntimeArn"),
            string_value=runtime_arn,
            description="AgentCore runtime ARN for clients (web app config)",
        )
        CfnOutput(self, "AgentRuntimeArn", value=runtime_arn)
        CfnOutput(self, "AgentRuntimeName", value=runtime_name)
