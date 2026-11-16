"""Live check of self sign-up in dev: register, confirm, verify group + demo orders, clean up.

    python evals/signup_check.py [--keep]

Registers through the public SignUp API (as the web app does), then confirms with
AdminConfirmSignUp, which fires the same post-confirmation trigger as a user typing the
emailed code. Prints the throwaway credentials so a browser check can use them; deletes the
user and orders afterwards unless --keep.
"""

import argparse
import json
import secrets
import sys
import time

import boto3
from boto3.dynamodb.conditions import Key

REGION = "us-east-1"


def outputs(stack: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=REGION)
    stack_outputs = cfn.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]
    return {o["OutputKey"]: o["OutputValue"] for o in stack_outputs}


def cleanup(idp, pool: str, email: str, sub: str, orders) -> None:
    for item in orders.query(KeyConditionExpression=Key("customer_id").eq(sub))["Items"]:
        orders.delete_item(Key={"customer_id": sub, "order_id": item["order_id"]})
    idp.admin_delete_user(UserPoolId=pool, Username=email)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="dev", choices=["dev"])
    ap.add_argument("--keep", action="store_true", help="keep the user for a browser check")
    ap.add_argument("--delete", metavar="EMAIL", help="only delete a kept test user")
    args = ap.parse_args()

    auth = outputs(f"csagent-{args.env}-auth")
    pool, client = auth["UserPoolId"], auth["WebClientId"]
    idp = boto3.client("cognito-idp", region_name=REGION)
    orders = boto3.resource("dynamodb", region_name=REGION).Table(f"csagent-{args.env}-orders")

    if args.delete:
        user = idp.admin_get_user(UserPoolId=pool, Username=args.delete)
        sub = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")
        cleanup(idp, pool, args.delete, sub, orders)
        print(f"deleted {args.delete}")
        return 0

    email = f"eval.signup.{secrets.token_hex(4)}@example.com"
    password = "Eval-1a" + secrets.token_urlsafe(12) + "!"
    sub = idp.sign_up(
        ClientId=client,
        Username=email,
        Password=password,
        UserAttributes=[{"Name": "email", "Value": email}],
    )["UserSub"]
    results: dict[str, object] = {"email": email}
    try:
        status = idp.admin_get_user(UserPoolId=pool, Username=email)["UserStatus"]
        results["status_before_confirm"] = status
        idp.admin_confirm_sign_up(UserPoolId=pool, Username=email)
        time.sleep(2)
        groups = [
            g["GroupName"]
            for g in idp.admin_list_groups_for_user(UserPoolId=pool, Username=email)["Groups"]
        ]
        seeded = orders.query(KeyConditionExpression=Key("customer_id").eq(sub))["Items"]
        results.update(groups=groups, demo_orders=sorted(i["order_id"] for i in seeded))
        ok = (
            status == "UNCONFIRMED"
            and groups == ["customers"]
            and len(seeded) == 4
        )
        results["ok"] = ok
        if args.keep:
            results["password"] = password
        print(json.dumps(results, indent=2))
        return 0 if ok else 1
    finally:
        if not args.keep:
            cleanup(idp, pool, email, sub, orders)


if __name__ == "__main__":
    sys.exit(main())
