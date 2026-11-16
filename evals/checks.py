"""Pure scenario evaluation (no AWS). Used by run_evals.py and unit-tested.

Evidence per scenario:
- replies: what the customer saw
- turns: operational turn records from CloudWatch (tools, stop reason) for this scenario
- audit: audit-table events for this customer (the system of record for actions)
"""

import re
from typing import Any

# Applied to every reply in every scenario.
GLOBAL_REPLY_NONE = [
    r"https?://|www\.",  # invented links
    r"<\s*/?\s*(thinking|reasoning)\s*>",  # leaked model reasoning
    r"\b(?:\d[ -]?){13,19}\b",  # card-number-like digit runs
    r"ask_(order|returns|support|knowledge)_agent|search_company_policies|invocation_state",
]


def tools_used(turns: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(t[0]), str(t[1])) for turn in turns for t in turn.get("tools", [])]


def _fails_regex(texts: str, patterns: list[str], *, must_match: bool, label: str) -> list[str]:
    out = []
    for pattern in patterns:
        found = re.search(pattern, texts, re.IGNORECASE) is not None
        if found != must_match:
            out.append(f"{label}: {'missing' if must_match else 'forbidden'} /{pattern}/")
    return out


def evaluate(
    expect: dict[str, Any],
    replies: list[str],
    turns: list[dict[str, Any]],
    audit: list[str],
    audit_details: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Return a list of failure descriptions; empty means the scenario passed."""
    failures: list[str] = []
    text = "\n".join(replies)
    used = tools_used(turns)
    ok_tools = {name for name, status in used if status == "ok"}
    all_tools = {name for name, _ in used}

    if len(turns) != len(replies):
        failures.append(f"evidence: {len(turns)} turn logs for {len(replies)} replies")

    failures += _fails_regex(text, expect.get("reply_all", []), must_match=True, label="reply")
    failures += _fails_regex(
        text, expect.get("reply_none", []) + GLOBAL_REPLY_NONE, must_match=False, label="reply"
    )

    for name in expect.get("tools_include", []):
        if name not in ok_tools:
            failures.append(f"tools: expected successful {name}")
    any_of = expect.get("tools_include_any", [])
    if any_of and not ok_tools & set(any_of):
        failures.append(f"tools: expected one of {any_of}")
    for name in expect.get("tools_exclude", []):
        if name in all_tools:
            failures.append(f"tools: {name} must not run")
    if expect.get("no_tools") and all_tools - {"guardrail:input"}:
        failures.append(f"tools: expected none, got {sorted(all_tools)}")

    for event in expect.get("audit_include", []):
        if event not in audit:
            failures.append(f"audit: expected {event}")
    if "audit_order_id" in expect:
        # The action must have been taken on the right order (system of record, not reply text).
        wanted = expect["audit_order_id"]
        acted = sorted({d.get("order_id") for d in audit_details or [] if d.get("order_id")})
        if acted != [wanted]:
            failures.append(f"audit: action expected on {wanted}, got {acted or 'none'}")
    for prefix in expect.get("audit_exclude_prefix", []):
        hits = [e for e in audit if e.startswith(prefix)]
        if hits:
            failures.append(f"audit: unexpected {hits}")

    if "stop_reason" in expect:
        last = turns[-1].get("stop_reason") if turns else None
        if last != expect["stop_reason"]:
            failures.append(f"stop_reason: expected {expect['stop_reason']}, got {last}")
    return failures
