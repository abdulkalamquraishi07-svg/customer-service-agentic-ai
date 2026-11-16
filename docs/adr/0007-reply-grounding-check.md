# ADR 0007: Reply grounding (hallucination) check

Status: Accepted (enforced at a deliberately low threshold; see limitations)

## Context
The end-to-end evals caught the assistant inventing a policy: after the knowledge base returned
nothing relevant, it told the customer "we do offer gift wrapping at checkout; the cost varies".
The system prompt already forbade this. Prompt rules alone cannot guarantee grounding.

## Decision
- Every reply that follows tool calls is scored against the tool results of the last 3 turns
  (follow-ups restate earlier facts) using Bedrock Guardrails *contextual grounding*
  (ApplyGuardrail with `grounding_source` / `query` qualifiers). Canned replies and turns with no
  tool results are not checked.
- A separate guardrail (`csagent-<env>-reply-grounding`, action NONE) only scores; the agent makes
  the decision from `GROUNDING_THRESHOLD`, so calibration needs no guardrail change.
- Below the threshold the reply is replaced with an honest fallback ("I couldn't confirm that...
  would you like a human?") and escalation is suggested. Errors fail closed.
- Rolled out in shadow mode first (score and record, never block); `GROUNDING_MODE` switches.

## Calibration (dev, 2026-11-15)
| Data | Result |
|---|---|
| Canary, 10 replies on real API + knowledge-base evidence | correct >= 0.88; clear inventions 0.00-0.03; wrong carrier 0.16-0.19 |
| Shadow run of the full eval suite (15/15 scenarios passed, i.e. replies judged correct) | 15 replies scored 0.10-0.99; 6 below 0.5, mostly ticket/escalation confirmations |

A threshold of 0.5 would have blocked 40% of correct replies. **0.05** blocks the clear
inventions and blocked no correct reply in this sample. After enforcing at 0.05: canary has no
misclassification, eval suite 14/15 (the failure was unrelated: product matching, since fixed).

## Limitations (stated, not hidden)
- CloudFormation exposes only the **Classic** tier, which penalizes conversational wording. Real
  correct replies score as low as 0.10, so subtler errors (a wrong carrier name, 0.19) are not
  caught; the canary reports this case as a known gap.
- Margin between the threshold (0.05) and the lowest correct reply observed (0.10) is thin and
  measured on a small sample. `GroundingScore` p10/p50 is on the dashboard for monitoring.
- Production path: the **Standard** tier (built for conversational responses; needs a
  cross-Region guardrail profile), created via API/custom resource until CloudFormation supports
  it, then re-calibrated with the same canary and a shadow run.
