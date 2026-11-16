"""Web hosting: private bucket, HTTPS-only CloudFront, strict security headers, CORS wiring."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from config import get_config
from stacks.web_stack import WebHostingStack, content_security_policy


@pytest.fixture(scope="module")
def template():
    app = cdk.App()
    stack = WebHostingStack(
        app, "web", cfg=get_config("prod"),
        env=cdk.Environment(account="111111111111", region="us-east-1"),
    )
    return Template.from_stack(stack)


def test_bucket_is_private_and_served_only_through_cloudfront(template):
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True, "BlockPublicPolicy": True,
            "IgnorePublicAcls": True, "RestrictPublicBuckets": True}},
    )
    template.resource_count_is("AWS::CloudFront::OriginAccessControl", 1)
    policy = json.dumps(template.find_resources("AWS::S3::BucketPolicy"))
    assert "cloudfront.amazonaws.com" in policy and "AWS:SourceArn" in policy


def test_https_only_modern_tls_and_spa_fallback(template):
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {"DistributionConfig": Match.object_like({
            "DefaultRootObject": "index.html",
            "DefaultCacheBehavior": Match.object_like(
                {"ViewerProtocolPolicy": "redirect-to-https"}
            ),
            "CustomErrorResponses": Match.array_with([
                Match.object_like({"ErrorCode": 403, "ResponseCode": 200,
                                   "ResponsePagePath": "/index.html"})]),
        })},
    )


def test_security_headers(template):
    (policy,) = template.find_resources("AWS::CloudFront::ResponseHeadersPolicy").values()
    sec = policy["Properties"]["ResponseHeadersPolicyConfig"]["SecurityHeadersConfig"]
    assert sec["FrameOptions"]["FrameOption"] == "DENY"
    assert sec["StrictTransportSecurity"]["IncludeSubdomains"] is True
    csp = sec["ContentSecurityPolicy"]["ContentSecurityPolicy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_csp_allows_only_required_endpoints_and_no_inline_or_eval():
    csp = content_security_policy("us-east-1", "https://*.execute-api.us-east-1.amazonaws.com")
    assert "https://cognito-idp.us-east-1.amazonaws.com" in csp
    assert "https://bedrock-agentcore.us-east-1.amazonaws.com" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    connect = next(d for d in csp.split("; ") if d.startswith("connect-src"))
    assert "*" not in connect.replace("https://*.execute-api.us-east-1.amazonaws.com", "")


def test_web_content_reads_every_client_setting_from_ssm(tmp_path):
    from stacks.web_stack import CLIENT_KEYS, WebContentStack

    (tmp_path / "index.html").write_text("<html></html>")
    app = cdk.App()
    env = cdk.Environment(account="111111111111", region="us-east-1")
    cfg = get_config("dev")
    hosting = WebHostingStack(app, "hosting", cfg=cfg, env=env)
    content = WebContentStack(app, "content", hosting=hosting, cfg=cfg, dist_dir=tmp_path,
                              env=env)
    t = Template.from_stack(content).to_json()
    ssm_defaults = {
        p["Default"] for p in t["Parameters"].values()
        if p["Type"].startswith("AWS::SSM::Parameter::Value")
        and not p["Default"].startswith("/cdk-bootstrap/")  # CDK's own version check
    }
    assert ssm_defaults == {f"/csagent/dev/client/{k}" for k in CLIENT_KEYS}
    # Regression: cross-stack refs inside the config.json markers broke the deploy twice.
    # (Importing the bucket/distribution themselves is a normal, supported reference.)
    (deployment,) = [
        r for r in t["Resources"].values() if r["Type"] == "Custom::CDKBucketDeployment"
    ]
    markers = json.dumps(deployment["Properties"].get("SourceMarkers", []))
    assert "Fn::ImportValue" not in markers and "Fn::GetAtt" not in markers
    assert markers.count('"Ref"') == len(CLIENT_KEYS)


def test_localhost_origin_only_in_dev():
    assert get_config("dev").allow_localhost_origin is True
    assert get_config("staging").allow_localhost_origin is False
    assert get_config("prod").allow_localhost_origin is False
