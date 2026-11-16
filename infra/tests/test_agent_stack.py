"""Agent runtime infrastructure: inbound auth, least privilege, packaging contract."""

import json
import zipfile

import aws_cdk as cdk
import pytest
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_sns as sns
from aws_cdk.assertions import Template
from config import get_config
from stacks.agent_stack import FOUNDATION_MODEL, MODEL_ID, AgentStack


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    zip_path = tmp_path_factory.mktemp("pkg") / "agent.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("main.py", "print('stub')\n")
    app = cdk.App()
    deps = cdk.Stack(app, "deps", env=cdk.Environment(account="111111111111", region="us-east-1"))
    table = dynamodb.Table(
        deps,
        "T",
        table_name="csagent-prod-ratelimit",
        partition_key=dynamodb.Attribute(name="bucket", type=dynamodb.AttributeType.STRING),
    )
    topic = sns.Topic(deps, "Topic")
    stack = AgentStack(
        app,
        "agent",
        cfg=get_config("prod"),
        package_zip=str(zip_path),
        api_url="https://api.example.com/prod/",
        user_pool_id="us-east-1_POOL",
        web_client_id="client123",
        knowledge_base_id="KB123",
        knowledge_base_arn="arn:aws:bedrock:us-east-1:111111111111:knowledge-base/KB123",
        ratelimit_table=table,
        alarm_topic=topic,
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _runtime(template):
    (runtime,) = template.find_resources("AWS::BedrockAgentCore::Runtime").values()
    return runtime["Properties"]


def _logical_id(template, prefix):
    kinds = {
        "Guardrail": "AWS::Bedrock::Guardrail",
        "GuardrailVersion": "AWS::Bedrock::GuardrailVersion",
    }
    (logical_id,) = [k for k in template.find_resources(kinds[prefix]) if k.startswith(prefix)
                     and not k.startswith("Grounding")]
    return logical_id


def _guardrail(template):
    (g,) = [g for k, g in template.find_resources("AWS::Bedrock::Guardrail").items()
            if not k.startswith("Grounding")]
    return g["Properties"]


def test_grounding_guardrail_only_scores_replies(template):
    (g,) = [g["Properties"] for k, g in template.find_resources("AWS::Bedrock::Guardrail").items()
            if k.startswith("Grounding")]
    (f,) = g["ContextualGroundingPolicyConfig"]["FiltersConfig"]
    assert f["Type"] == "GROUNDING" and f["Action"] == "NONE" and f["Enabled"] is True
    # It must not also run content/PII policies (that is the chat guardrail's job).
    assert "ContentPolicyConfig" not in g and "SensitiveInformationPolicyConfig" not in g


def test_angry_customers_are_not_blocked_but_assistant_insults_are(template):
    filters = {f["Type"]: f for f in _guardrail(template)["ContentPolicyConfig"]["FiltersConfig"]}
    assert filters["INSULTS"]["InputStrength"] == "NONE"
    assert filters["INSULTS"]["OutputStrength"] == "HIGH"
    assert filters["PROMPT_ATTACK"]["InputStrength"] == "HIGH"
    assert filters["PROMPT_ATTACK"]["OutputStrength"] == "NONE"
    assert all(filters[k]["OutputStrength"] == "HIGH" for k in ("HATE", "SEXUAL", "VIOLENCE"))


def test_payment_pii_is_masked_never_blocked_and_no_word_lists(template):
    props = _guardrail(template)
    entities = props["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
    assert {e["Type"] for e in entities} >= {"CREDIT_DEBIT_CARD_NUMBER", "CREDIT_DEBIT_CARD_CVV"}
    assert {e["Action"] for e in entities} == {"ANONYMIZE"}
    # Live finding: without explicit input settings, card numbers were only masked on output.
    for e in entities:
        assert e["InputEnabled"] is True and e["InputAction"] == "ANONYMIZE"
        assert e["OutputEnabled"] is True and e["OutputAction"] == "ANONYMIZE"
    # Would collide with order data (dates, amounts) or block frustrated customers.
    assert not {"CREDIT_DEBIT_CARD_EXPIRY", "PIN", "PHONE", "NAME"} & {e["Type"] for e in entities}
    assert "WordPolicyConfig" not in props


def test_runtime_may_apply_only_its_own_guardrail(template):
    (stmt,) = [s for s in _policy_statements(template) if s.get("Sid") == "ApplyChatGuardrail"]
    assert stmt["Action"] == "bedrock:ApplyGuardrail"
    resources = json.dumps(stmt["Resource"])
    assert _logical_id(template, "Guardrail") in resources and "GroundingGuardrail" in resources
    assert len(stmt["Resource"]) == 2  # exactly these two guardrails


def _policy_statements(template):
    statements = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        statements += policy["Properties"]["PolicyDocument"]["Statement"]
    return statements


def test_inbound_requires_cognito_jwt_for_this_app_client(template):
    props = _runtime(template)
    jwt = props["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert jwt["AllowedAudience"] == ["client123"]
    assert "us-east-1_POOL/.well-known/openid-configuration" in json.dumps(jwt["DiscoveryUrl"])
    # Only the Authorization header is forwarded to agent code.
    assert props["RequestHeaderConfiguration"] == {"RequestHeaderAllowlist": ["Authorization"]}


def test_runtime_matches_the_built_package(template):
    code = _runtime(template)["AgentRuntimeArtifact"]["CodeConfiguration"]
    assert code["Runtime"] == "PYTHON_3_12"  # build_agent.py targets cp312 arm64 wheels
    assert code["EntryPoint"] == ["main.py"]


def test_environment_points_at_api_model_and_kb(template):
    env = _runtime(template)["EnvironmentVariables"]
    assert set(env) == {
        "API_BASE_URL", "MODEL_ID", "KNOWLEDGE_BASE_ID", "GUARDRAIL_ID", "GUARDRAIL_VERSION",
        "APP_ENV", "RATE_LIMIT_TABLE", "LIMIT_PER_MINUTE", "LIMIT_PER_HOUR", "LIMIT_PER_DAY",
        "LIMIT_DAILY_TOKENS", "GROUNDING_GUARDRAIL_ID", "GROUNDING_GUARDRAIL_VERSION",
        "GROUNDING_THRESHOLD", "GROUNDING_MODE",
    }
    assert env["GROUNDING_MODE"] in ("shadow", "enforce")
    assert env["GROUNDING_THRESHOLD"] == str(get_config("prod").agent_grounding_threshold)
    # Always configured when deployed: an unset table would silently disable rate limiting.
    assert env["RATE_LIMIT_TABLE"] and env["LIMIT_PER_MINUTE"] == "6"
    assert env["API_BASE_URL"] == "https://api.example.com/prod/"
    assert env["MODEL_ID"] == MODEL_ID and env["KNOWLEDGE_BASE_ID"] == "KB123"
    # The runtime pins the published guardrail version, never DRAFT.
    assert env["GUARDRAIL_VERSION"] == {"Fn::GetAtt": [_logical_id(template, "GuardrailVersion"),
                                                        "Version"]}
    assert not any(k.lower().startswith(("aws_secret", "aws_access")) for k in env)


def test_session_lifecycle_is_bounded(template):
    life = _runtime(template)["LifecycleConfiguration"]
    assert life["IdleRuntimeSessionTimeout"] <= 900 and life["MaxLifetime"] <= 3600


def test_trust_policy_is_confused_deputy_safe(template):
    roles = template.find_resources("AWS::IAM::Role")
    (role,) = roles.values()
    trust = json.dumps(role["Properties"]["AssumeRolePolicyDocument"])
    assert "bedrock-agentcore.amazonaws.com" in trust
    assert "aws:SourceAccount" in trust and "aws:SourceArn" in trust


def test_model_access_is_limited_to_the_configured_model(template):
    statements = _policy_statements(template)
    (stmt,) = [s for s in statements if s.get("Sid") == "InvokeConfiguredModelOnly"]
    resources = json.dumps(stmt["Resource"])
    assert f"inference-profile/{MODEL_ID}" in resources
    assert resources.count(f"foundation-model/{FOUNDATION_MODEL}") == 3
    assert "foundation-model/*" not in resources and '"*"' not in resources


def test_knowledge_access_is_retrieve_only_on_this_kb(template):
    (stmt,) = [s for s in _policy_statements(template) if s.get("Sid") == "RetrievePolicyDocuments"]
    assert stmt["Action"] == "bedrock:Retrieve"
    assert stmt["Resource"].endswith(":knowledge-base/KB123")


def test_runtime_role_has_no_data_store_or_admin_access(template):
    text = json.dumps(_policy_statements(template))
    forbidden_actions = (
        "rds-data:", "secretsmanager:", "iam:", "sqs:", "kms:", "s3:Put", "s3:Delete",
    )
    for forbidden in forbidden_actions:
        assert forbidden not in text, forbidden
    for stmt in _policy_statements(template):
        actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        assert not any(a == "*" or a.endswith(":*") for a in actions), actions
        if stmt["Resource"] == "*":
            # Only services without resource-level permissions, and metrics scoped by namespace.
            assert all(a.startswith(("xray:", "cloudwatch:PutMetricData")) for a in actions)


def test_the_only_data_store_access_is_the_rate_limit_counter_table(template):
    dynamo = [s for s in _policy_statements(template)
              if "dynamodb:" in json.dumps(s["Action"])]
    (stmt,) = dynamo
    assert stmt["Sid"] == "RateLimitCounters"
    # No Put, Delete, Scan or Query: the runtime can only read and increment its own counters.
    assert sorted(stmt["Action"]) == ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    assert "ratelimit" in json.dumps(stmt["Resource"]).lower() or "Fn::ImportValue" in json.dumps(
        stmt["Resource"]
    )


def test_custom_metrics_are_limited_to_the_agent_namespaces(template):
    (stmt,) = [s for s in _policy_statements(template)
               if s.get("Sid") == "PublishRuntimeAndAgentMetrics"]
    allowed = stmt["Condition"]["StringEquals"]["cloudwatch:namespace"]
    assert sorted(allowed) == ["CSAgent", "bedrock-agentcore"]


def test_agent_slo_alarms_exist_and_notify(template):
    alarms = {a["Properties"]["AlarmName"]: a["Properties"]
              for a in template.find_resources("AWS::CloudWatch::Alarm").values()}
    names = {n.removeprefix("csagent-prod-agent-") for n in alarms}
    assert names == {"latencyp95", "turnfailures", "toolerrors", "ratelimitedsurge",
                     "guardrailinputsurge", "unverifiedclaims", "tokenspend"}
    for props in alarms.values():
        assert props["AlarmActions"], props["AlarmName"]
        assert props["TreatMissingData"] == "notBreaching"  # quiet nights must not page anyone
    latency = alarms["csagent-prod-agent-latencyp95"]
    (m,) = latency["Metrics"]
    assert m["MetricStat"]["Stat"] == "p95"
    assert m["MetricStat"]["Metric"]["Namespace"] == "CSAgent"
    assert m["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "Environment", "Value": "prod"}]
    assert latency["Threshold"] == 60000
    # Sustained for 15 minutes, not a single slow turn.
    assert latency["EvaluationPeriods"] == latency["DatapointsToAlarm"] == 3
    spend = alarms["csagent-prod-agent-tokenspend"]
    assert spend["Threshold"] == get_config("prod").agent_token_alarm_per_hour
    assert any(x.get("Expression") == "i + o" for x in spend["Metrics"])


def test_dashboard_covers_the_agent_metrics(template):
    (dash,) = template.find_resources("AWS::CloudWatch::Dashboard").values()
    body = json.dumps(dash["Properties"]["DashboardBody"])
    for metric in ("Turns", "TurnFailed", "LatencyMs", "InputTokens", "ToolErrors",
                   "RateLimited", "GuardrailInputBlocked", "ClaimCheckTriggered"):
        assert metric in body, metric
