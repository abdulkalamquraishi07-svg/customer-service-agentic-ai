"""The API's CORS allowlist (preflight + Lambda) is exactly the web origins passed in."""

import aws_cdk as cdk
from aws_cdk.assertions import Template
from config import get_config
from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.data_stack import DataStack
from stacks.network_stack import NetworkStack

ORIGIN = "https://d222.cloudfront.net"


def test_preflight_and_lambdas_use_the_web_origin_allowlist():
    cfg = get_config("prod")
    app = cdk.App()
    env = cdk.Environment(account="111111111111", region="us-east-1")
    network = NetworkStack(app, "net", cfg=cfg, env=env)
    data = DataStack(app, "data", cfg=cfg, env=env)
    auth = AuthStack(
        app, "auth", cfg=cfg, orders_table=data.tables["orders"], key=data.key, env=env
    )
    api = ApiStack(app, "api", cfg=cfg, vpc=network.vpc, app_sg=network.app_sg,
                   user_pool=auth.user_pool, tables=data.tables, key=data.key,
                   web_origins=[ORIGIN], env=env)
    t = Template.from_stack(api)

    fns = [f for f in t.find_resources("AWS::Lambda::Function").values()
           if f["Properties"].get("Handler", "").startswith("bizapi.")]
    assert fns and all(
        f["Properties"]["Environment"]["Variables"]["ALLOWED_ORIGINS"] == ORIGIN for f in fns
    )
    options = [m for m in t.find_resources("AWS::ApiGateway::Method").values()
               if m["Properties"]["HttpMethod"] == "OPTIONS"]
    assert options
    for m in options:
        params = m["Properties"]["Integration"]["IntegrationResponses"][0]["ResponseParameters"]
        assert params["method.response.header.Access-Control-Allow-Origin"] == f"'{ORIGIN}'"
        assert "localhost" not in str(params)
