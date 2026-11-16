"""A reply may only claim an escalation or ticket that a tool actually performed this turn."""

import logging

import pytest
from csagent_agents.service import unverified_action_claim

OK_TICKET = [{"tool": "create_support_ticket", "status": "ok"}]
FAILED_TICKET = [{"tool": "create_support_ticket", "status": "error"}]
LOOKUPS_ONLY = [{"tool": "get_shipment_status", "status": "ok"}]

# Observed live from the hosted runtime (no ticket was created):
LIVE_FALSE_PROMISE = (
    "I'm sorry to hear that you were charged twice for order ORD-1001. I will escalate this "
    "issue to our support team for further investigation and resolution."
)


@pytest.mark.parametrize(
    "reply",
    [
        LIVE_FALSE_PROMISE,
        "I've created a ticket for you.",
        "I have escalated your case to a human representative.",
        "Your case has been escalated.",
        "Your ticket number is TKT-123.",
        "I'll open a support ticket right away.",
    ],
)
def test_claims_without_a_successful_tool_are_flagged(reply):
    assert unverified_action_claim(reply, LOOKUPS_ONLY)
    assert unverified_action_claim(reply, FAILED_TICKET)


def test_claims_backed_by_a_tool_pass():
    assert not unverified_action_claim(LIVE_FALSE_PROMISE, OK_TICKET)
    assert not unverified_action_claim(
        "I have escalated your case.", [{"tool": "escalate_to_human", "status": "ok"}]
    )


@pytest.mark.parametrize(
    "reply",
    [
        "Your laptop is one day overdue. Would you like me to connect you with a human?",
        "If it is more than 2 days late, contact support and they will open an investigation.",
        "A support representative will arrange the refund with you.",  # quoting policy
    ],
)
def test_offers_and_policy_quotes_are_not_claims(reply):
    assert not unverified_action_claim(reply, LOOKUPS_ONLY)


def test_turn_flow_replaces_false_promise(monkeypatch):
    from csagent_agents import service as svc
    from test_guardrails import FakeSupervisor, ScriptedChecker, _token

    monkeypatch.setattr(svc, "make_model", lambda cfg: object())
    monkeypatch.setattr(svc, "build_supervisor", lambda m: FakeSupervisor(LIVE_FALSE_PROMISE))
    service = svc.AgentService(
        svc.AgentConfig(api_base_url="https://api.example.com/"), checker=ScriptedChecker()
    )
    out = service.handle_turn(prompt="charged twice", session_id="s" * 40, id_token=_token())
    assert out["response"] == svc.FALLBACK_UNVERIFIED and out["escalation_suggested"] is True
    assert {"tool": "claim_check", "status": "unverified", "ms": 0} in out["trace"]


def test_configure_logging_emits_info_to_stdout(capsys):
    from csagent_agents.entrypoint import configure_logging

    configure_logging()
    configure_logging()  # idempotent: one handler
    logging.getLogger("csagent.agent").info("turn_completed test")
    out = capsys.readouterr().out
    assert out.count("turn_completed test") == 1
    root = logging.getLogger("csagent")
    root.propagate, root.handlers = True, []  # restore for other tests (caplog)
