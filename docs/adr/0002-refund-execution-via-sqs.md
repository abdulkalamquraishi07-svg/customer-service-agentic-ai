# ADR 0002: Refund execution via SQS with a DynamoDB-backed approval state machine

Status: Accepted (supersedes the initial idea of using Step Functions `waitForTaskToken`)

## Context
Refunds are irreversible. They need human approval above a threshold, approval expiry,
retries on transient payment failures, and protection against duplicate payouts.

## Decision
- Approval state lives in the return record (DynamoDB) as an explicit state machine with
  optimistic locking, expiry timestamps and an audit trail.
- Approved refunds are executed asynchronously: the API/ops handler enqueues
  `{customer_id, return_id}` on SQS; a worker Lambda executes it.
- The queue has a DLQ (`maxReceiveCount=5`), partial batch responses, bounded concurrency (5),
  and a CloudWatch alarm on DLQ depth.
- Duplicates and redelivery are safe: execution is idempotent (state check + payment key =
  `return_id`).
- A scheduled Lambda (15 min) expires overdue approvals and re-enqueues APPROVED/FAILED refunds
  that sat unexecuted, covering the DB-write/enqueue dual-write gap.

## Alternatives considered
- Step Functions `waitForTaskToken`: good visual workflow, but requires persisting task tokens
  and a second source of truth for approval state. Revisit if approval chains become multi-step.
- Executing the refund inline in the approval request: simpler, but couples approver latency and
  errors to the payment provider and loses retry/DLQ semantics.

## Consequences
- At-least-once delivery is expected; every consumer must stay idempotent.
- A refund blocked by the daily cap is deferred and retried by the reconciler (not hot-looped).
- Payment provider is currently SIMULATED (DynamoDB ledger). A real provider adapter must keep
  the idempotent-on-key contract.
