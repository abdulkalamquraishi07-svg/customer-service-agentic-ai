"""Data layer: KMS key + DynamoDB tables (customer-scoped partition keys)."""

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_kms as kms
from config import EnvConfig, resource_name
from constructs import Construct


class DataStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        removal = RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY

        self.key = kms.Key(
            self,
            "DataKey",
            alias=f"alias/{resource_name(cfg, 'data')}",
            enable_key_rotation=True,
            removal_policy=removal,
            description="Encrypts customer-service application data",
        )

        def table(
            id_: str,
            name: str,
            pk: str,
            sk: str | None = None,
            *,
            ttl: str | None = None,
            stream: bool = False,
        ) -> dynamodb.Table:
            return dynamodb.Table(
                self,
                id_,
                table_name=resource_name(cfg, name),
                partition_key=dynamodb.Attribute(name=pk, type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=sk, type=dynamodb.AttributeType.STRING)
                if sk
                else None,
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
                encryption_key=self.key,
                point_in_time_recovery=True,
                deletion_protection=cfg.deletion_protection,
                removal_policy=removal,
                time_to_live_attribute=ttl,
                stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES if stream else None,
            )

        # Partition key = customer_id on customer data enables IAM leading-key
        # conditions (dynamodb:LeadingKeys) for tenant/customer isolation.
        self.orders = table("Orders", "orders", "customer_id", "order_id")

        self.returns = table("Returns", "returns", "customer_id", "return_id")
        self.returns.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )

        self.tickets = table("Tickets", "tickets", "customer_id", "ticket_id")
        self.tickets.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="created_at", type=dynamodb.AttributeType.STRING),
        )

        # Idempotency records expire automatically via TTL.
        self.idempotency = table("Idempotency", "idempotency", "idempotency_key", ttl="expires_at")

        # Append-only audit trail; stream feeds archival to S3 (Object Lock) in a later phase.
        self.audit = table("Audit", "audit", "entity_id", "event_ts", stream=True)

        # Refund payout ledger (simulated payment gateway). Keyed by idempotency key so a
        # payout can be recorded only once.
        self.ledger = table("Ledger", "ledger", "idempotency_key")

        # Per-customer rate-limit counters for the agent path. Disposable (TTL) and non-sensitive
        # (counts keyed by customer id), so: AWS-owned encryption (the agent runtime then needs no
        # KMS access), no PITR, destroyable. Deliberately NOT in `tables` (those are business data).
        self.ratelimit = dynamodb.Table(
            self,
            "RateLimit",
            table_name=resource_name(cfg, "ratelimit"),
            partition_key=dynamodb.Attribute(name="bucket", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.tables = {
            "ledger": self.ledger,
            "orders": self.orders,
            "returns": self.returns,
            "tickets": self.tickets,
            "idempotency": self.idempotency,
            "audit": self.audit,
        }
