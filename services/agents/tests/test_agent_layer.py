"""Agent-layer tests that need no model: API client resilience, identity, tool contracts."""

import base64
import json

import httpx
import pytest
from csagent_agents.api_client import BusinessApiClient, CircuitBreaker, ToolFailure, _seg
from csagent_agents.context import RequestContext, subject_from_token


def jwt(claims: dict) -> str:
    def enc(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.sig"


def client(handler, **kw) -> BusinessApiClient:
    return BusinessApiClient(
        "https://api.example.com/dev/",
        "tok",
        transport=httpx.MockTransport(handler),
        breaker=kw.pop("breaker", CircuitBreaker()),
        sleep=lambda _s: None,
        **kw,
    )


# ---------- identity ----------


def test_subject_is_read_from_token():
    assert subject_from_token(jwt({"sub": "cust-1"})) == "cust-1"


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", jwt({"no": "sub"}), jwt({"sub": ""})])
def test_bad_tokens_rejected(token):
    with pytest.raises(ValueError):
        subject_from_token(token)


def test_idempotency_keys_are_deterministic_and_scoped():
    def ctx(session, customer):
        return RequestContext(session, customer, api=None)  # type: ignore[arg-type]

    a = ctx("s1", "c1").idempotency_key("request_return", "ORD-1", "broken")
    assert a == ctx("s1", "c1").idempotency_key("request_return", "ORD-1", "broken")
    assert a != ctx("s1", "c2").idempotency_key("request_return", "ORD-1", "broken")
    assert a != ctx("s2", "c1").idempotency_key("request_return", "ORD-1", "broken")
    assert 8 <= len(a) <= 128  # satisfies the API's key length rule


# ---------- path safety ----------


BAD_IDS = ["", "../ops/approvals", "ORD-1/../../x", "a b", "x" * 65, "ORD?x=1"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_model_supplied_ids_cannot_alter_paths(bad):
    with pytest.raises(ToolFailure):
        _seg(bad)


def test_valid_id_passes():
    assert _seg(" ORD-1001 ") == "ORD-1001"


# ---------- client behavior ----------


def test_sends_caller_token_and_idempotency_header():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers.get("authorization")
        seen["key"] = req.headers.get("idempotency-key")
        seen["url"] = str(req.url)
        return httpx.Response(201, json={"return_id": "RET-1"})

    assert client(handler).create_return("ORD-1", "broken", "k" * 20)["return_id"] == "RET-1"
    assert seen == {
        "auth": "tok",
        "key": "k" * 20,
        "url": "https://api.example.com/dev/v1/returns",
    }


def test_get_retries_transient_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    assert client(handler).list_orders() == {"ok": 1}
    assert len(calls) == 3


def test_unkeyed_write_is_never_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    with pytest.raises(ToolFailure) as exc:
        client(handler).escalate_ticket("TKT-1", "summary")
    assert exc.value.retryable and len(calls) == 1


def test_client_errors_are_not_retried_and_carry_safe_message():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(
            422, json={"error": {"message": "return not allowed: RETURN_WINDOW_EXPIRED"}}
        )

    with pytest.raises(ToolFailure) as exc:
        client(handler).create_return("ORD-1", "x", "k" * 20)
    assert len(calls) == 1 and not exc.value.retryable
    assert "RETURN_WINDOW_EXPIRED" in str(exc.value)


def test_timeouts_become_retryable_tool_failures():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ToolFailure) as exc:
        client(handler).list_orders()
    assert exc.value.retryable


def test_circuit_breaker_opens_and_stops_calls():
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=60)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    c = client(handler, breaker=breaker, max_retries=0)
    for _ in range(2):
        with pytest.raises(ToolFailure):
            c.list_orders()
    with pytest.raises(ToolFailure, match="temporarily unavailable"):
        c.list_orders()
    assert len(calls) == 2  # third call short-circuited, API protected


def test_404_does_not_trip_breaker():
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)

    def handler(req):
        return httpx.Response(404, json={"error": {"message": "order not found"}})

    c = client(handler, breaker=breaker)
    for _ in range(3):
        with pytest.raises(ToolFailure, match="not found"):
            c.get_order("ORD-X")
    assert breaker.allow()


# ---------- knowledge retrieval on a scale-to-zero vector store ----------


class FakeKb:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def retrieve(self, **kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"retrievalResults": []}


