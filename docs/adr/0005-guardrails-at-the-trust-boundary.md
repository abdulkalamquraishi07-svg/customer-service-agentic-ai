# ADR 0005: Bedrock Guardrails at the trust boundary, plus a verified-action check

Status: Accepted

## Context
Customer text is untrusted (prompt injection, abuse, pasted card numbers) and model output must
be safe to show. Strands can attach a guardrail to the model, which evaluates every model call.

## Decision
1. Call the ApplyGuardrail API twice per turn: on the customer's message (INPUT) before any agent
   runs, and on the final reply (OUTPUT) before the customer sees it.
   - Per-call attachment would evaluate ~6 calls and ~40 text units per policy per turn,
     including our own order data and policy text (cost and false positives). Boundary checks
     use ~3 text units per turn (about $0.001 at us-east-1 list prices).
2. Policy (infra/stacks/agent_stack.py):
   - PROMPT_ATTACK HIGH on input. HATE / SEXUAL HIGH both ways; VIOLENCE / MISCONDUCT medium on
     input, high on output.
   - INSULTS: NONE on input, HIGH on output. Angry customers are normal in support; they must
     reach the agents and get escalated, while the assistant must never insult.
   - PII: payment and credential types ANONYMIZE (masked, never blocked) on input and output.
     Dates, amounts, PIN, phone, name are excluded: they collide with order data. No profanity
     word list: word policies also apply to input and would block frustrated customers.
3. Fail closed: if ApplyGuardrail errors, the turn fails with a generic message; unchecked text
   never crosses the boundary.
4. The runtime pins a published guardrail version (never DRAFT); a policy fingerprint in the
   version description forces a new version when the policy changes.
5. Verified-action check (deterministic, in code): a reply that claims an escalation or ticket
   ("I will escalate", "ticket number", ...) is replaced with an honest offer unless
   create_support_ticket / escalate_to_human succeeded in the same turn.

## Findings that shaped this (live, dev)
- PII configured with only `Action` masked card numbers on OUTPUT but never on INPUT. Explicit
  InputAction/InputEnabled/OutputAction/OutputEnabled are now set (property override; the
  pinned CDK version does not expose them) and pinned by a test.
- The assistant once replied "I will escalate this issue" with no ticket created (audit table
  confirmed). The prompt now routes payment problems to the support agent, and the claim check
  makes such a reply impossible to deliver.
- `evals/guardrail_canary.py` (13 labeled cases: angry, "smashed", injections, card numbers,
  normal and abusive replies) passes 13/13 against the deployed version.

## Consequences
- Guardrail checks add ~1 s per turn; a blocked injection returns in ~1.4 s without model calls.
- A blocked reply leaves the original model message in the session history (not shown to the
  customer). Acceptable for now; revisit if it causes repeated blocks.
