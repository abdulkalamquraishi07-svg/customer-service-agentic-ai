import json
from decimal import Decimal
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from csagent_core.policy import DEFAULT_POLICY
from kb_setup import handler as h

DOCS = Path(__file__).resolve().parents[3] / "data" / "knowledge"


def test_statements_are_idempotent_and_least_privilege():
    sql = h.statements("A" * 32, 1024)
    joined = " ".join(sql)
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "vector(1024)" in joined
    for stmt in sql:
        if stmt.startswith("CREATE TABLE") or stmt.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in stmt
    assert "SUPERUSER" not in joined.upper() and "GRANT ALL" not in joined.upper()


BAD_PASSWORDS = ["short", "a" * 19, "abc'; DROP TABLE x; --" + "a" * 10, "a b" * 10]


@pytest.mark.parametrize("bad", BAD_PASSWORDS)
def test_rejects_passwords_that_could_break_sql(bad):
    with pytest.raises(ValueError):
        h.statements(bad, 1024)


def test_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        h.statements("A" * 32, 0)


class FakeDataApi:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def execute_statement(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ClientError(
                {"Error": {"Code": "DatabaseResumingException", "Message": "resuming"}},
                "ExecuteStatement",
            )
        return {}


def test_waits_for_paused_cluster_to_resume(monkeypatch):
    monkeypatch.setattr(h.time, "sleep", lambda _s: None)
    api = FakeDataApi(fail_times=3)
    h._execute(api, "SELECT 1", cluster_arn="c", secret_arn="s", database="d")
    assert api.calls == 4


def test_other_errors_are_not_swallowed(monkeypatch):
    class Broken:
        def execute_statement(self, **kw):
            raise ClientError({"Error": {"Code": "BadRequestException", "Message": "x"}}, "Op")

    with pytest.raises(ClientError):
        h._execute(Broken(), "SELECT 1", cluster_arn="c", secret_arn="s", database="d")


def test_delete_is_a_no_op():
    out = h.handler({"RequestType": "Delete", "PhysicalResourceId": "pid"}, None)
    assert out == {"PhysicalResourceId": "pid"}


# ---- the agent quotes these documents; they must match what the backend enforces ----


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_return_windows_in_policy_doc_match_rules_engine():
    doc = _doc("returns-and-refunds-policy.md")
    windows = DEFAULT_POLICY.window_days_by_category
    electronics = "Electronics (laptops, tablets, phones, monitors)"
    accessories = "Accessories (headphones, keyboards, cables, cases)"
    assert f"{electronics}: {windows['electronics']} days" in doc
    assert f"{accessories}: {windows['accessories']} days" in doc
    assert f"All other categories: {DEFAULT_POLICY.default_window_days} days" in doc


def test_approval_threshold_and_expiry_in_policy_doc_match_rules_engine():
    doc = _doc("returns-and-refunds-policy.md")
    threshold = f"{int(DEFAULT_POLICY.approval_threshold):,}"
    assert f"Refunds above INR {threshold}" in doc
    assert f"INR {threshold} or less are approved automatically" in doc
    hours = int(DEFAULT_POLICY.approval_ttl.total_seconds() // 3600)
    assert f"within {hours} hours" in doc
    assert DEFAULT_POLICY.approval_threshold == Decimal("10000")


def test_knowledge_documents_contain_no_secrets_or_instructions_to_the_model():
    for path in DOCS.glob("*.md"):
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("ignore previous", "system prompt", "api key", "password", "secret"):
            assert marker not in text, (path.name, marker)
    assert len(list(DOCS.glob("*.md"))) >= 3


def test_handler_reads_kb_password_from_secret(monkeypatch):
    executed = []

    class Secrets:
        def get_secret_value(self, SecretId):
            assert SecretId == "kb-secret"
            return {"SecretString": json.dumps({"username": "bedrock_user", "password": "B" * 32})}

    class Data:
        def execute_statement(self, **kw):
            executed.append(kw)
            return {}

    clients = {"secretsmanager": Secrets(), "rds-data": Data()}
    monkeypatch.setattr(h.boto3, "client", lambda name: clients[name])
    out = h.handler(
        {
            "RequestType": "Create",
            "ResourceProperties": {
                "ClusterArn": "arn:c",
                "MasterSecretArn": "arn:m",
                "KbUserSecretArn": "kb-secret",
                "Database": "knowledge",
                "Dimensions": "1024",
            },
        },
        None,
    )
    assert out["Data"]["TableName"] == "bedrock_integration.bedrock_kb"
    assert {e["secretArn"] for e in executed} == {"arn:m"}  # schema setup uses master creds
    assert len(executed) == len(h.statements("B" * 32, 1024))