def test_retrieve_waits_for_resuming_database():
    from csagent_agents.tools import _retrieve_with_resume

    kb = FakeKb([RuntimeError("DB cluster is resuming after being auto-paused")] * 2)
    assert _retrieve_with_resume(kb, sleep=lambda _s: None, knowledgeBaseId="kb") == {
        "retrievalResults": []
    }
    assert kb.calls == 3


def test_retrieve_other_errors_fail_fast_and_honestly():
    from csagent_agents.tools import _retrieve_with_resume

    kb = FakeKb([RuntimeError("AccessDenied")])
    with pytest.raises(ToolFailure, match="temporarily unavailable"):
        _retrieve_with_resume(kb, sleep=lambda _s: None, knowledgeBaseId="kb")
    assert kb.calls == 1


# ---------- relevance ranking (scores observed live from the Aurora-backed KB) ----------


def _hit(score: float, doc: str) -> dict:
    return {
        "score": score,
        "location": {"s3Location": {"uri": f"s3://bucket/policies/{doc}"}},
        "content": {"text": f"text of {doc}"},
    }


def test_distance_scores_are_reranked_best_first():
    from csagent_agents.tools import rank_results

    # Exactly what Bedrock returned for "my delivery is late": worst match listed first.
    raw = [_hit(0.869, "support-faq.md"), _hit(0.840, "returns.md"), _hit(0.587, "delivery.md")]
    ranked = rank_results(raw, score_mode="distance", min_similarity=0.15)
    assert [r["source"] for r in ranked] == ["delivery.md", "returns.md"]
    assert ranked[0]["relevance"] == 0.413


def test_off_topic_query_returns_nothing():
    from csagent_agents.tools import rank_results

    # Observed for "recipe for chocolate cake" (includes a distance > 1).
    raw = [_hit(1.022, "returns.md"), _hit(0.996, "support-faq.md"), _hit(0.927, "delivery.md")]
    assert rank_results(raw, score_mode="distance", min_similarity=0.15) == []


def test_similarity_mode_and_unknown_mode():
    from csagent_agents.tools import rank_results, to_similarity

    ranked = rank_results([_hit(0.9, "a.md"), _hit(0.1, "b.md")], score_mode="similarity",
                          min_similarity=0.15)
    assert [r["source"] for r in ranked] == ["a.md"]
    with pytest.raises(ValueError):
        to_similarity(0.5, "bogus")


# ---------- customer-facing reply hygiene ----------


def test_reasoning_blocks_are_removed_from_replies():
    from csagent_agents.service import clean_reply

    raw = (
        "<thinking> The order is one day overdue, so I should inform the customer. </thinking>\n"
        "Your laptop order (ORD-1001) is one day overdue."
    )
    assert clean_reply(raw) == "Your laptop order (ORD-1001) is one day overdue."


def test_multiple_and_mixed_case_blocks_removed():
    from csagent_agents.service import clean_reply

    raw = "<Thinking>a</Thinking>Hello.\n\n\n\n<reasoning>b\nc</reasoning>Bye."
    assert clean_reply(raw) == "Hello.\n\nBye."


def test_unclosed_block_is_dropped_and_empty_reply_gets_fallback():
    from csagent_agents.service import FALLBACK_EMPTY, clean_reply

    assert clean_reply("Answer first. <thinking>never closed") == "Answer first."
    assert clean_reply("<thinking>only reasoning</thinking>") == FALLBACK_EMPTY


# ---------- invented links ----------


def test_escalation_uses_the_ticket_created_this_turn_not_the_models_copy():
    from csagent_agents.tools import resolve_ticket_id

    # Live failure: the model typed a wrong id, the API said 404, the customer stayed un-escalated.
    assert resolve_ticket_id("TKT-WRONG123", ["TKT-ABC123456789"]) == "TKT-ABC123456789"
    assert resolve_ticket_id("TKT-ABC123456789", ["TKT-ABC123456789"]) == "TKT-ABC123456789"
    assert resolve_ticket_id("", ["TKT-A", "TKT-B"]) == "TKT-B"  # latest ticket of the turn
    # No ticket created this turn (customer escalates an older one): the typed id is used and
    # the API enforces that it belongs to the customer.
    assert resolve_ticket_id(" TKT-OLD ", []) == "TKT-OLD"


def test_invented_markdown_link_keeps_text_drops_url():
    from csagent_agents.service import clean_reply

    # Observed live from Nova Pro: a URL no tool or document supplied.
    raw = "You can track your package [here](https://www.bluedart.com/track-your-shipment/)."
    assert clean_reply(raw) == "You can track your package here."


