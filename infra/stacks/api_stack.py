"""Edge: API Gateway (Cognito authorizer, validation, throttling) + WAF."""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_wafv2 as wafv2
from config import EnvConfig, client_param, resource_name
from constructs import Construct

from stacks.business_backend import BusinessBackend

HANDLERS_DIR = str(Path(__file__).resolve().parents[2] / "services" / "chat_api" / "src")


class ApiStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        app_sg: ec2.ISecurityGroup,
        user_pool: cognito.IUserPool,
        tables: dict[str, dynamodb.ITable],
        key: kms.IKey,
        web_origins: list[str],
        alarm_email: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY

        retention = (
            logs.RetentionDays.ONE_YEAR
            if cfg.log_retention_days >= 365
            else logs.RetentionDays.ONE_MONTH
        )

        vpc_kwargs = (
            {
                "vpc": vpc,
                "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                "security_groups": [app_sg],
            }
            if cfg.lambdas_in_vpc
            else {}
        )

        def fn(id_: str, handler: str) -> _lambda.Function:
            log_group = logs.LogGroup(
                self,
                f"{id_}Logs",
                log_group_name=f"/aws/lambda/{resource_name(cfg, id_.lower())}",
                retention=retention,
                removal_policy=removal,
            )
            return _lambda.Function(
                self,
                id_,
                function_name=resource_name(cfg, id_.lower()),
                runtime=_lambda.Runtime.PYTHON_3_12,
                architecture=_lambda.Architecture.ARM_64,
                handler=handler,
                code=_lambda.Code.from_asset(HANDLERS_DIR),
                timeout=Duration.seconds(10),
                memory_size=256,
                **vpc_kwargs,
                environment={"APP_ENV": cfg.name},
                tracing=_lambda.Tracing.ACTIVE,
                log_group=log_group,
            )

        health_fn = fn("Health", "handlers.edge.health")
        whoami_fn = fn("Whoami", "handlers.edge.whoami")

        access_logs = logs.LogGroup(
            self,
            "AccessLogs",
            retention=retention,
            removal_policy=removal,
        )

        api = apigw.RestApi(
            self,
            "Api",
            rest_api_name=resource_name(cfg, "api"),
            description="Customer service platform edge API",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            deploy_options=apigw.StageOptions(
                stage_name=cfg.name,
                throttling_rate_limit=cfg.api_rate_limit,
                throttling_burst_limit=cfg.api_burst_limit,
                tracing_enabled=True,
                metrics_enabled=True,
                logging_level=apigw.MethodLoggingLevel.ERROR,
                data_trace_enabled=False,  # never log request/response bodies (PII)
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False,
                    http_method=True,
                    ip=True,
                    protocol=True,
                    request_time=True,
                    resource_path=True,
                    response_length=True,
                    status=True,
                    user=False,
                ),
            ),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=web_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
            ),
        )

        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self,
            "Authorizer",
            cognito_user_pools=[user_pool],
            results_cache_ttl=Duration.minutes(5),
        )

        # Reject malformed requests before they reach compute.
        validator = apigw.RequestValidator(
            self,
            "Validator",
            rest_api=api,
            validate_request_body=True,
            validate_request_parameters=True,
        )

        # GET /health (public liveness)
        api.root.add_resource("health").add_method(
            "GET",
            apigw.LambdaIntegration(health_fn),
            authorization_type=apigw.AuthorizationType.NONE,
        )

        # GET /v1/whoami (authenticated)
        v1 = api.root.add_resource("v1")
        v1.add_resource("whoami").add_method(
            "GET",
            apigw.LambdaIntegration(whoami_fn),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
            request_validator=validator,
        )

        # ---- business API ----
        backend = BusinessBackend(
            self,
            "Backend",
            cfg=cfg,
            vpc=vpc,
            app_sg=app_sg,
            tables=tables,
            key=key,
            retention=retention,
            removal=removal,
            alarm_email=alarm_email,
            web_origins=web_origins,
        )
        self.backend = backend

        def model(id_: str, props: dict, required: list[str]) -> apigw.Model:
            return apigw.Model(
                self,
                id_,
                rest_api=api,
                content_type="application/json",
                schema=apigw.JsonSchema(
                    schema=apigw.JsonSchemaVersion.DRAFT4,
                    type=apigw.JsonSchemaType.OBJECT,
                    required=required,
                    properties=props,
                    additional_properties=False,  # unknown fields (e.g. customer_id) rejected
                ),
            )

        def string(max_len: int, min_len: int = 1) -> apigw.JsonSchema:
            return apigw.JsonSchema(
                type=apigw.JsonSchemaType.STRING, min_length=min_len, max_length=max_len
            )

        return_model = model(
            "ReturnModel",
            {"order_id": string(64), "reason": string(1000)},
            ["order_id", "reason"],
        )
        ticket_model = model(
            "TicketModel",
            {
                "category": string(32),
                "subject": string(200),
                "description": string(4000),
                "related_order_id": string(64),
            },
            ["category", "subject", "description"],
        )
        escalate_model = model("EscalateModel", {"summary": string(4000)}, ["summary"])
        decision_model = model(
            "DecisionModel",
            {
                "approve": apigw.JsonSchema(type=apigw.JsonSchemaType.BOOLEAN),
                "note": string(500, min_len=0),
            },
            ["approve"],
        )

        def route(
            path: str,
            method: str,
            function: _lambda.IFunction,
            *,
            body_model: apigw.Model | None = None,
            idempotent: bool = False,
        ) -> None:
            resource = v1
            for part in path.strip("/").split("/"):
                resource = resource.get_resource(part) or resource.add_resource(part)
            resource.add_method(
                method,
                apigw.LambdaIntegration(function),
                authorizer=authorizer,
                authorization_type=apigw.AuthorizationType.COGNITO,
                request_validator=validator,
                request_models={"application/json": body_model} if body_model else None,
                request_parameters=(
                    {"method.request.header.Idempotency-Key": True} if idempotent else None
                ),
            )

        customer, ops = backend.customer_fn, backend.ops_fn
        route("orders", "GET", customer)
        route("orders/{order_id}", "GET", customer)
        route("orders/{order_id}/shipment", "GET", customer)
        route("orders/{order_id}/return-eligibility", "GET", customer)
        route("returns", "POST", customer, body_model=return_model, idempotent=True)
        route("returns/{return_id}", "GET", customer)
        route("tickets", "POST", customer, body_model=ticket_model, idempotent=True)
        route("tickets/{ticket_id}/escalate", "POST", customer, body_model=escalate_model)
        route("ops/approvals", "GET", ops)
        route("ops/tickets", "GET", ops)
        route(
            "ops/returns/{customer_id}/{return_id}/decision",
            "POST",
            ops,
            body_model=decision_model,
        )
        route("ops/tickets/{customer_id}/{ticket_id}/resolve", "POST", ops)

        server_errors = cloudwatch.Alarm(
            self,
            "Api5xx",
            alarm_name=resource_name(cfg, "api-5xx"),
            alarm_description="API Gateway returned 5xx responses",
            metric=api.metric_server_error(period=Duration.minutes(5), statistic="Sum"),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        server_errors.add_alarm_action(cw_actions.SnsAction(backend.alarm_topic))

        # Usage plan: per-stage throttling and daily quota.
        plan = api.add_usage_plan(
            "UsagePlan",
            name=resource_name(cfg, "usage"),
            throttle=apigw.ThrottleSettings(
                rate_limit=cfg.api_rate_limit, burst_limit=cfg.api_burst_limit
            ),
            quota=apigw.QuotaSettings(
                limit=cfg.api_rate_limit * 86400 // 4, period=apigw.Period.DAY
            ),
        )
        plan.add_api_stage(stage=api.deployment_stage)

        # WAF: AWS managed rules + per-IP rate limiting.
        def managed(name: str, priority: int) -> wafv2.CfnWebACL.RuleProperty:
            return wafv2.CfnWebACL.RuleProperty(
                name=name,
                priority=priority,
                override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                statement=wafv2.CfnWebACL.StatementProperty(
                    managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                        vendor_name="AWS", name=name
                    )
                ),
                visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                    cloud_watch_metrics_enabled=True,
                    metric_name=name,
                    sampled_requests_enabled=True,
                ),
            )

        web_acl = wafv2.CfnWebACL(
            self,
            "WebAcl",
            name=resource_name(cfg, "waf"),
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name=resource_name(cfg, "waf"),
                sampled_requests_enabled=True,
            ),
            rules=[
                managed("AWSManagedRulesCommonRuleSet", 10),
                managed("AWSManagedRulesKnownBadInputsRuleSet", 20),
                managed("AWSManagedRulesAmazonIpReputationList", 30),
                wafv2.CfnWebACL.RuleProperty(
                    name="PerIpRateLimit",
                    priority=40,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=cfg.waf_rate_limit_per_5min, aggregate_key_type="IP"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="PerIpRateLimit",
                        sampled_requests_enabled=True,
                    ),
                ),
            ],
        )
        wafv2.CfnWebACLAssociation(
            self,
            "WebAclAssociation",
            resource_arn=api.deployment_stage.stage_arn,
            web_acl_arn=web_acl.attr_arn,
        )

        self.api_url = api.url
        CfnOutput(self, "ApiUrl", value=api.url)
        ssm.StringParameter(
            self, "Client-apiUrl", parameter_name=client_param(cfg, "apiUrl"), string_value=api.url
        )
