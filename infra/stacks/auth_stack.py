"""Authentication: Cognito user pool with groups for customers, support agents, approvers.

With cfg.self_sign_up, anyone can register with an email address (verified by a code). A
post-confirmation trigger puts them in `customers` only; staff groups stay admin-assigned.
"""

from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from config import EnvConfig, client_param, resource_name
from constructs import Construct

BUILD_DIR = str(Path(__file__).resolve().parents[2] / "build" / "lambda")

GROUPS = {
    "customers": "End customers using the chat app",
    "support-agents": "Human support representatives (ops console)",
    "approvers": "Senior reps allowed to approve refunds",
}


class AuthStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        orders_table: dynamodb.ITable | None = None,
        key: kms.IKey | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if cfg.seed_demo_orders_on_signup and (orders_table is None or key is None):
            raise ValueError("seed_demo_orders_on_signup needs orders_table and key")

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=resource_name(cfg, "users"),
            self_sign_up_enabled=cfg.self_sign_up,
            # Sign-up emails use the Cognito default sender (about 50 emails/day). Production
            # would send through SES with a verified domain.
            user_verification=cognito.UserVerificationConfig(
                email_subject="Your verification code",
                email_body="Your verification code is {####}",
                email_style=cognito.VerificationEmailStyle.CODE,
            ),
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            deletion_protection=cfg.deletion_protection,
            removal_policy=RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY,
        )

        self.web_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name=resource_name(cfg, "web"),
            generate_secret=False,  # public SPA client; uses SRP + PKCE
            auth_flows=cognito.AuthFlow(user_srp=True),
            prevent_user_existence_errors=True,
            access_token_validity=Duration.minutes(30),
            id_token_validity=Duration.minutes(30),
            refresh_token_validity=Duration.days(7),
            enable_token_revocation=True,
        )

        for group, description in GROUPS.items():
            cognito.CfnUserPoolGroup(
                self,
                f"Group-{group}",
                user_pool_id=self.user_pool.user_pool_id,
                group_name=group,
                description=description,
            )

        if cfg.self_sign_up:
            self._add_signup_trigger(cfg, orders_table, key)

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "WebClientId", value=self.web_client.user_pool_client_id)
        for key, value in (
            ("userPoolId", self.user_pool.user_pool_id),
            ("userPoolClientId", self.web_client.user_pool_client_id),
        ):
            ssm.StringParameter(
                self, f"Client-{key}", parameter_name=client_param(cfg, key), string_value=value
            )

    def _add_signup_trigger(
        self, cfg: EnvConfig, orders_table: dynamodb.ITable | None, key: kms.IKey | None
    ) -> None:
        environment = {"APP_ENV": cfg.name}
        if cfg.seed_demo_orders_on_signup and orders_table is not None:
            environment["DEMO_ORDERS_TABLE"] = orders_table.table_name
        name = resource_name(cfg, "signup")
        removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY
        function = _lambda.Function(
            self,
            "SignupTrigger",
            function_name=name,
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            handler="bizapi.signup.handler",
            code=_lambda.Code.from_asset(BUILD_DIR),
            timeout=Duration.seconds(5),  # Cognito gives triggers 5 seconds
            memory_size=256,
            environment=environment,
            tracing=_lambda.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "SignupTriggerLogs",
                log_group_name=f"/aws/lambda/{name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=removal,
            ),
        )
        self.user_pool.add_trigger(cognito.UserPoolOperation.POST_CONFIRMATION, function)
        # A separate policy (not the function's default one) avoids a dependency cycle:
        # pool -> trigger function, and policy -> pool ARN.
        iam.Policy(
            self,
            "SignupTriggerGroups",
            roles=[function.role] if function.role else [],
            statements=[
                iam.PolicyStatement(
                    actions=["cognito-idp:AdminAddUserToGroup"],
                    resources=[self.user_pool.user_pool_arn],
                )
            ],
        )
        if cfg.seed_demo_orders_on_signup and orders_table is not None and key is not None:
            orders_table.grant(function, "dynamodb:PutItem", "dynamodb:BatchWriteItem")
            key.grant_encrypt_decrypt(function)  # orders table uses the CMK
