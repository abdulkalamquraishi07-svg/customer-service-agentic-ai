# ADR 0001: Identity propagation and customer data isolation

Status: Accepted

## Context
Agents are driven by an LLM that can be influenced by user text, retrieved documents and tool
output (prompt injection). If the model chooses which customer's data a tool reads, a single
injected instruction could expose another customer's orders.

## Decision
1. Customer identity is the Cognito `sub`, taken only from the JWT verified by API Gateway.
2. The identity is passed to agents out of band (Strands `invocation_state`), never as a
   model-visible or model-controllable tool argument.
3. Customer-owned DynamoDB tables use `customer_id` as the partition key. Tool IAM roles can
   add `dynamodb:LeadingKeys` conditions as defense in depth.
4. Self sign-up is a per-environment flag (`self_sign_up` in `infra/config.py`): on in dev, off in staging and prod. A post-confirmation trigger adds self-registered users to `customers` only; staff groups are always assigned by an administrator. In dev the trigger also seeds sample orders.

## Consequences
- Cross-customer access requires bypassing authentication, not just persuading the model.
- Access patterns that are not per-customer (ops console queues) use GSIs and a separate role.
- Every tool must read identity from context and is covered by a cross-customer test.
