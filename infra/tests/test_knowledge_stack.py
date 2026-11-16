"""Knowledge base infrastructure: cost, isolation and least-privilege properties."""

import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from config import get_config
from stacks.data_stack import DataStack
from stacks.knowledge_stack import KnowledgeStack
from stacks.network_stack import NetworkStack


def _build(env_name: str) -> Template:
    cfg = get_config(env_name)
    app = cdk.App()
    env = cdk.Environment(account="111111111111", region="us-east-1")
    network = NetworkStack(app, "net", cfg=cfg, env=env)
    data = DataStack(app, "data", cfg=cfg, env=env)
    kb = KnowledgeStack(app, "kb", cfg=cfg, vpc=network.vpc, key=data.key, env=env)
    return Template.from_stack(kb)


@pytest.fixture(scope="module")
def dev():
    return _build("dev")


@pytest.fixture(scope="module")
def prod():
    return _build("prod")


def test_no_opensearch_anywhere(dev, prod):
    for t in (dev, prod):
        assert not t.find_resources("AWS::OpenSearchServerless::Collection")
        assert "opensearch" not in json.dumps(t.to_json()).lower()


def test_dev_scales_to_zero_with_auto_pause(dev):
    dev.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "ServerlessV2ScalingConfiguration": {
                "MinCapacity": 0,
                "MaxCapacity": 4,
                "SecondsUntilAutoPause": 600,
            }
        },
    )


def test_prod_keeps_warm_minimum_capacity(prod):
    clusters = prod.find_resources("AWS::RDS::DBCluster")
    (cluster,) = clusters.values()
    scaling = cluster["Properties"]["ServerlessV2ScalingConfiguration"]
    assert scaling["MinCapacity"] == 0.5 and "SecondsUntilAutoPause" not in scaling


def test_cluster_is_private_encrypted_data_api_and_protected_in_prod(prod):
    prod.has_resource_properties(
        "AWS::RDS::DBCluster",
        {
            "EnableHttpEndpoint": True,
            "StorageEncrypted": True,
            "KmsKeyId": Match.any_value(),
            "DeletionProtection": True,
            "Engine": "aurora-postgresql",
            "EngineVersion": "16.15",
        },
    )
    (cluster,) = prod.find_resources("AWS::RDS::DBCluster").values()
    assert cluster["DeletionPolicy"] == "Snapshot"
    prod.has_resource_properties("AWS::RDS::DBInstance", {"PubliclyAccessible": False})


def test_db_security_group_allows_no_inbound(dev):
    groups = dev.find_resources("AWS::EC2::SecurityGroup")
    db = [g for g in groups.values() if "Knowledge DB" in g["Properties"]["GroupDescription"]]
    assert db and "SecurityGroupIngress" not in db[0]["Properties"]
    assert not dev.find_resources("AWS::EC2::SecurityGroupIngress")


def test_kb_uses_rds_with_titan_v2_and_dedicated_user(dev):
    dev.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "KnowledgeBaseConfiguration": {
                "Type": "VECTOR",
                "VectorKnowledgeBaseConfiguration": Match.object_like(
                    {
                        "EmbeddingModelArn": Match.string_like_regexp(
                            "amazon.titan-embed-text-v2:0"
                        ),
                    }
                ),
            },
            "StorageConfiguration": {
                "Type": "RDS",
                "RdsConfiguration": Match.object_like(
                    {
                        "TableName": "bedrock_integration.bedrock_kb",
                        "CredentialsSecretArn": {"Ref": Match.string_like_regexp("KbUserSecret")},
                    }
                ),
            },
        },
    )


def test_kb_role_is_least_privilege_and_confused_deputy_safe(dev):
    roles = dev.find_resources("AWS::IAM::Role")
    kb_role = next(r for k, r in roles.items() if k.startswith("KbRole"))
    trust = json.dumps(kb_role["Properties"]["AssumeRolePolicyDocument"])
    assert "aws:SourceAccount" in trust and "aws:SourceArn" in trust

    policies = [
        p
        for p in dev.find_resources("AWS::IAM::Policy").values()
        if "KbRole" in json.dumps(p["Properties"]["Roles"])
    ]
    text = json.dumps(policies)
    assert "bedrock:InvokeModel" in text and "rds-data:ExecuteStatement" in text
    assert '"*"' not in text.replace('"Resource": "*"', "__")  # no wildcard actions
    assert "rds-data:*" not in text and "s3:*" not in text and "bedrock:*" not in text


def test_documents_bucket_is_private_versioned_tls_only(prod):
    prod.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "VersioningConfiguration": {"Status": "Enabled"},
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    prod.has_resource_properties(
        "AWS::S3::BucketPolicy",
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


def test_chunking_and_source_prefix(dev):
    dev.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "DataSourceConfiguration": Match.object_like(
                {"S3Configuration": Match.object_like({"InclusionPrefixes": ["policies/"]})}
            ),
            "VectorIngestionConfiguration": {
                "ChunkingConfiguration": {
                    "ChunkingStrategy": "FIXED_SIZE",
                    "FixedSizeChunkingConfiguration": {"MaxTokens": 300, "OverlapPercentage": 15},
                }
            },
        },
    )
