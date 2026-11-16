import boto3
import pytest
from bizapi import signup
from boto3.dynamodb.conditions import Key


@pytest.fixture
def pool(world, monkeypatch):
    """A moto user pool with the real groups and one unconfirmed-then-confirmed user."""
    idp = boto3.client("cognito-idp", region_name="us-east-1")
    pool_id = idp.create_user_pool(PoolName="users")["UserPool"]["Id"]
    for group in ("customers", "support-agents", "approvers"):
        idp.create_group(UserPoolId=pool_id, GroupName=group)
    user = idp.admin_create_user(
        UserPoolId=pool_id,
        Username="new.user@example.com",
        UserAttributes=[{"Name": "email", "Value": "new.user@example.com"}],
        MessageAction="SUPPRESS",
    )["User"]
    sub = next(a["Value"] for a in user["Attributes"] if a["Name"] == "sub")
    monkeypatch.setenv("APP_ENV", "dev")
    return idp, pool_id, sub


def event(pool_id: str, sub: str, trigger: str = "PostConfirmation_ConfirmSignUp") -> dict:
    return {
        "triggerSource": trigger,
        "userPoolId": pool_id,
        "userName": "new.user@example.com",
        "request": {"userAttributes": {"sub": sub, "email": "new.user@example.com"}},
        "response": {},
    }


def groups_of(idp, pool_id: str) -> list[str]:
    resp = idp.admin_list_groups_for_user(UserPoolId=pool_id, Username="new.user@example.com")
    return [g["GroupName"] for g in resp["Groups"]]


def test_confirmed_signup_becomes_customer_only(pool, monkeypatch):
    idp, pool_id, sub = pool
    monkeypatch.delenv("DEMO_ORDERS_TABLE", raising=False)
    ev = event(pool_id, sub)
    assert signup.handler(ev, None) is ev  # Cognito requires the event back
    assert groups_of(idp, pool_id) == ["customers"]


def test_dev_signup_seeds_demo_orders_idempotently(pool, world, monkeypatch):
    idp, pool_id, sub = pool
    monkeypatch.setenv("DEMO_ORDERS_TABLE", "orders")
    signup.handler(event(pool_id, sub), None)
    signup.handler(event(pool_id, sub), None)  # Cognito retry: same keys, no duplicates
    items = world.tables["orders"].query(
        KeyConditionExpression=Key("customer_id").eq(sub)
    )["Items"]
    assert len(items) == 4
    assert {i["order_id"] for i in items} == {"ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004"}
    assert groups_of(idp, pool_id) == ["customers"]


def test_forgot_password_confirmation_is_ignored(pool, monkeypatch):
    idp, pool_id, sub = pool
    monkeypatch.setenv("DEMO_ORDERS_TABLE", "orders")
    signup.handler(event(pool_id, sub, "PostConfirmation_ConfirmForgotPassword"), None)
    assert groups_of(idp, pool_id) == []


def test_demo_seeding_refuses_prod(pool, monkeypatch):
    _, pool_id, sub = pool
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DEMO_ORDERS_TABLE", "orders")
    with pytest.raises(RuntimeError, match="never run in prod"):
        signup.handler(event(pool_id, sub), None)

