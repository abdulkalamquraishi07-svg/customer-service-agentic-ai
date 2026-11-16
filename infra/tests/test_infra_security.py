"""Assertions that lock in the security/reliability properties of the infrastructure."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from config import get_config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.data_stack import DataStack
from stacks.network_stack import NetworkStack


def _build(env_name: str):
    cfg = get_config(env_name)
    app = cdk.App()
    env = cdk.Environment(account="111111111111", region="us-east-1")
    network = NetworkStack(app, "net", cfg=cfg, env=env)
    data = DataStack(app, "data", cfg=cfg, env=env)
    auth = AuthStack(
        app, "auth", cfg=cfg, orders_table=data.tables["orders"], key=data.key, env=env
    )
    api = ApiStack(
        app,
        "api",
        cfg=cfg,
        vpc=network.vpc,
        app_sg=network.app_sg,
        user_pool=auth.user_pool,
        tables=data.tables,
        key=data.key,
        web_origins=["https://d111.cloudfront.net"],
        env=env,
    )
    return {k: Template.from_stack(s) for k, s in
            {"network": network, "data": data, "auth": auth, "api": api}.items()}


@pytest.fixture(scope="module")
def prod():
    return _build("prod")


@pytest.fixture(scope="module")
def dev():
    return _build("dev")


def test_no_nat_gateways(prod):
    prod["network"].resource_count_is("AWS::EC2::NatGateway", 0)


def test_prod_has_interface_endpoints_dev_does_not(prod, dev):
    prod["network"].resource_count_is("AWS::EC2::VPCEndpoint", 9)  # 2 gateway + 7 interface
    dev["network"].resource_count_is("AWS::EC2::VPCEndpoint", 2)


def test_all_business_tables_encrypted_pitr_and_protected_in_prod(prod):
    tables = {
        name: res for name, res in prod["data"].find_resources("AWS::DynamoDB::Table").items()
        if res["Properties"]["TableName"] != "csagent-prod-ratelimit"
    }
    assert len(tables) == 6
    for table in tables.values():
        props = table["Properties"]
        assert props["SSESpecification"]["SSEEnabled"] is True
        assert props["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"] is True
        assert props["DeletionProtectionEnabled"] is True
        assert table["DeletionPolicy"] == "Retain"


def test_rate_limit_table_is_disposable_ttl_and_not_business_data(prod):
    (table,) = [
        r["Properties"] for r in prod["data"].find_resources("AWS::DynamoDB::Table").values()
        if r["Properties"]["TableName"] == "csagent-prod-ratelimit"
    ]
    assert table["TimeToLiveSpecification"] == {"AttributeName": "expires_at", "Enabled": True}
    assert table["KeySchema"] == [{"AttributeName": "bucket", "KeyType": "HASH"}]
    # AWS-owned encryption on purpose: the agent runtime must need no KMS access.
    assert "SSESpecification" not in table


def test_customer_tables_are_keyed_by_customer_id(prod):
    for name in ("orders", "returns", "tickets"):
        prod["data"].has_resource_properties(
            "AWS::DynamoDB::Table",
            {
                "TableName": f"csagent-prod-{name}",
                "KeySchema": Match.array_with(
                    [{"AttributeName": "customer_id", "KeyType": "HASH"}]
                ),
            },
        )


def test_kms_key_rotation_enabled(prod):
    prod["data"].has_resource_properties("AWS::KMS::Key", {"EnableKeyRotation": True})


def test_user_pool_blocks_self_signup_and_has_strong_passwords(prod):
    prod["auth"].has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True},
            "Policies": {"PasswordPolicy": Match.object_like({"MinimumLength": 12})},
            "DeletionProtection": "ACTIVE",
        },
    )


def test_prod_has_no_signup_trigger_or_demo_seeding(prod):
    prod["auth"].resource_count_is("AWS::Lambda::Function", 0)
    pool = next(iter(prod["auth"].find_resources("AWS::Cognito::UserPool").values()))
    assert "LambdaConfig" not in pool["Properties"]


def test_dev_self_signup_verifies_email_and_only_grants_customers(dev):
    auth = dev["auth"]
    auth.has_resource_properties(
        "AWS::Cognito::UserPool",
        {
            "AdminCreateUserConfig": {"AllowAdminCreateUserOnly": False},
            "AutoVerifiedAttributes": ["email"],
            "LambdaConfig": {"PostConfirmation": Match.any_value()},
        },
    )
    auth.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "bizapi.signup.handler", "Timeout": 5,
         "Environment": {"Variables": Match.object_like({"APP_ENV": "dev"})}},
    )
    # The trigger may add users to groups in its own pool only (no wildcard resource).
    policies = auth.find_resources("AWS::IAM::Policy")
    statements = [s for p in policies.values()
                  for s in p["Properties"]["PolicyDocument"]["Statement"]]
    group_grants = [s for s in statements if "cognito-idp:AdminAddUserToGroup" in str(s["Action"])]
    assert len(group_grants) == 1
    assert group_grants[0]["Resource"] != "*"
    assert "Fn::GetAtt" in group_grants[0]["Resource"]
    # Seeding writes orders but cannot read or delete them.
    order_actions = [s["Action"] for s in statements if "dynamodb" in str(s["Action"])]
    assert order_actions and all("dynamodb:DeleteItem" not in str(a) for a in order_actions)
    assert all("dynamodb:GetItem" not in str(a) for a in order_actions)


def test_prod_config_rejects_demo_seeding():
    import dataclasses

    from config import get_config

    with pytest.raises(ValueError, match="prod"):
        dataclasses.replace(get_config("prod"), seed_demo_orders_on_signup=True)


def test_protected_route_requires_cognito_and_health_is_public(prod):
    methods = prod["api"].find_resources("AWS::ApiGateway::Method")
    by_auth = {m["Properties"]["AuthorizationType"] for m in methods.values()}
    assert "COGNITO_USER_POOLS" in by_auth
    assert "NONE" in by_auth


def test_waf_has_managed_rules_and_rate_limit_and_is_associated(prod):
    prod["api"].has_resource_properties(
        "AWS::WAFv2::WebACL",
        {
            "Rules": Match.array_with(
                [
                    Match.object_like({"Name": "AWSManagedRulesCommonRuleSet"}),
                    Match.object_like({"Name": "PerIpRateLimit"}),
                ]
            )
        },
    )
    prod["api"].resource_count_is("AWS::WAFv2::WebACLAssociation", 1)


def test_lambdas_run_in_vpc_with_tracing(prod):
    fns = prod["api"].find_resources("AWS::Lambda::Function")
    app_fns = [
        f for f in fns.values() if f["Properties"].get("Handler", "").startswith("handlers.")
    ]
    assert len(app_fns) == 2
    for fn in app_fns:
        assert "VpcConfig" in fn["Properties"]
        assert fn["Properties"]["TracingConfig"] == {"Mode": "Active"}


def test_api_never_logs_request_bodies(prod):
    prod["api"].has_resource_properties(
        "AWS::ApiGateway::Stage",
        {"MethodSettings": Match.array_with([Match.object_like({"DataTraceEnabled": False})])},
    )


# ---------------- business backend ----------------


def _policies_for(template: Template, function_logical_prefix: str) -> str:
    """All IAM policy statements attached to the role of the named function, as text."""
    import json

    out = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        if function_logical_prefix in json.dumps(res["Properties"]["Roles"]):
            out.append(json.dumps(res["Properties"]["PolicyDocument"]))
    return " ".join(out)


def test_refund_queue_has_dlq_redrive_and_tls_only(prod):
    prod["api"].has_resource_properties(
        "AWS::SQS::Queue",
        {
            "QueueName": "csagent-prod-refund-queue",
            "RedrivePolicy": Match.object_like({"maxReceiveCount": 5}),
            "SqsManagedSseEnabled": True,
        },
    )
    prod["api"].has_resource_properties(
        "AWS::SQS::QueuePolicy",
        {
            "PolicyDocument": Match.object_like(
                {
                    "Statement": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Effect": "Deny",
                                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                                }
                            )
                        ]
                    )
                }
            )
        },
    )


def test_worker_uses_partial_batch_responses_with_bounded_concurrency(prod):
    prod["api"].has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "ScalingConfig": {"MaximumConcurrency": 5},
        },
    )


def test_dlq_and_error_alarms_exist(prod):
    prod["api"].has_resource_properties(
        "AWS::CloudWatch::Alarm", {"AlarmName": "csagent-prod-refunddlqnotempty"}
    )
    prod["api"].has_resource_properties(
        "AWS::CloudWatch::Alarm", {"AlarmName": "csagent-prod-api-5xx"}
    )
    alarms = prod["api"].find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) >= 6  # dlq + 4 functions + api 5xx


def test_maintenance_runs_every_15_minutes(prod):
    prod["api"].has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(15 minutes)"}
    )


def test_least_privilege_per_function(prod):
    customer = _policies_for(prod["api"], "CustomerApi")
    worker = _policies_for(prod["api"], "RefundWorker")
    scheduler = _policies_for(prod["api"], "Scheduler")

    # Guard against a vacuous pass: each lookup must actually find the function's policy.
    assert "returns" in customer.lower() and "sqs:SendMessage" in customer
    assert "returns" in worker.lower() and "ledger" in worker.lower()
    assert "returns" in scheduler.lower() and "sqs:SendMessage" in scheduler
    assert "ledger" not in customer.lower()  # customers' role cannot touch the payout ledger
    assert "dynamodb:PutItem" in worker  # worker records payouts
    assert "tickets" not in worker.lower() and "tickets" not in scheduler.lower()
    for policy in (customer, worker, scheduler):
        assert "dynamodb:*" not in policy and '"*"' not in policy.replace('"Resource": "*"', "")


def test_audit_table_is_append_only_for_every_function(prod):
    import json

    audit_statements = 0
    for res in prod["api"].find_resources("AWS::IAM::Policy").values():
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            if "audit" in json.dumps(stmt).lower():
                audit_statements += 1
                actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
                assert actions == ["dynamodb:PutItem"], actions
    assert audit_statements == 4  # customer, ops, worker, scheduler


def test_edge_models_reject_unknown_fields_and_require_idempotency_header(prod):
    models = prod["api"].find_resources("AWS::ApiGateway::Model")
    assert len(models) == 4
    for model in models.values():
        assert model["Properties"]["Schema"]["additionalProperties"] is False

    methods = prod["api"].find_resources("AWS::ApiGateway::Method")
    with_key = [
        m
        for m in methods.values()
        if m["Properties"].get("RequestParameters", {}).get("method.request.header.Idempotency-Key")
    ]
    assert len(with_key) == 2  # POST /returns and POST /tickets


def test_all_business_routes_require_cognito(prod):
    methods = prod["api"].find_resources("AWS::ApiGateway::Method")
    unauthenticated = [
        m for m in methods.values()
        if m["Properties"]["AuthorizationType"] == "NONE"
        and m["Properties"]["HttpMethod"] != "OPTIONS"
    ]
    assert len(unauthenticated) == 1  # only GET /health


def test_business_lambdas_in_vpc_for_prod_not_dev(prod, dev):
    def business(template):
        fns = template.find_resources("AWS::Lambda::Function")
        return [f for f in fns.values() if f["Properties"].get("Handler", "").startswith("bizapi.")]

    assert len(business(prod["api"])) == 4
    assert all("VpcConfig" in f["Properties"] for f in business(prod["api"]))
    assert all("VpcConfig" not in f["Properties"] for f in business(dev["api"]))
    traced = [f["Properties"]["TracingConfig"] for f in business(prod["api"])]
    assert traced == [{"Mode": "Active"}] * 4


def test_lambda_log_groups_use_standard_names_and_retention(prod):
    groups = prod["api"].find_resources("AWS::Logs::LogGroup")
    names = {g["Properties"].get("LogGroupName") for g in groups.values()}
    for fn in ("customerapi", "opsapi", "refundworker", "scheduler", "health", "whoami"):
        assert f"/aws/lambda/csagent-prod-{fn}" in names
    assert all("RetentionInDays" in g["Properties"] for g in groups.values())
