"""CI/CD identity: GitHub Actions authenticates with short-lived OIDC tokens, never stored keys.

Two roles per environment, both bound to one GitHub *environment* (so GitHub's protection rules
and required reviewers apply) of one repository:
- deploy: may ONLY assume CDK's bootstrap roles (deploy / file-publishing / lookup). CloudFormation
  then acts through CDK's execution role, so a deploy job can create anything the templates
  declare. The control is the GitHub environment (branch + reviewers), not the IAM policy.
- verify: runs smoke tests, canaries and evals against that environment only, with exactly the
  data-plane permissions those scripts use. No IAM, no CloudFormation writes, no other
  environment's resources.
"""

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from config import APP_NAME, EnvConfig, resource_name
from constructs import Construct

OIDC_HOST = "token.actions.githubusercontent.com"
CDK_QUALIFIER = "hnb659fds"  # default bootstrap qualifier (matches the deployed CDKToolkit)


class GithubOidcStack(Stack):
    """Account-level, deployed once: AWS allows one OIDC provider per URL."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.provider = iam.CfnOIDCProvider(
            self,
            "GithubProvider",
            url=f"https://{OIDC_HOST}",
            client_id_list=["sts.amazonaws.com"],
        )
        CfnOutput(self, "ProviderArn", value=self.provider.attr_arn)


class CicdStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        github_repo: str,
        user_pool: cognito.IUserPool,
        key: kms.IKey,
        tables: dict[str, dynamodb.ITable],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if github_repo.count("/") != 1 or any(c in github_repo for c in " *?"):
            raise ValueError("github_repo must look like 'owner/name'")

        provider_arn = f"arn:aws:iam::{self.account}:oidc-provider/{OIDC_HOST}"

        def trust() -> iam.IPrincipal:
            return iam.WebIdentityPrincipal(
                provider_arn,
                conditions={
                    "StringEquals": {
                        f"{OIDC_HOST}:aud": "sts.amazonaws.com",
                        # Exactly one repository and one GitHub environment. No wildcards.
                        f"{OIDC_HOST}:sub": f"repo:{github_repo}:environment:{cfg.name}",
                    }
                },
            )

        # ---------------- deploy ----------------
        self.deploy_role = iam.Role(
            self,
            "DeployRole",
            role_name=resource_name(cfg, "ci-deploy"),
            assumed_by=trust(),
            max_session_duration=Duration.hours(1),
            description=f"GitHub Actions deploy ({cfg.name}): can only assume CDK bootstrap roles",
        )
        self.deploy_role.add_to_policy(
            iam.PolicyStatement(
                sid="AssumeCdkBootstrapRoles",
                actions=["sts:AssumeRole"],
                resources=[
                    f"arn:aws:iam::{self.account}:role/cdk-{CDK_QUALIFIER}-{kind}-role-"
                    f"{self.account}-{self.region}"
                    for kind in ("deploy", "file-publishing", "lookup")
                ],
            )
        )

        # ---------------- verify ----------------
        self.verify_role = iam.Role(
            self,
            "VerifyRole",
            role_name=resource_name(cfg, "ci-verify"),
            assumed_by=trust(),
            max_session_duration=Duration.hours(1),
            description=f"GitHub Actions post-deploy verification ({cfg.name} only)",
        )
        v = self.verify_role
        acct, region = self.account, self.region
        v.add_to_policy(
            iam.PolicyStatement(
                sid="ReadOwnStackOutputs",
                actions=["cloudformation:DescribeStacks"],
                resources=[f"arn:aws:cloudformation:{region}:{acct}:stack/{APP_NAME}-{cfg.name}-*/*"],
            )
        )
        v.add_to_policy(
            iam.PolicyStatement(
                sid="ManageTestUsers",
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminAddUserToGroup",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:AdminDeleteUser",
                ],
                resources=[user_pool.user_pool_arn],
            )
        )
        reads = [t.table_arn for t in tables.values()] + [
            f"{t.table_arn}/index/*" for t in tables.values()
        ]
        v.add_to_policy(
            iam.PolicyStatement(
                sid="ReadTestData",
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                resources=reads,
            )
        )
        v.add_to_policy(
            iam.PolicyStatement(
                sid="SeedAndCleanTestOrders",
                actions=["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"],
                resources=[tables["orders"].table_arn],
            )
        )
        v.add_to_policy(
            iam.PolicyStatement(
                sid="CleanTestReturnsAndTickets",
                actions=["dynamodb:DeleteItem"],
                resources=[tables["returns"].table_arn, tables["tickets"].table_arn],
            )
        )
        key.grant_encrypt_decrypt(v)  # the tables use the customer-managed key
        v.add_to_policy(
            iam.PolicyStatement(
                sid="ReadTurnLogs",
                actions=["logs:FilterLogEvents", "logs:DescribeLogStreams", "logs:GetLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/bedrock-agentcore/runtimes/"
                    f"{APP_NAME}_{cfg.name}_supervisor-*",
                    f"arn:aws:logs:{region}:{acct}:log-group:/aws/lambda/{APP_NAME}-{cfg.name}-*",
                ],
            )
        )
        v.add_to_policy(
            iam.PolicyStatement(
                sid="FindRuntimeAndItsConfig",
                actions=["bedrock-agentcore:ListAgentRuntimes"],
                resources=["*"],  # list calls do not support resource-level permissions
            )
        )
        v.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:GetAgentRuntime"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{region}:{acct}:runtime/"
                    f"{APP_NAME}_{cfg.name}_supervisor-*"
                ],
            )
        )
        # The rate-limit check needs nothing extra: its counters expire via TTL and its test
        # customers are cleaned with the permissions above.
        v.add_to_policy(
            iam.PolicyStatement(
                sid="CanaryAndIngestion",
                actions=[
                    "bedrock:ApplyGuardrail",
                    "bedrock:Retrieve",
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                ],
                resources=[
                    f"arn:aws:bedrock:{region}:{acct}:guardrail/*",
                    f"arn:aws:bedrock:{region}:{acct}:knowledge-base/*",
                ],
            )
        )

        CfnOutput(self, "DeployRoleArn", value=self.deploy_role.role_arn)
        CfnOutput(self, "VerifyRoleArn", value=self.verify_role.role_arn)
