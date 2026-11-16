"""Business backend: Lambdas, refund queue + DLQ, scheduler and alarms.

Each function has its own role with only the table permissions it needs.
"""

from pathlib import Path

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from config import EnvConfig, resource_name
from constructs import Construct

BUILD_DIR = str(Path(__file__).resolve().parents[2] / "build" / "lambda")


class BusinessBackend(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        vpc: ec2.IVpc,
        app_sg: ec2.ISecurityGroup,
        tables: dict[str, dynamodb.ITable],
        key: kms.IKey,
        retention: logs.RetentionDays,
        removal: RemovalPolicy,
        web_origins: list[str],
        alarm_email: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id)

        # ---- queues ----
        self.dlq = sqs.Queue(
            self,
            "RefundDlq",
            queue_name=resource_name(cfg, "refund-dlq"),
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
        )
        self.queue = sqs.Queue(
            self,
            "RefundQueue",
            queue_name=resource_name(cfg, "refund-queue"),
            # >= 6x the worker timeout, as recommended for Lambda event sources.
            visibility_timeout=Duration.seconds(180),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=self.dlq),
        )

        env = {
            "APP_ENV": cfg.name,
            "ORDERS_TABLE": tables["orders"].table_name,
            "RETURNS_TABLE": tables["returns"].table_name,
            "TICKETS_TABLE": tables["tickets"].table_name,
            "IDEMPOTENCY_TABLE": tables["idempotency"].table_name,
            "AUDIT_TABLE": tables["audit"].table_name,
            "LEDGER_TABLE": tables["ledger"].table_name,
            "REFUND_QUEUE_URL": self.queue.queue_url,
            "ALLOWED_ORIGINS": ",".join(web_origins),
        }

        def fn(id_: str, handler: str, *, timeout: int, memory: int = 512) -> _lambda.Function:
            vpc_kwargs = (
                {
                    "vpc": vpc,
                    "vpc_subnets": ec2.SubnetSelection(
                        subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
                    ),
                    "security_groups": [app_sg],
                }
                if cfg.lambdas_in_vpc
                else {}
            )
            function = _lambda.Function(
                self,
                id_,
                function_name=resource_name(cfg, id_.lower()),
                runtime=_lambda.Runtime.PYTHON_3_12,
                architecture=_lambda.Architecture.ARM_64,
                handler=handler,
                code=_lambda.Code.from_asset(BUILD_DIR),
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment=env,
                tracing=_lambda.Tracing.ACTIVE,
                log_group=logs.LogGroup(
                    self,
                    f"{id_}Logs",
                    log_group_name=f"/aws/lambda/{resource_name(cfg, id_.lower())}",
                    retention=retention,
                    removal_policy=removal,
                ),
                **vpc_kwargs,
            )
            key.grant_encrypt_decrypt(function)  # tables are encrypted with the CMK
            return function

        def audit_append_only(function: _lambda.Function) -> None:
            tables["audit"].grant(function, "dynamodb:PutItem")

        # ---- customer API ----
        self.customer_fn = fn("CustomerApi", "bizapi.customer_api.handler", timeout=15)
        tables["orders"].grant_read_data(self.customer_fn)
        tables["returns"].grant_read_write_data(self.customer_fn)
        tables["tickets"].grant_read_write_data(self.customer_fn)
        tables["idempotency"].grant_read_write_data(self.customer_fn)
        audit_append_only(self.customer_fn)
        self.queue.grant_send_messages(self.customer_fn)

        # ---- ops API (human reps) ----
        self.ops_fn = fn("OpsApi", "bizapi.ops_api.handler", timeout=15)
        tables["returns"].grant_read_write_data(self.ops_fn)
        tables["tickets"].grant_read_write_data(self.ops_fn)
        tables["idempotency"].grant_read_write_data(self.ops_fn)
        audit_append_only(self.ops_fn)
        self.queue.grant_send_messages(self.ops_fn)

        # ---- refund worker (SQS) ----
        self.worker_fn = fn("RefundWorker", "bizapi.refund_worker.handler", timeout=30)
        tables["orders"].grant_read_data(self.worker_fn)
        tables["returns"].grant_read_write_data(self.worker_fn)
        tables["idempotency"].grant_read_write_data(self.worker_fn)
        audit_append_only(self.worker_fn)
        tables["ledger"].grant(self.worker_fn, "dynamodb:PutItem")  # payout record only
        self.worker_fn.add_event_source(
            event_sources.SqsEventSource(
                self.queue,
                batch_size=5,
                report_batch_item_failures=True,
                max_concurrency=5,  # protects the payment gateway / downstream
            )
        )

        # ---- scheduler (expiry + reconciliation) ----
        self.scheduler_fn = fn("Scheduler", "bizapi.scheduler.handler", timeout=60)
        tables["returns"].grant_read_write_data(self.scheduler_fn)
        tables["idempotency"].grant_read_write_data(self.scheduler_fn)
        audit_append_only(self.scheduler_fn)
        self.queue.grant_send_messages(self.scheduler_fn)
        events.Rule(
            self,
            "SchedulerRule",
            rule_name=resource_name(cfg, "maintenance"),
            schedule=events.Schedule.rate(Duration.minutes(15)),
            targets=[targets.LambdaFunction(self.scheduler_fn, retry_attempts=2)],
        )

        # ---- alarms ----
        self.alarm_topic = sns.Topic(self, "AlarmTopic", topic_name=resource_name(cfg, "alarms"))
        if alarm_email:
            from aws_cdk import aws_sns_subscriptions as subs

            self.alarm_topic.add_subscription(subs.EmailSubscription(alarm_email))
        action = cw_actions.SnsAction(self.alarm_topic)

        def alarm(id_: str, metric: cloudwatch.IMetric, description: str) -> None:
            a = cloudwatch.Alarm(
                self,
                id_,
                alarm_name=resource_name(cfg, id_.lower()),
                alarm_description=description,
                metric=metric,
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            a.add_alarm_action(action)

        alarm(
            "RefundDlqNotEmpty",
            self.dlq.metric_approximate_number_of_messages_visible(period=Duration.minutes(1)),
            "Refund messages reached the DLQ; investigate before they are lost.",
        )
        for function in (self.customer_fn, self.ops_fn, self.worker_fn, self.scheduler_fn):
            alarm(
                f"{function.node.id}Errors",
                function.metric_errors(period=Duration.minutes(5)),
                f"{function.node.id} invocation errors",
            )
