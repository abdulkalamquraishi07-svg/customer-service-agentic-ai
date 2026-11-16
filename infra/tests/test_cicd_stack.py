"""CI/CD roles: OIDC trust is pinned to one repo + environment; permissions stay narrow."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template
from config import get_config
from stacks.auth_stack import AuthStack
from stacks.cicd_stack import CicdStack, GithubOidcStack
from stacks.data_stack import DataStack

REPO = "acme-corp/customer-service-agents"
ENV = cdk.Environment(account="111111111111", region="us-east-1")


def _template(env_name: str = "prod") -> Template:
    cfg = get_config(env_name)
    app = cdk.App()
    data = DataStack(app, "data", cfg=cfg, env=ENV)
    auth = AuthStack(
        app, "auth", cfg=cfg, orders_table=data.tables["orders"], key=data.key, env=ENV
    )
    stack = CicdStack(app, "cicd", cfg=cfg, github_repo=REPO, user_pool=auth.user_pool,
                      key=data.key, tables=data.tables, env=ENV)
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def prod():
    return _template("prod")


def _roles(template):
    roles = template.find_resources("AWS::IAM::Role")
    return {r["Properties"]["RoleName"]: r["Properties"] for r in roles.values()}


def _statements(template, role_name):
    out = []
    for res in template.find_resources("AWS::IAM::Policy").values():
        if role_name in json.dumps(res["Properties"]["Roles"]):
            out += res["Properties"]["PolicyDocument"]["Statement"]
    return out


def test_oidc_provider_uses_github_issuer_and_sts_audience():
    t = Template.from_stack(GithubOidcStack(cdk.App(), "oidc", env=ENV))
    t.has_resource_properties(
        "AWS::IAM::OIDCProvider",
        {
            "Url": "https://token.actions.githubusercontent.com",
            "ClientIdList": ["sts.amazonaws.com"],
        },
    )


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_trust_is_pinned_to_exact_repo_and_environment_with_no_wildcards(env_name):
    roles = _roles(_template(env_name))
    assert set(roles) == {f"csagent-{env_name}-ci-deploy", f"csagent-{env_name}-ci-verify"}
    for props in roles.values():
        (stmt,) = props["AssumeRolePolicyDocument"]["Statement"]
        assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
        cond = stmt["Condition"]["StringEquals"]
        assert cond["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
        assert cond["token.actions.githubusercontent.com:sub"] == (
            f"repo:{REPO}:environment:{env_name}"
        )
        assert "StringLike" not in stmt["Condition"]  # no pattern matching on the subject
        assert props["MaxSessionDuration"] == 3600


def test_deploy_role_can_only_assume_cdk_bootstrap_roles(prod):
    stmts = _statements(prod, "DeployRole")
    assert len(stmts) == 1 and stmts[0]["Action"] == "sts:AssumeRole"
    resources = json.dumps(stmts[0]["Resource"])
    for kind in ("deploy", "file-publishing", "lookup"):
        assert f"cdk-hnb659fds-{kind}-role-111111111111-us-east-1" in resources
    assert "*" not in resources


def test_verify_role_has_no_iam_cloudformation_write_or_wildcard_actions(prod):
    stmts = _statements(prod, "VerifyRole")
    assert stmts
    for s in stmts:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        assert not any(a == "*" or a.endswith(":*") for a in actions), actions
        assert not any(a.startswith(("iam:", "sts:", "lambda:", "s3:")) for a in actions), actions
        assert "cloudformation:CreateStack" not in actions
    text = json.dumps(stmts)
    assert "cloudformation:DescribeStacks" in text and "stack/csagent-prod-" in text


def test_verify_role_writes_are_limited_to_test_data(prod):
    stmts = _statements(prod, "VerifyRole")
    writes = {}
    for s in stmts:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        for a in actions:
            if a in ("dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"):
                writes.setdefault(a, set()).add(json.dumps(s["Resource"]))
    # Seeding orders is allowed; the audit log, ledger and idempotency tables are never writable.
    for resources in writes.values():
        joined = " ".join(resources)
        for protected in ("Audit", "Ledger", "Idempotency"):
            assert protected not in joined, (protected, joined)


def test_log_and_runtime_access_is_scoped_to_this_environment(prod):
    text = json.dumps(_statements(prod, "VerifyRole"))
    assert "csagent_prod_supervisor-*" in text and "/aws/lambda/csagent-prod-*" in text
    assert "csagent-dev" not in text and "csagent_dev" not in text


def test_invalid_repository_names_are_rejected():
    cfg = get_config("dev")
    app = cdk.App()
    data = DataStack(app, "data", cfg=cfg, env=ENV)
    auth = AuthStack(
        app, "auth", cfg=cfg, orders_table=data.tables["orders"], key=data.key, env=ENV
    )
    for bad in ("no-slash", "a/b/c", "owner/*", "owner/na me"):
        with pytest.raises(ValueError):
            CicdStack(
                app,
                f"c-{abs(hash(bad))}",
                cfg=cfg,
                github_repo=bad,
                user_pool=auth.user_pool,
                key=data.key,
                tables=data.tables,
                env=ENV,
            )
