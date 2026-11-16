# Production readiness: what this POC uses, and what production would use instead

This is a proof of concept with sample data, running in one AWS dev environment. The left column is
what is actually built and tested here; the right column is what a production deployment would add
or replace, and why. Everything marked "built" has tests and was verified live in dev.

## 1. Already production-style in the POC (built and tested)
| Area | What is implemented |
|---|---|
| Identity propagation | Customer identity comes only from the verified Cognito JWT; no tool accepts a customer id; cross-customer access blocked and red-team tested |
| Business safety | Deterministic rules engine for eligibility, refund state machine, idempotency keys, optimistic locking, human approval above INR 10,000 with expiry, separation of duties, daily refund cap, append-only audit table |
| Reliability | SQS + DLQ for refunds, partial batch failures, reconciler for lost messages, retries with backoff, circuit breaker, timeouts |
| AI safety | Bedrock Guardrails on input and output, card numbers masked, verified-action check (no "I will escalate" without a ticket), reply grounding check (ADR 0007), per-request turn/token caps, invented links stripped |
| Abuse and cost | WAF and throttling on the API; per-customer rate limits (6/min, 60/h, 250/day) and a daily token budget on the agent path |
| Observability | Structured logs without customer text, agent dashboard, 7 agent alarms + API/DLQ alarms |
| Quality gates | 340+ unit/integration tests, smoke test, KB/guardrail/grounding/rate-limit canaries, 15-scenario end-to-end evals judged on audit records |
| Delivery | CDK for every resource, GitHub Actions CI/CD with OIDC (no stored keys), pinned actions, environment-gated promotion |
| Recovery | DynamoDB point-in-time recovery, restore drill (77/77 items, 269 s) |

## 2. POC substitutes and their production replacements
| Area | POC (what is used now) | Production | Why |
|---|---|---|---|
| Payments | Simulated gateway: a DynamoDB ledger, idempotent on the refund id | Real payment provider (Stripe, Razorpay, Adyen) behind the same `PaymentGateway` interface, with webhooks for async confirmation | Real money movement; the idempotency contract is already enforced, so only the adapter changes |
| Data | Seeded sample orders and policy documents | Integration with the real order management system (APIs or EventBridge events) and the company's policy content owners | The agent's tools call an API, so the source can be swapped without touching the agents |
| Customer accounts | Scripted demo users with permanent passwords, plus dev-only self sign-up (email code, Cognito default email sender, sample orders seeded on confirmation) | Federation with the company's existing customer identity (OIDC/SAML), or self sign-up with SES email from a verified domain, Cognito threat protection, and bot protection (CAPTCHA/WAF) on sign-up. Each sign-up gets its own agent rate limit, so open sign-up needs bot protection to cap model spend | Real customers already have accounts; default sender is capped at about 50 emails/day |
| Domain and TLS | CloudFront default domain (`*.cloudfront.net`), API Gateway default URL | Route 53 + ACM certificates, custom domains for web, API and Cognito | Brand trust, stable URLs, cookie and CSP scoping |
| AWS accounts | One account, deployed with root credentials from a laptop | AWS Organizations with separate dev/staging/prod accounts, IAM Identity Center with MFA for people, root locked away, deploys only through the OIDC pipeline (already built) | Blast radius, least privilege, auditability |
| Environments | dev only (staging/prod configs exist, never deployed) | staging and prod, promoted by the pipeline after evals pass | Changes are proven before customers see them |
| Network | dev Lambdas outside the VPC; agent runtime in PUBLIC network mode | Lambdas and AgentCore in VPC mode with interface endpoints (configured for staging/prod); private API where possible | Keep traffic off the internet (about USD 7 per endpoint per AZ per month) |
| Web protection | WAF on the API only | WAF on CloudFront too, with Bot Control | The SPA and its auth flows are public |
| Human handoff | Minimal ops console (approvals, escalated tickets) | Integration with a helpdesk / contact center (Zendesk, ServiceNow, Amazon Connect) for live handoff and SLAs | Agents need queues, SLAs and reporting |
| Notifications | None to customers (alarm emails only) | SES / SNS / WhatsApp notifications for refund approvals and ticket updates | Customers should not have to ask |
| Model capacity | Amazon Nova Pro on demand | Provisioned throughput or cross-Region inference, quota increases, prompt caching, model re-evaluation before any switch (ADR 0003 process) | Predictable latency and cost under load |
| Grounding check | Classic tier, threshold 0.05 (catches clear inventions only) | Standard tier (conversational), re-calibrated with the same canary and a shadow run | Classic tier cannot catch subtler errors without blocking correct replies |
| Responses | Whole reply after 5-40 s, with a progress timer | Streaming responses (AgentCore streaming) to the UI | Perceived latency |
| Vector store | Aurora Serverless v2 scaled to 0 in dev (cold start up to 75 s, mitigated by warm-up) | Minimum 0.5 ACU (already configured for prod) | No cold starts for customers |
| Audit archive | DynamoDB audit table with PITR | DynamoDB Streams to S3 with Object Lock (WORM) and a retention policy | Tamper-evident long-term records |
| Disaster recovery | PITR on DynamoDB, one restore drill | AWS Backup plans with cross-Region copies, tested restores for Aurora, S3 and Cognito, documented RPO/RTO | Recover from account- or Region-level failures |
| On-call | SNS topic (email) | Paging (PagerDuty / Opsgenie / AWS Incident Manager), runbooks, SLO reviews | Someone must be woken up |
| Load and resilience | Not tested | Load tests (k6 or Distributed Load Testing on AWS), fault injection (AWS FIS) | Know the limits before customers find them |
| Security assurance | Automated red-team evals, dependency audits, IAM policy tests | Threat model, external penetration test, SAST (CodeQL), secret scanning, CDK upgrade (pinned 2.170 has an advisory for an unused feature) | Independent assurance |
| Cost control | Rate limits, token budget, token-spend alarm | AWS Budgets and Cost Anomaly Detection, per-tenant cost reporting | Catch spend surprises early |
| Privacy | No real personal data | Data retention and deletion workflow (DPDP / GDPR), data-processing agreements for model providers | Legal obligations |

## 3. How to explain the architecture in one minute
Customers chat through a React app on CloudFront. They sign in with Cognito, and the browser calls
a Bedrock AgentCore runtime that runs a Strands supervisor agent with four specialists (orders,
returns, support, knowledge). Specialists never touch databases: they call the business API
(API Gateway, Lambda, DynamoDB) with the customer's own token, so a prompt injection cannot reach
another customer's data. Policy answers come from a Bedrock Knowledge Base on Aurora pgvector.
Money moves only through a deterministic refund state machine with idempotency, human approval
above a threshold and an audit trail. Guardrails, a verified-action check and a grounding check
stand between the model and the customer, and every change is gated by end-to-end evals that check
the audit records, not just the reply text.
