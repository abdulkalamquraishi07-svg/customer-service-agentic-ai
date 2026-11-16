"""Cognito post-confirmation trigger for self sign-up.

A user who registers and confirms their email is added to the `customers` group, and only that
group: staff groups are assigned by an administrator. In non-production environments with
DEMO_ORDERS_TABLE set, the new customer also gets sample orders so the chat has data to use.

Cognito waits for this trigger (5 s limit) and fails the confirmation if it raises, so the
user can retry. Both steps are idempotent: re-adding a group member and re-writing the same
order keys are no-ops.
"""

import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from bizapi.common import log
from csagent_core.demo_data import build_orders

CUSTOMER_GROUP = "customers"
HANDLED_TRIGGER = "PostConfirmation_ConfirmSignUp"


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # Also fired for PostConfirmation_ConfirmForgotPassword: nothing to do there.
    if event.get("triggerSource") != HANDLED_TRIGGER:
        return event

    pool_id = event["userPoolId"]
    username = event["userName"]
    sub = event["request"]["userAttributes"]["sub"]

    boto3.client("cognito-idp").admin_add_user_to_group(
        UserPoolId=pool_id, Username=username, GroupName=CUSTOMER_GROUP
    )

    seeded = 0
    table_name = os.environ.get("DEMO_ORDERS_TABLE")
    if table_name:
        if os.environ.get("APP_ENV") == "prod":
            raise RuntimeError("Demo order seeding must never run in prod")
        table = boto3.resource("dynamodb").Table(table_name)
        with table.batch_writer() as batch:
            for order in build_orders(sub, datetime.now(UTC)):
                batch.put_item(Item=order)
                seeded += 1

    log(logging.INFO, "signup_confirmed", customer_id=sub, demo_orders=seeded)
    return event