def test_bare_urls_are_removed():
    from csagent_agents.service import clean_reply

    out = clean_reply("Visit https://evil.example.com/login or www.phish.test now.")
    assert "http" not in out and "www." not in out and "evil" not in out


def test_allowlisted_domains_survive(monkeypatch):
    from csagent_agents import service

    monkeypatch.setattr(service, "ALLOWED_LINK_DOMAINS", frozenset({"shop.example.com"}))
    raw = "See [orders](https://shop.example.com/orders) not [x](https://shop.example.com.evil.io/)."
    assert service.clean_reply(raw) == "See [orders](https://shop.example.com/orders) not x."


def test_order_ids_and_amounts_are_untouched():
    from csagent_agents.service import clean_reply

    raw = "Order ORD-1001 (tracking BD123456789) refund of 2499 INR, ticket TKT-ABC123."
    assert clean_reply(raw) == raw


# ---------- AgentCore entrypoint ----------


class StubService:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls: list[dict] = []

    def handle_turn(self, *, prompt, session_id, id_token):
        self.calls.append({"prompt": prompt, "session_id": session_id, "id_token": id_token})
        if self.exc:
            raise self.exc
        return {
            "response": "Your order ORD-1001 is one day overdue.",
            "session_id": session_id,
            "request_id": "req-1",
            "stop_reason": "end_turn",
            "escalation_suggested": False,
            "trace": [{"tool": "get_shipment_status", "status": "ok", "ms": 120}],
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


SESSION = "s" * 40


def test_bearer_token_parsing():
    from csagent_agents.entrypoint import bearer_token

    assert bearer_token({"Authorization": "Bearer abc.def.ghi"}) == "abc.def.ghi"
    assert bearer_token({"authorization": "bearer xyz"}) == "xyz"
    assert bearer_token({"Authorization": "rawtoken"}) == "rawtoken"
    assert bearer_token({}) is None
    assert bearer_token({"Authorization": ""}) is None


def test_invocation_passes_validated_token_and_hides_internals():
    from csagent_agents.entrypoint import handle_invocation

    svc = StubService()
    out = handle_invocation(
        {"prompt": "where is my laptop"}, SESSION, {"Authorization": "Bearer tok"}, svc
    )
    assert svc.calls == [{"prompt": "where is my laptop", "session_id": SESSION, "id_token": "tok"}]
    assert out == {
        "response": "Your order ORD-1001 is one day overdue.",
        "session_id": SESSION,
        "request_id": "req-1",
        "escalation_suggested": False,
    }  # no tool names, trace or token usage leak to the customer


@pytest.mark.parametrize(
    ("payload", "headers", "session", "code"),
    [
        ({"prompt": 5}, {"Authorization": "Bearer t"}, SESSION, "invalid_request"),
        ("not a dict", {"Authorization": "Bearer t"}, SESSION, "invalid_request"),
        ({"prompt": "hi"}, {}, SESSION, "unauthenticated"),
        ({"prompt": "hi"}, {"Authorization": "Bearer t"}, None, "invalid_request"),
    ],
)
def test_invalid_invocations_never_reach_the_agent(payload, headers, session, code):
    from csagent_agents.entrypoint import handle_invocation

    svc = StubService()
    assert handle_invocation(payload, session, headers, svc)["error"]["code"] == code
    assert svc.calls == []


def test_rejections_and_crashes_return_safe_messages(caplog):
    from csagent_agents.entrypoint import GENERIC_FAILURE, handle_invocation
    from csagent_agents.service import RequestRejected

    headers = {"Authorization": "Bearer t"}
    rejected = handle_invocation(
        {"prompt": "hi"}, SESSION, headers, StubService(RequestRejected("Message is too long."))
    )
    assert rejected["error"] == {"code": "rejected", "message": "Message is too long."}

    crashed = handle_invocation(
        {"prompt": "hi"}, SESSION, headers, StubService(RuntimeError("db password=hunter2"))
    )
    assert crashed["error"] == {"code": "internal_error", "message": GENERIC_FAILURE}
    assert "hunter2" not in json.dumps(crashed)


def test_operational_log_has_no_conversation_text(caplog):
    import logging

    from csagent_agents.entrypoint import handle_invocation

    with caplog.at_level(logging.INFO, logger="csagent.agent"):
        handle_invocation(
            {"prompt": "my phone number is 98765"}, SESSION, {"Authorization": "Bearer t"},
            StubService(),
        )
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "turn_completed" in logged and "get_shipment_status" in logged
    assert "98765" not in logged and "ORD-1001 is one day overdue" not in logged
    assert SESSION not in logged  # session id is fingerprinted
