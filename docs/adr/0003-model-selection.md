# ADR 0003: Foundation model selection (Amazon Nova Pro)

Status: Accepted (provisional, pending an eval suite)

## Context
The design assumed Anthropic Claude on Bedrock. The account has no Anthropic use-case form on
file, so Claude calls fail. We need a model that supports Bedrock Converse tool use, works with
Strands "agents as tools", and stays grounded in tool results.

## Evaluation (dev, 2026-11-12)
1. Single tool-call probe: Nova 2 Lite, Nova Pro, gpt-oss-120b, Llama 4 Maverick and Qwen3 Next
   80B all emitted a correct tool call. Nova Premier: end of life.
2. Full multi-agent run of the reference scenario ("laptop ordered five days ago hasn't arrived")
   plus a prompt-injection prompt, against the deployed API:
   - gpt-oss-120b: returned raw function-call text as its final answer (incompatible output).
   - Nova 2 Lite: correct facts; made an ungrounded policy claim without consulting policy.
   - Nova Pro: correct facts; consulted policy, reported missing evidence honestly, escalated to
     a human with a ticket. Both Nova models refused the injection.

An early run exposed a tool-data gap (order list lacked product names, so "the laptop" could not
be matched); fixed in the API before the comparison above.

## Decision
Use `us.amazon.nova-pro-v1:0` for all agents. The model id is configuration (`MODEL_ID`).

## Consequences / follow-ups
- Sample size is one scenario per model: not statistically meaningful. An offline eval suite
  (routing accuracy, groundedness, injection) must confirm this choice and gate future changes.
- Nova Pro used ~1.6x the tokens of Nova 2 Lite here. A tiered setup (cheaper model for
  specialists) is a later cost optimization, only if evals show no grounding regression.
- Cross-customer access is prevented in code (no tool accepts a customer id; API enforces the
  JWT), so model refusals are defense in depth, not the primary control.
