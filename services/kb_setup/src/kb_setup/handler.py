"""CloudFormation custom resource: prepare Aurora PostgreSQL as a Bedrock KB vector store.

Runs through the RDS Data API (no VPC access needed). Idempotent: safe on create and update.
Creates a dedicated least-privilege `bedrock_user` (the KB never uses the master user), the
pgvector extension, the vector table and its indexes.
"""

import json
import logging
import os
import re
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SCHEMA = "bedrock_integration"
TABLE = "bedrock_kb"
KB_USER = "bedrock_user"
_SAFE_PASSWORD = re.compile(r"^[A-Za-z0-9]{20,128}$")


def _role_statement(password: str) -> str:
    # DDL (CREATE ROLE ... PASSWORD) cannot take bind parameters. Only constants and the
    # regex-validated alphanumeric password (checked in statements()) are interpolated.
    exists = f"SELECT 1 FROM pg_roles WHERE rolname = '{KB_USER}'"  # noqa: S608
    return (
        "DO $$ BEGIN "
        f"IF NOT EXISTS ({exists}) THEN "
        f"CREATE ROLE {KB_USER} WITH LOGIN PASSWORD '{password}'; "
        f"ELSE ALTER ROLE {KB_USER} WITH LOGIN PASSWORD '{password}'; "
        "END IF; END $$"
    )


def statements(password: str, dimensions: int) -> list[str]:
    # Generated password is alphanumeric only (validated) so it is safe inside a SQL literal.
    if not _SAFE_PASSWORD.match(password):
        raise ValueError("KB user password must be 20-128 alphanumeric characters")
    if not 1 <= dimensions <= 4096:
        raise ValueError("invalid embedding dimensions")
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        _role_statement(password),
        f"GRANT USAGE ON SCHEMA {SCHEMA} TO {KB_USER}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.{TABLE} (
              id uuid PRIMARY KEY,
              embedding vector({dimensions}),
              chunks text,
              metadata json,
              custom_metadata jsonb
            )""",
        f"ALTER TABLE {SCHEMA}.{TABLE} OWNER TO {KB_USER}",
        f"CREATE INDEX IF NOT EXISTS bedrock_kb_embedding_idx ON {SCHEMA}.{TABLE} "
        "USING hnsw (embedding vector_cosine_ops) WITH (ef_construction=256)",
        f"CREATE INDEX IF NOT EXISTS bedrock_kb_chunks_idx ON {SCHEMA}.{TABLE} "
        "USING gin (to_tsvector('simple', chunks))",
        f"CREATE INDEX IF NOT EXISTS bedrock_kb_custom_metadata_idx ON {SCHEMA}.{TABLE} "
        "USING gin (custom_metadata)",
    ]


def _execute(client: Any, sql: str, *, cluster_arn: str, secret_arn: str, database: str) -> None:
    """Execute one statement, waiting for a paused (scale-to-zero) cluster to resume."""
    deadline = time.monotonic() + 240
    delay = 5.0
    while True:
        try:
            client.execute_statement(
                resourceArn=cluster_arn, secretArn=secret_arn, database=database, sql=sql
            )
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            resuming = code == "DatabaseResumingException" or "resum" in str(exc).lower()
            if resuming and time.monotonic() < deadline:
                logger.info("database resuming; retrying in %.0fs", delay)
                time.sleep(delay)
                delay = min(delay * 1.5, 30.0)
                continue
            raise


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request_type = event["RequestType"]
    physical_id = event.get("PhysicalResourceId") or "kb-vector-store"
    if request_type == "Delete":
        # Keep data: the cluster's own removal policy decides what happens to it.
        return {"PhysicalResourceId": physical_id}

    props = event["ResourceProperties"]
    secrets = boto3.client("secretsmanager")
    kb_secret = json.loads(
        secrets.get_secret_value(SecretId=props["KbUserSecretArn"])["SecretString"]
    )
    client = boto3.client("rds-data")
    for sql in statements(kb_secret["password"], int(props["Dimensions"])):
        _execute(
            client,
            sql,
            cluster_arn=props["ClusterArn"],
            secret_arn=props["MasterSecretArn"],
            database=props["Database"],
        )
    logger.info("vector store ready: %s.%s", SCHEMA, TABLE)
    return {"PhysicalResourceId": physical_id, "Data": {"TableName": f"{SCHEMA}.{TABLE}"}}


if __name__ == "__main__":  # pragma: no cover - local inspection helper
    print("\n;\n".join(statements("x" * 24, int(os.environ.get("DIMS", "1024")))))
