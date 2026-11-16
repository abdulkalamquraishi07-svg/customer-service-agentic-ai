"""Hallucination check: replies must be supported by recent tool results."""

import pytest
from csagent_agents import service as svc
from csagent_agents.guard import (
    BedrockGrounding,
    GroundingVerdict,
    GuardrailUnavailable,
    grounding_score,
)
from test_guardrails import FakeResult, ScriptedChecker, _token

# Observed live (eval run): knowledge base returned nothing relevant, reply invented a service.
GIFT_WRAP_REPLY = (
    "We do offer gift wrapping services for orders. You can select this option during the "
    "checkout process. The cost of gift wrapping varies depending on the size and type of the item."
)


def _response(score):
    return {
        "action": "NONE",
        "assessments": [
            {"contextualGroundingPolicy": {"filters": [
                {"type": "GROUNDING", "threshold": 0.5, "score": score, "action": "NONE"},
                {"type": "RELEVANCE", "threshold": 0.5, "score": 0.9, "action": "NONE"},
            ]}}
        ],
    }


class Client:
    def __init__(self, score=0.9, fail=False, empty=False):
        self.score, self.fail, self.empty, self.calls = score, fail, empty, []

    def apply_guardrail(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise TimeoutError("timed out")
        return {"action": "NONE", "assessments": []} if self.empty else _response(self.score)


def test_score_is_read_from_the_grounding_filter_only():
    assert grounding_score(_response(0.31)) == 0.31
    assert grounding_score({"assessments": []}) is None


def test_request_marks_source_query_and_response_and_respects_limits():
    client = Client()
    BedrockGrounding(client, "gr-g", "1", 0.5).check("S" * 150_000, "Q" * 2000, "R" * 9000)
    (call,) = client.calls
    assert call["source"] == "OUTPUT" and call["guardrailIdentifier"] == "gr-g"
    src, query, resp = call["content"]
    assert src["text"]["qualifiers"] == ["grounding_source"]
    assert query["text"]["qualifiers"] == ["query"]
    assert "qualifiers" not in resp["text"]  # the content being judged
    assert len(src["text"]["text"]) == 100_000  # newest evidence kept (tail)
    assert len(query["text"]["text"]) == 1_000 and len(resp["text"]["text"]) == 5_000


@pytest.mark.parametrize(("score", "grounded"), [(0.49, False), (0.5, True), (0.95, True)])
def test_threshold_decides(score, grounded):
    v = BedrockGrounding(Client(score), "g", "1", 0.5).check("s", "q", "r")
    assert v == GroundingVerdict(score=score, grounded=grounded)


def test_errors_and_missing_scores_fail_closed():
    with pytest.raises(GuardrailUnavailable):
        BedrockGrounding(Client(fail=True), "g", "1", 0.5).check("s", "q", "r")
    with pytest.raises(GuardrailUnavailable):
        BedrockGrounding(Client(empty=True), "g", "1", 0.5).check("s", "q", "r")
    with pytest.raises(ValueError):
        BedrockGrounding(Client(), "g", "1", 1.0)


# ---------------- turn flow ----------------


class EvidenceSupervisor:
    """Behaves like the real supervisor: tools add evidence to the request, then it replies."""

    def __init__(self, replies, evidence=('{"results": []}',)):
        self.replies, self.evidence = list(replies), evidence

    def __call__(self, prompt, **kw):
        request = kw["invocation_state"]["request"]
        for item in self.evidence:
            request.add_evidence("search_company_policies", item)
        return FakeResult(self.replies.pop(0))


class ScriptedGrounding:
    def __init__(self, grounded):
        self.grounded, self.calls = list(grounded), []

    def check(self, source, query, response):
        self.calls.append({"source": source, "query": query, "response": response})
        ok = self.grounded.pop(0)
        return GroundingVerdict(score=0.9 if ok else 0.1, grounded=ok)


def make(monkeypatch, supervisor, grounding, mode="enforce"):
    monkeypatch.setattr(svc, "make_model", lambda cfg: object())
    monkeypatch.setattr(svc, "build_supervisor", lambda model: supervisor)

    class Limiter:
        def acquire(self, c):
            return None

        def record_tokens(self, c, t):
            return None

    class Metrics:
        def emit(self, m):
            return None

    return svc.AgentService(
        svc.AgentConfig(api_base_url="https://api.example.com/", grounding_mode=mode),
        checker=ScriptedChecker(), limiter=Limiter(), metrics=Metrics(), grounding=grounding,
    )


def turn(service, prompt="Do you offer gift wrapping?"):
    return service.handle_turn(prompt=prompt, session_id="s" * 40, id_token=_token())


def test_the_live_gift_wrap_hallucination_is_replaced_and_escalated(monkeypatch):
    grounding = ScriptedGrounding([False])
    out = turn(make(monkeypatch, EvidenceSupervisor([GIFT_WRAP_REPLY]), grounding))
    assert out["response"] == svc.FALLBACK_UNGROUNDED and out["escalation_suggested"] is True
    assert "gift wrapping services" not in out["response"]
    assert {"tool": "grounding_check", "status": "blocked", "ms": 0, "score": 0.1} in out["trace"]
    (call,) = grounding.calls
    assert call["query"] == "Do you offer gift wrapping?" and call["response"] == GIFT_WRAP_REPLY
    assert '{"results": []}' in call["source"]


def test_grounded_reply_passes_unchanged(monkeypatch):
    reply = "Our return window for headphones is 30 days from delivery."
    out = turn(make(monkeypatch, EvidenceSupervisor([reply]), ScriptedGrounding([True])))
    assert out["response"] == reply and out["escalation_suggested"] is False


def test_turns_without_tool_results_are_not_checked(monkeypatch):
    grounding = ScriptedGrounding([])
    supervisor = EvidenceSupervisor(["Hello! How can I help?"], evidence=())
    assert turn(make(monkeypatch, supervisor, grounding))["response"] == "Hello! How can I help?"
    assert grounding.calls == []


def test_follow_up_turns_are_judged_against_earlier_evidence_too(monkeypatch):
    grounding = ScriptedGrounding([True, True])
    supervisor = EvidenceSupervisor(["first", "second"], evidence=("EVIDENCE",))
    service = make(monkeypatch, supervisor, grounding)
    turn(service, "q1")
    turn(service, "q2")
    assert grounding.calls[1]["source"].count("EVIDENCE") == 2  # this turn + the previous one


def test_canned_replies_are_never_grounding_checked(monkeypatch):
    grounding = ScriptedGrounding([])
    supervisor = EvidenceSupervisor([svc.FALLBACK_EMPTY])
    turn(make(monkeypatch, supervisor, grounding))
    assert grounding.calls == []


def test_shadow_mode_records_the_verdict_but_never_changes_the_reply(monkeypatch):
    grounding = ScriptedGrounding([False])
    out = turn(make(monkeypatch, EvidenceSupervisor([GIFT_WRAP_REPLY]), grounding, mode="shadow"))
    assert out["response"] == GIFT_WRAP_REPLY and out["escalation_suggested"] is False
    assert {"tool": "grounding_check", "status": "shadow_blocked", "ms": 0, "score": 0.1} in (
        out["trace"]
    )


def test_shadow_is_the_default_until_calibrated():
    assert svc.AgentConfig(api_base_url="https://x/").grounding_mode == "shadow"


def test_metrics_distinguish_enforced_shadow_and_checked():
    from csagent_agents.metrics import turn_metrics

    def values(status):
        trace = [{"tool": "grounding_check", "status": status, "score": 0.2}]
        m = turn_metrics({"trace": trace, "stop_reason": "end_turn", "usage": {}}, 1)
        return {n: v for n, v, _ in m}

    assert values("blocked")["GroundingBlocked"] == 1
    assert values("blocked")["GroundingShadowBlocked"] == 0
    assert values("shadow_blocked")["GroundingShadowBlocked"] == 1
    assert values("ok")["GroundingChecked"] == 1 and values("ok")["GroundingBlocked"] == 0
