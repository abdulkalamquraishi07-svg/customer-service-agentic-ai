import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from handlers import edge  # noqa: E402


def test_health_ok():
    resp = edge.health({}, None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "ok"


def test_whoami_uses_only_verified_claims():
    event = {
        "body": json.dumps({"customer_id": "attacker-supplied"}),  # must be ignored
        "requestContext": {
            "authorizer": {"claims": {"sub": "abc-123", "cognito:groups": "[customers]"}}
        },
    }
    body = json.loads(edge.whoami(event, None)["body"])
    assert body == {"customer_id": "abc-123", "groups": ["customers"]}


def test_whoami_rejects_missing_claims():
    assert edge.whoami({"requestContext": {}}, None)["statusCode"] == 401
