"""Web app hosting: private S3 bucket behind CloudFront (Origin Access Control).

Split in two stacks to avoid a dependency cycle: the API needs the CloudFront domain for its CORS
allowlist (WebHostingStack, deployed first), and the app needs the API/agent endpoints in its
runtime config (WebContentStack, deployed last).
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_ssm as ssm
from config import EnvConfig, client_param, resource_name
from constructs import Construct

CLIENT_KEYS = ("userPoolId", "userPoolClientId", "apiUrl", "agentRuntimeArn")

REGION_ENDPOINTS = (
    "https://cognito-idp.{region}.amazonaws.com",
    "https://bedrock-agentcore.{region}.amazonaws.com",
)


def content_security_policy(region: str, api_origin: str) -> str:
    connect = " ".join([*(e.format(region=region) for e in REGION_ENDPOINTS), api_origin])
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src 'self' {connect}",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ]
    )


class WebHostingStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY,
            auto_delete_objects=not cfg.retain_data,
        )
        # The API is created after this stack (it needs our domain for CORS), so connect-src
        # allows API Gateway in this region by wildcard rather than by exact API id.
        api_origin = f"https://*.execute-api.{self.region}.amazonaws.com"
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            response_headers_policy_name=resource_name(cfg, "web-security-headers"),
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=content_security_policy(self.region, api_origin),
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(730),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
                    override=True,
                ),
            ),
        )
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=resource_name(cfg, "web"),
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            # Client-side routes (/ops, /chat) resolve to the SPA entry point.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=code,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                )
                for code in (403, 404)
            ],
        )
        self.origin = f"https://{self.distribution.distribution_domain_name}"
        CfnOutput(self, "WebUrl", value=self.origin)


class WebContentStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        hosting: WebHostingStack,
        cfg: EnvConfig,
        dist_dir: Path,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        # Values come from SSM parameters published by the auth, api and agent stacks. Direct
        # cross-stack references inside BucketDeployment JSON are not exported by CDK (deploy
        # failed with unresolved dependencies), and Fn::ImportValue is rejected there.
        # value_for_string_parameter resolves to a Ref, which BucketDeployment accepts.
        runtime_config: dict[str, object] = {
            "environment": cfg.name,
            "region": self.region,
            "selfSignUp": cfg.self_sign_up,  # UI hint only; Cognito enforces the setting
        }
        for key in CLIENT_KEYS:
            runtime_config[key] = ssm.StringParameter.value_for_string_parameter(
                self, client_param(cfg, key)
            )
        # The same build serves every environment; endpoints come from /config.json.
        s3deploy.BucketDeployment(
            self,
            "Deploy",
            destination_bucket=hosting.bucket,
            sources=[
                s3deploy.Source.asset(str(dist_dir)),
                s3deploy.Source.json_data("config.json", runtime_config),
            ],
            distribution=hosting.distribution,
            distribution_paths=["/index.html", "/config.json"],
            prune=True,
            memory_limit=512,
        )
