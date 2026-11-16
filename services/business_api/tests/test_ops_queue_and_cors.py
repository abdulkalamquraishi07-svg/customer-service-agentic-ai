import json

import pytest
from bizapi import customer_api, ops_api
from conftest import make_event

KEY = "idem-key-0001"
AGENT = {"sub": "agent-1", "groups": "support-agents"}


def call(module, event):
    resp = module.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"]), resp


def _escalated_ticket(world, sub="cust-1"):
    ev = make_event("POST", "/v1/tickets", sub=sub, key=KEY,
                    body={"category": "delivery", "subject": "Late", "description": "Not here"})
    tid = call(customer_api, ev)[1]["ticket_id"]
    esc = make_event("POST", "/v1/tickets/{ticket_id}/escalate", sub=sub,
                     path={"ticket_id": tid}, body={"summary": "Wants a human"})
    assert call(customer_api, esc)[0] == 200
    return tid


def test_support_agent_sees_escalated_queue_across_customers(world):
    a = _escalated_ticket(world, "cust-1")
    b = _escalated_ticket(world, "cust-2")
    status, body, _ = call(ops_api, make_event("GET", "/v1/ops/tickets", **AGENT))
    assert status == 200
    assert {t["ticket_id"] for t in body["tickets"]} == {a, b}
    # Staff need the customer id to act on a ticket; internal version is hidden.
    assert all("customer_id" in t and "version" not in t for t in body["tickets"])


def test_customers_cannot_read_the_queue(world):
    _escalated_ticket(world)
    assert call(ops_api, make_event("GET", "/v1/ops/tickets"))[0] == 403


def test_queue_status_filter_and_validation(world):
    _escalated_ticket(world)
    ev = make_event("GET", "/v1/ops/tickets", **AGENT)
    ev["queryStringParameters"] = {"status": "resolved"}
    assert call(ops_api, ev)[1]["tickets"] == []
    ev["queryStringParameters"] = {"status": "bogus"}
    assert call(ops_api, ev)[0] == 400


@pytest.fixture
def origins(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://d123.cloudfront.net, http://localhost:5173")


def _with_origin(origin):
    ev = make_event("GET", "/v1/orders")
    ev["headers"] = {"Origin": origin} if origin else {}
    return ev


def test_allowed_origin_is_echoed(world, origins):
    _, _, resp = call(customer_api, _with_origin("https://d123.cloudfront.net"))
    assert resp["headers"]["Access-Control-Allow-Origin"] == "https://d123.cloudfront.net"
    assert resp["headers"]["Vary"] == "Origin"


def test_unknown_origin_gets_no_cors_header(world, origins):
    for origin in ("https://evil.example.com", "https://d123.cloudfront.net.evil.io", None):
        _, _, resp = call(customer_api, _with_origin(origin))
        assert "Access-Control-Allow-Origin" not in resp["headers"], origin


def test_errors_also_carry_cors_for_allowed_origins(world, origins):
    ev = _with_origin("http://localhost:5173")
    ev["requestContext"]["authorizer"]["claims"] = {}
    _, _, resp = call(customer_api, ev)
    assert resp["statusCode"] == 403
    assert resp["headers"]["Access-Control-Allow-Origin"] == "http://localhost:5173"
