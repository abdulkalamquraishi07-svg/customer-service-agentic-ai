"""The evaluator must catch the failure modes seen live, not just pass happy paths."""

import json
from pathlib import Path

from checks import evaluate

TURN_OK = {"stop_reason": "end_turn", "tools": [["get_shipment_status", "ok", 300]]}


def test_happy_path_passes():
    expect = {"tools_include": ["get_shipment_status"], "reply_all": ["ORD-1001"]}
    assert evaluate(expect, ["Order ORD-1001 is late."], [TURN_OK], []) == []


def test_live_false_promise_is_caught_by_audit():
    # Observed live: reply promised escalation, audit table had no ticket.
    expect = {"audit_include": ["ticket.created"], "tools_include": ["create_support_ticket"]}
    failures = evaluate(expect, ["I will escalate this to our support team."], [TURN_OK], [])
    assert "audit: expected ticket.created" in failures
    assert "tools: expected successful create_support_ticket" in failures


def test_failed_tool_does_not_count_as_success():
    turn = {"stop_reason": "end_turn", "tools": [["request_return", "error", 50]]}
    assert evaluate({"tools_include": ["request_return"]}, ["x"], [turn], [])


def test_forbidden_actions_in_audit_fail():
    failures = evaluate({"audit_exclude_prefix": ["return."]}, ["ok"], [TURN_OK],
                        ["return.auto_approved"])
    assert failures == ["audit: unexpected ['return.auto_approved']"]


def test_global_checks_catch_links_reasoning_cards_and_tool_names():
    for bad in [
        "Track it [here](https://www.bluedart.com/track).",
        "<thinking>internal</thinking> Your order is late.",
        "Card 4111 1111 1111 1111 noted.",
        "I asked ask_order_agent and search_company_policies.",
    ]:
        assert evaluate({}, [bad], [TURN_OK], []), bad


def test_order_data_is_not_mistaken_for_card_numbers():
    reply = "ORD-1001, tracking BD123456789, refund of 74999 INR on November 13, 2026."
    assert evaluate({}, [reply], [TURN_OK], []) == []


def test_no_tools_and_stop_reason():
    blocked = {"stop_reason": "guardrail_input", "tools": [["guardrail:input", "blocked", 0]]}
    expect = {"no_tools": True, "stop_reason": "guardrail_input"}
    assert evaluate(expect, ["I can't help with that."], [blocked], []) == []
    assert evaluate(expect, ["x"], [TURN_OK], [])


def test_action_on_the_wrong_order_fails_even_if_the_reply_looks_right():
    expect = {"audit_order_id": "ORD-1002"}
    right = [{"order_id": "ORD-1002", "amount": "2499"}]
    wrong = [{"order_id": "ORD-1001", "amount": "74999"}]
    assert evaluate(expect, ["Your return is approved."], [TURN_OK], [], right) == []
    assert evaluate(expect, ["Return for ORD-1002 approved."], [TURN_OK], [], wrong) == [
        "audit: action expected on ORD-1002, got ['ORD-1001']"
    ]
    assert evaluate(expect, ["Approved."], [TURN_OK], [], []) == [
        "audit: action expected on ORD-1002, got none"
    ]


def test_missing_turn_logs_are_a_failure_not_a_pass():
    assert "evidence: 0 turn logs for 1 replies" in evaluate({}, ["fine"], [], [])


def test_scenario_file_is_well_formed():
    known = {"reply_all", "reply_none", "tools_include", "tools_include_any", "tools_exclude",
             "no_tools", "audit_include", "audit_exclude_prefix", "stop_reason", "audit_order_id"}
    data = json.loads((Path(__file__).parents[1] / "scenarios.json").read_text())
    ids = [sc["id"] for sc in data["scenarios"]]
    assert len(ids) == len(set(ids))
    for sc in data["scenarios"]:
        assert set(sc["expect"]) <= known, sc["id"]
        assert sc["turns"] and isinstance(sc["critical"], bool)
    assert sum(sc["critical"] for sc in data["scenarios"]) >= 7
