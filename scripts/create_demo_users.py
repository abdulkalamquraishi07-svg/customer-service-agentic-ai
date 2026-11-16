"""Create (or reset) demo users for the web app in dev/staging.

    python scripts/create_demo_users.py --env dev

Users: a customer with seeded demo orders, a support agent and a refund approver. Each gets a
random PERMANENT password, printed once (re-running the script rotates it). These are test
credentials for a non-production pool with sample data only. Refuses to run against prod.
"""

import argparse
import contextlib
import secrets
import string
import sys
from datetime import UTC, datetime

import boto3

sys.path.insert(0, "scripts")
from seed_data import build_orders  # noqa: E402

REGION = "us-east-1"
USERS = [
    ("demo.customer@example.com", "customers"),
    ("demo.agent@example.com", "support-agents"),
    ("demo.approver@example.com", "approvers"),
]


def demo_password() -> str:
    """Meets the pool policy: 12+ chars with upper, lower, digit and symbol."""
    pool = string.ascii_letters + string.digits
    return "Demo-1a" + "".join(secrets.choice(pool) for _ in range(13))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    args = parser.parse_args()

    outputs = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=f"csagent-{args.env}-auth"
    )["Stacks"][0]["Outputs"]
    pool = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "UserPoolId")
    idp = boto3.client("cognito-idp", region_name=REGION)

    print(f"User pool {pool}. Permanent demo passwords (dev test accounts only):\n")
    for email, group in USERS:
        password = demo_password()
        with contextlib.suppress(idp.exceptions.UsernameExistsException):
            idp.admin_create_user(
                UserPoolId=pool,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",
            )
        # Permanent=True moves the user to CONFIRMED: no forced change at first sign-in.
        idp.admin_set_user_password(
            UserPoolId=pool, Username=email, Password=password, Permanent=True
        )
        idp.admin_add_user_to_group(UserPoolId=pool, Username=email, GroupName=group)
        if group == "customers":
            user = idp.admin_get_user(UserPoolId=pool, Username=email)
            sub = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")
            table = boto3.resource("dynamodb", region_name=REGION).Table(
                f"csagent-{args.env}-orders"
            )
            for item in build_orders(sub, datetime.now(UTC)):
                table.put_item(Item=item)
        print(f"  {email:30s} {group:15s} {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
