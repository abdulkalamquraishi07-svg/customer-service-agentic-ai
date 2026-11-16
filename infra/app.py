#!/usr/bin/env python3
"""CDK entry point. Usage: cdk synth -c env=dev

Build first: `python scripts/build_agent.py` (agent runtime zip) and `npm run build` in web/
(web app). Stacks: network, data, auth, web-hosting, api, knowledge, agent, web-content.
"""

import os
from pathlib import Path

import aws_cdk as cdk
from config import get_config, resource_name
from stacks.agent_stack import AgentStack
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.cicd_stack import CicdStack, GithubOidcStack
from stacks.data_stack import DataStack
from stacks.knowledge_stack import KnowledgeStack
from stacks.network_stack import NetworkStack
from stacks.web_stack import WebContentStack, WebHostingStack

ROOT = Path(__file__).resolve().parents[1]
AGENT_ZIP = ROOT / "build" / "agent" / "agent.zip"
WEB_DIST = ROOT / "web" / "dist"

app = cdk.App()
cfg = get_config(app.node.try_get_context("env") or "dev")

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

BUILD_HINTS = ((AGENT_ZIP, "python scripts/build_agent.py"), (WEB_DIST, "cd web; npm run build"))
for path, hint in BUILD_HINTS:
    if not path.exists():
        raise SystemExit(f"Missing {path}. Run: {hint}")

network = NetworkStack(app, resource_name(cfg, "network"), cfg=cfg, env=env)
data = DataStack(app, resource_name(cfg, "data"), cfg=cfg, env=env)
auth = AuthStack(
    app,
    resource_name(cfg, "auth"),
    cfg=cfg,
    orders_table=data.tables["orders"],
    key=data.key,
    env=env,
)
web_hosting = WebHostingStack(app, resource_name(cfg, "web-hosting"), cfg=cfg, env=env)

web_origins = [web_hosting.origin]
if cfg.allow_localhost_origin:
    web_origins.append("http://localhost:5173")

api = ApiStack(
    app,
    resource_name(cfg, "api"),
    cfg=cfg,
    vpc=network.vpc,
    app_sg=network.app_sg,
    user_pool=auth.user_pool,
    tables=data.tables,
    key=data.key,
    web_origins=web_origins,
    alarm_email=app.node.try_get_context("alarm_email"),
    env=env,
)
knowledge = KnowledgeStack(
    app, resource_name(cfg, "knowledge"), cfg=cfg, vpc=network.vpc, key=data.key, env=env
)
agent = AgentStack(
    app,
    resource_name(cfg, "agent"),
    cfg=cfg,
    package_zip=str(AGENT_ZIP),
    api_url=api.api_url,
    user_pool_id=auth.user_pool.user_pool_id,
    web_client_id=auth.web_client.user_pool_client_id,
    knowledge_base_id=knowledge.knowledge_base.attr_knowledge_base_id,
    knowledge_base_arn=knowledge.knowledge_base.attr_knowledge_base_arn,
    ratelimit_table=data.ratelimit,
    alarm_topic=api.backend.alarm_topic,
    env=env,
)
web_content = WebContentStack(
    app,
    resource_name(cfg, "web-content"),
    hosting=web_hosting,
    cfg=cfg,
    dist_dir=WEB_DIST,
    env=env,
)
# Reads client settings from SSM parameters written by these stacks.
for producer in (auth, api, agent):
    web_content.add_dependency(producer)

# CI/CD identity is opt-in and deployed by an administrator, never by the pipeline itself:
#   cdk deploy csagent-github-oidc -c github_oidc=true            (once per AWS account)
#   cdk deploy csagent-<env>-cicd -c env=<env> -c github_repo=owner/name
if app.node.try_get_context("github_oidc"):
    GithubOidcStack(app, "csagent-github-oidc", env=env)
github_repo = app.node.try_get_context("github_repo")
if github_repo:
    CicdStack(
        app,
        resource_name(cfg, "cicd"),
        cfg=cfg,
        github_repo=github_repo,
        user_pool=auth.user_pool,
        key=data.key,
        tables=data.tables,
        env=env,
    )

for key, value in {"app": "csagent", "environment": cfg.name, "managed-by": "cdk"}.items():
    cdk.Tags.of(app).add(key, value)

app.synth()
