"""Seed demo orders for a customer.

The customer_id MUST be the Cognito `sub` of the demo user, because the platform derives
identity only from the verified JWT. Example:

    python scripts/seed_data.py --env dev --customer-id <cognito-sub> --dry-run
    python scripts/seed_data.py --env dev --customer-id <cognito-sub>

Self-registered users in dev get the same orders automatically (sign-up Lambda trigger).
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "core" / "src"))
from csagent_core.demo_data import build_orders  # noqa: E402

__all__ = ["build_orders"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    parser.add_argument("--customer-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.env == "prod":
        raise SystemExit("Refusing to seed demo data into prod.")

    orders = build_orders(args.customer_id, datetime.now(UTC))
    if args.dry_run:
        print(json.dumps(orders, indent=2, default=str))
        return

    import boto3  # imported lazily so --dry-run needs no AWS dependencies

    table = boto3.resource("dynamodb", region_name=args.region).Table(f"csagent-{args.env}-orders")
    with table.batch_writer() as batch:
        for order in orders:
            batch.put_item(Item=order)
    print(f"Seeded {len(orders)} orders into {table.name}")


if __name__ == "__main__":
    main()
