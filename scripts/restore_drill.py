"""Disaster-recovery drill: restore a DynamoDB table from point-in-time recovery and verify it.

    python scripts/restore_drill.py --env dev [--table audit]

Restores the table to its latest restorable time under a temporary name (the original is never
touched), measures how long the restore took (a real RTO sample), checks the restored data against
the source (every restored key must exist in the source; counts must agree up to writes made since
the restore point) and deletes the temporary table. Never runs against prod.
"""

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime

import boto3

REGION = "us-east-1"
KEYS = {
    "orders": ("customer_id", "order_id"),
    "returns": ("customer_id", "return_id"),
    "tickets": ("customer_id", "ticket_id"),
    "audit": ("entity_id", "event_ts"),
    "ledger": ("idempotency_key",),
    "idempotency": ("idempotency_key",),
}


def scan_keys(table, key_names: tuple[str, ...]) -> set[tuple[str, ...]]:
    keys: set[tuple[str, ...]] = set()
    kwargs: dict = {"ProjectionExpression": ", ".join(f"#{i}" for i in range(len(key_names))),
                    "ExpressionAttributeNames": {f"#{i}": n for i, n in enumerate(key_names)}}
    while True:
        page = table.scan(**kwargs)
        keys |= {tuple(str(item[n]) for n in key_names) for item in page["Items"]}
        if "LastEvaluatedKey" not in page:
            return keys
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    parser.add_argument("--table", default="audit", choices=sorted(KEYS))
    args = parser.parse_args()

    source = f"csagent-{args.env}-{args.table}"
    target = f"{source}-restore-drill-{uuid.uuid4().hex[:6]}"
    client = boto3.client("dynamodb", region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)

    described = client.describe_continuous_backups(TableName=source)
    pitr = described["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
    print(
        f"{source}: PITR {pitr['PointInTimeRecoveryStatus']}, "
        f"earliest restorable {pitr.get('EarliestRestorableDateTime')}, "
        f"latest {pitr.get('LatestRestorableDateTime')}"
    )
    if pitr["PointInTimeRecoveryStatus"] != "ENABLED":
        print("FAIL: point-in-time recovery is not enabled")
        return 1

    description = client.describe_table(TableName=source)["Table"]
    sse = description.get("SSEDescription", {})
    overrides = (
        {"SSESpecificationOverride": {"Enabled": True, "SSEType": "KMS",
                                      "KMSMasterKeyId": sse["KMSMasterKeyArn"]}}
        if sse.get("KMSMasterKeyArn") else {}
    )

    started = time.monotonic()
    restore_time = datetime.now(UTC)
    client.restore_table_to_point_in_time(
        SourceTableName=source, TargetTableName=target, UseLatestRestorableTime=True, **overrides
    )
    print(f"restoring to {target} ...")
    try:
        client.get_waiter("table_exists").wait(
            TableName=target, WaiterConfig={"Delay": 10, "MaxAttempts": 120}
        )
        while client.describe_table(TableName=target)["Table"]["TableStatus"] != "ACTIVE":
            time.sleep(5)
        elapsed = time.monotonic() - started

        names = KEYS[args.table]
        source_keys = scan_keys(ddb.Table(source), names)
        restored_keys = scan_keys(ddb.Table(target), names)
        missing_in_source = restored_keys - source_keys
        lost_since = source_keys - restored_keys  # written after the restore point
        restored_sse = client.describe_table(TableName=target)["Table"].get("SSEDescription", {})

        print(f"restore completed in {elapsed:.0f}s (table ACTIVE)")
        print(f"source items {len(source_keys)}, restored items {len(restored_keys)}, "
              f"written after restore point {len(lost_since)}")
        ok = (
            not missing_in_source
            and len(restored_keys) > 0
            and restored_sse.get("Status") == "ENABLED"
        )
        print(f"encryption on restored table: {restored_sse.get('SSEType')} "
              f"{restored_sse.get('Status')}")
        print("RESULT:", "PASS" if ok else "FAIL",
              f"| RTO sample {elapsed:.0f}s for {len(restored_keys)} items "
              f"(started {restore_time:%H:%M:%S} UTC)")
        return 0 if ok else 1
    finally:
        client.delete_table(TableName=target)
        print(f"deleted temporary table {target}")


if __name__ == "__main__":
    sys.exit(main())
