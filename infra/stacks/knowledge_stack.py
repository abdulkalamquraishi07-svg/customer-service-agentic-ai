"""Knowledge base: S3 policy documents -> Bedrock Knowledge Base -> Aurora PostgreSQL (pgvector).

Aurora Serverless v2 with the Data API is used instead of OpenSearch Serverless, whose minimum
capacity charge dominates cost at this scale (ADR 0004). Bedrock reaches the database through the
Data API, so the cluster stays in isolated subnets with no inbound access.
"""

from pathlib import Path

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from config import EnvConfig, resource_name
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]
EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
DATABASE = "knowledge"
TABLE = "bedrock_integration.bedrock_kb"


class KnowledgeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        key: kms.IKey,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY
        retention = (
            logs.RetentionDays.ONE_YEAR
            if cfg.log_retention_days >= 365
            else logs.RetentionDays.ONE_MONTH
        )

        # ---------- documents ----------
        self.bucket = s3.Bucket(
            self,
            "Documents",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,  # policy history + recovery from bad uploads
            removal_policy=removal,
            auto_delete_objects=not cfg.retain_data,
        )
        s3deploy.BucketDeployment(
            self,
            "PolicyDocuments",
            sources=[s3deploy.Source.asset(str(ROOT / "data" / "knowledge"))],
            destination_bucket=self.bucket,
            destination_key_prefix="policies/",
            prune=True,
        )

        # ---------- vector store ----------
        db_sg = ec2.SecurityGroup(
            self,
            "DbSg",
            vpc=vpc,
            description="Knowledge DB: no inbound; accessed only through the RDS Data API",
            allow_all_outbound=False,
        )
        self.cluster = rds.DatabaseCluster(
            self,
            "VectorDb",
            cluster_identifier=resource_name(cfg, "knowledge-db"),
            engine=rds.DatabaseClusterEngine.aurora_postgres(
                version=rds.AuroraPostgresEngineVersion.of("16.15", "16")
            ),
            writer=rds.ClusterInstance.serverless_v2("writer"),
            serverless_v2_min_capacity=max(cfg.kb_min_acu, 0.5),  # overridden below for 0
            serverless_v2_max_capacity=cfg.kb_max_acu,
            default_database_name=DATABASE,
            credentials=rds.Credentials.from_generated_secret(
                "kbadmin", secret_name=resource_name(cfg, "knowledge-db-admin")
            ),
            enable_data_api=True,
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
            security_groups=[db_sg],
            storage_encrypted=True,
            storage_encryption_key=key,
            iam_authentication=True,
            backup=rds.BackupProps(retention=Duration.days(7 if cfg.retain_data else 1)),
            deletion_protection=cfg.deletion_protection,
            removal_policy=RemovalPolicy.SNAPSHOT if cfg.retain_data else RemovalPolicy.DESTROY,
            cloudwatch_logs_exports=["postgresql"],
            cloudwatch_logs_retention=retention,
        )
        if cfg.kb_min_acu == 0:
            # Scale-to-zero (auto-pause). Not exposed by this CDK version's L2/L1 types.
            cfn_cluster = self.cluster.node.default_child
            cfn_cluster.add_property_override(  # type: ignore[union-attr]
                "ServerlessV2ScalingConfiguration",
                {
                    "MinCapacity": 0,
                    "MaxCapacity": cfg.kb_max_acu,
                    "SecondsUntilAutoPause": cfg.kb_auto_pause_seconds,
                },
            )

        # Dedicated, least-privilege DB user for Bedrock (never the master user).
        self.kb_user_secret = secretsmanager.Secret(
            self,
            "KbUserSecret",
            secret_name=resource_name(cfg, "knowledge-db-bedrock-user"),
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "bedrock_user"}',  # noqa: S106 - username only
                generate_string_key="password",
                exclude_punctuation=True,  # setup Lambda requires alphanumeric only
                password_length=32,
            ),
        )

        # ---------- schema setup (custom resource via Data API) ----------
        setup_fn = _lambda.Function(
            self,
            "KbSetup",
            function_name=resource_name(cfg, "kb-setup"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="kb_setup.handler.handler",
            code=_lambda.Code.from_asset(str(ROOT / "services" / "kb_setup" / "src")),
            timeout=Duration.minutes(10),
            log_group=logs.LogGroup(
                self,
                "KbSetupLogs",
                log_group_name=f"/aws/lambda/{resource_name(cfg, 'kb-setup')}",
                retention=retention,
                removal_policy=removal,
            ),
        )
        self.cluster.grant_data_api_access(setup_fn)  # includes the master secret read
        self.kb_user_secret.grant_read(setup_fn)
        provider = cr.Provider(self, "KbSetupProvider", on_event_handler=setup_fn)
        setup = CustomResource(
            self,
            "VectorStoreSetup",
            service_token=provider.service_token,
            properties={
                "ClusterArn": self.cluster.cluster_arn,
                "MasterSecretArn": self.cluster.secret.secret_arn,  # type: ignore[union-attr]
                "KbUserSecretArn": self.kb_user_secret.secret_arn,
                "Database": DATABASE,
                "Dimensions": str(EMBEDDING_DIMENSIONS),
                "SchemaVersion": "1",  # bump to re-run setup after a schema change
            },
        )
        setup.node.add_dependency(self.cluster)

        # ---------- Bedrock knowledge base ----------
        embedding_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{EMBEDDING_MODEL}"
        kb_arn_pattern = f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
        kb_role = iam.Role(
            self,
            "KbRole",
            role_name=resource_name(cfg, "kb-role"),
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": kb_arn_pattern},
                },
            ),
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(actions=["bedrock:InvokeModel"], resources=[embedding_arn])
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["rds:DescribeDBClusters"], resources=[self.cluster.cluster_arn]
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["rds-data:ExecuteStatement", "rds-data:BatchExecuteStatement"],
                resources=[self.cluster.cluster_arn],
            )
        )
        self.kb_user_secret.grant_read(kb_role)
        self.bucket.grant_read(kb_role, "policies/*")

        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=resource_name(cfg, "policies"),
            description="Company policies, FAQs and support documentation",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=EMBEDDING_DIMENSIONS
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="RDS",
                rds_configuration=bedrock.CfnKnowledgeBase.RdsConfigurationProperty(
                    resource_arn=self.cluster.cluster_arn,
                    credentials_secret_arn=self.kb_user_secret.secret_arn,
                    database_name=DATABASE,
                    table_name=TABLE,
                    field_mapping=bedrock.CfnKnowledgeBase.RdsFieldMappingProperty(
                        primary_key_field="id",
                        vector_field="embedding",
                        text_field="chunks",
                        metadata_field="metadata",
                    ),
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(setup)
        self.knowledge_base.node.add_dependency(kb_role)

        self.data_source = bedrock.CfnDataSource(
            self,
            "PolicySource",
            name=resource_name(cfg, "policy-docs"),
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            data_deletion_policy="DELETE",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.bucket.bucket_arn,
                    inclusion_prefixes=["policies/"],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="FIXED_SIZE",
                    fixed_size_chunking_configuration=bedrock.CfnDataSource.FixedSizeChunkingConfigurationProperty(
                        max_tokens=300, overlap_percentage=15
                    ),
                )
            ),
        )

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=self.data_source.attr_data_source_id)
        CfnOutput(self, "DocumentsBucket", value=self.bucket.bucket_name)
