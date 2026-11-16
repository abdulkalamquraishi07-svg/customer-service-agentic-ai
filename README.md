# Multi-Agent Customer Service Automation (AWS, Strands Agents)

A supervisor agent routes customer requests to specialist agents (Knowledge/RAG, Order
Management, Returns & Refunds, Support). Facts and actions come from backend APIs; policy
answers are grounded in retrieved documents; unresolved or risky cases escalate to humans.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Repo, CDK (network, data, auth, API + WAF), seed data, infra tests | Done (not yet deployed) |
| 2 | Business core + API: orders, returns rules + refund state machine, tickets, Lambda handlers, SQS refund worker, scheduler, CDK wiring | Done; unit/integration tested (moto) and deployed to a personal AWS `dev` environment; live smoke test passes (18/18, repeatable) |
| 3 | Knowledge Base: S3 policy docs -> Bedrock KB -> Aurora Serverless v2 pgvector (scale-to-zero) | Deployed to dev; ingested; live retrieval canary 15/15 (`evals/kb_canary.py`) |
| 4 | Agents: Strands supervisor + Order, Returns, Support, Knowledge specialists on Amazon Nova Pro | Done; reference scenario verified live |
| 5 | Hosting on Bedrock AgentCore Runtime (direct code zip, arm64, Cognito JWT inbound auth) | Deployed to dev (`csagent-dev-agent`); verified with `scripts/invoke_runtime.py`: no-token and forged-token calls refused (401), laptop scenario answered from a cold (paused) vector store |
| 6 | Bedrock Guardrails on customer input and final reply + verified-action check (ADR 0005) | Deployed to dev; `evals/guardrail_canary.py` 13/13; injection refused, card numbers masked, false "I will escalate" promises blocked |
| 7 | End-to-end eval suite (`evals/run_evals.py`, 15 scenarios, evidence from reply + CloudWatch turn log + audit table) | 15/15 on two consecutive runs against hosted dev runtime v7 (first run: 11/15, failures fixed) |
| 8 | Web app: customer chat + orders panel, ops console (refund approvals, escalated tickets); S3 + CloudFront | Deployed to dev (https://d3t8s3rylz5sib.cloudfront.net); CORS, security headers, role gating and the chat->escalation->ops-queue path verified over HTTPS; not clicked through in a real browser (see below) |
| 9 | CI/CD (GitHub Actions, OIDC, gated promotion), reproducible builds, dependency audits, DynamoDB restore drill | Written and statically verified (actionlint, policy tests with mutation checks, offline `cdk synth` of all three environments, clean-environment test run). **The workflows have not run on GitHub yet** and the OIDC roles are not deployed (they need your GitHub repo name). DynamoDB PITR restore drilled in dev. See ADR 0006 and `docs/runbook.md` |
| 10 | Per-customer rate limits + token budget, agent dashboard and alarms, reply grounding (hallucination) check | Deployed to dev; live rate-limit check passes; grounding calibrated and enforced (ADR 0007) |
| 11 | CI/CD identity for `abdulkalamquraishi07-svg/customer-service-agentic-ai` | OIDC provider and dev deploy/verify roles deployed; CI passes on GitHub; the dev deploy job needs the role ARNs set as `dev` environment variables |

Scope: runs with sample data (fictional retailer, simulated payments) in one AWS dev environment.
See `docs/production-readiness.md` for what each stand-in component becomes in production.

## Web app (`web/`)
React 19 + TypeScript + Vite + Tailwind. Sign-in with Cognito (SRP, handles first-login password
change and TOTP). Customers see the chat and their orders; support agents see escalated tickets;
approvers see refund approvals; staff-only accounts land on the ops console. Role gating in the UI
is convenience only: the API and AgentCore authorize every call again.
- The chat calls the AgentCore runtime directly with the user's ID token (AgentCore validates the
  JWT; its CORS preflight allows browser calls) and the business API for the orders panel.
- One build serves every environment: endpoints load at runtime from `/config.json`, which CDK
  writes from SSM parameters published by the auth, api and agent stacks.
- CloudFront (OAC, TLS 1.2+, SPA fallback) sends CSP (no inline script/style, connect-src limited
  to Cognito, AgentCore and API Gateway), HSTS, X-Frame-Options DENY, nosniff, Referrer-Policy.
- Model text is rendered as plain text (only bold and line breaks), never as HTML; links are also
  stripped server-side.
- API CORS echoes only allowlisted origins (the CloudFront domain; localhost only in dev).
- Local dev: `python scripts/write_web_env.py --env dev; cd web; npm run dev`.
  Demo users: `python scripts/create_demo_users.py --env dev` (permanent dev-only passwords, printed once; re-running rotates them). New customers can also register on the sign-in page (dev only); they get sample orders automatically.
- Tests: `cd web; npm test` (14) and `npx tsc --noEmit`.

Not verified: an interactive browser session. The responses a browser depends on (preflight,
actual-response CORS headers, CSP, config, SPA routes) were checked directly over HTTPS, but the
UI itself has only unit tests. Responses take 5 to 40 seconds and the UI shows elapsed time but not
streamed text.

## Evaluation
| Suite | What it checks | Command | Latest result (dev) |
|---|---|---|---|
| Unit/integration | Rules, services, handlers, agent layer, infra policies, evaluator, workflow policy | `pytest` | 294 passed (also from a fresh virtualenv) |
| Smoke (business API) | Auth, isolation, idempotency, approval, SQS refund | `python scripts/smoke_test.py --env dev` | 18/18 |
| KB retrieval canary | 20 labeled queries, expected doc in top 2, off-topic returns nothing | `python evals/kb_canary.py --env dev` | 20/20 |
| Guardrail canary | 13 labeled messages: angry customers allowed, injections blocked, card numbers masked | `python evals/guardrail_canary.py --env dev` | 13/13 |
| End-to-end agent evals | 15 scenarios (routing, grounding, actions, policy enforcement, escalation, red-team); 9 critical | `python evals/run_evals.py --env dev` | 15/15 (x2) |

End-to-end scenarios are judged by deterministic checks, not by another model: tools that ran
(CloudWatch turn log), actions that happened (audit table) and reply patterns. A scenario that
claims an action without an audit event fails. Each scenario runs as a fresh customer.
The first run found three critical failures, all fixed in runtime v7: the returns specialist could
not map "my headphones" to an order and once named the wrong order; and cancellation requests
were answered with "contact customer service" instead of policy plus an offer to escalate.

Sample size: two full runs. Model behavior is non-deterministic; treat the suite as a regression
gate, not a statistical accuracy measurement.

Only what is listed as done is implemented.

## Phase 1 contents
- **Network:** VPC with isolated private subnets, no NAT, gateway endpoints (DynamoDB, S3),
  interface endpoints for Bedrock/SQS/Secrets/Logs/KMS/Step Functions in staging and prod,
  REJECT flow logs.
- **Data:** KMS-encrypted DynamoDB tables (orders, returns, tickets, idempotency w/ TTL,
  audit w/ stream), PITR, deletion protection and retain policies outside dev.
- **Auth:** Cognito (email self sign-up with code verification in dev only, new users join `customers` only; 12+ char passwords, optional TOTP MFA, groups).
- **Edge:** API Gateway with Cognito authorizer, request validation, throttling, usage plan,
  no body logging, WAF (AWS managed rules + per-IP rate limit).
- **Environments:** `dev | staging | prod` via `-c env=...` (see `infra/config.py`).

## Phase 2: business core (`services/core`)
- **Rules engine** (`policy.py`): pure functions decide return eligibility, refund amount and
  whether approval is needed. Category-specific return windows, approval threshold, daily cap.
- **Return state machine** (`models.py`): `PENDING_APPROVAL -> APPROVED|REJECTED|EXPIRED`,
  `APPROVED -> EXECUTED|FAILED`, `FAILED -> EXECUTED` (retry), `EXECUTED -> REVERSED`.
- **Safeguards** (`return_service.py`): idempotency keys, per-order duplicate guard,
  optimistic locking, approval expiry, separation of duties, idempotent gateway payout keyed
  by `return_id`, daily refund cap, append-only audit events.
- **Isolation:** every customer query is scoped by the caller's `customer_id`; another
  customer's resource returns `NotFound`.
- **Testing:** 71 tests; service tests run against both the in-memory and DynamoDB (moto)
  adapters. Safeguards were mutation-checked (disabling separation of duties, the daily cap or
  the order guard each makes tests fail).
- **Known gaps:** the payment gateway is SIMULATED (DynamoDB ledger, idempotent on key);
  `REVERSED` has no service method yet; dev runs Lambdas outside the VPC (staging/prod inside).

## Phase 2: API surface (`services/business_api`)
Customer (Cognito ID token): `GET /v1/orders`, `/v1/orders/{id}`, `/shipment`,
`/return-eligibility`; `POST /v1/returns` and `POST /v1/tickets` (require `Idempotency-Key`);
`GET /v1/returns/{id}`; `POST /v1/tickets/{id}/escalate`.
Ops: `GET /v1/ops/approvals`, `POST /v1/ops/returns/{customer_id}/{return_id}/decision`,
`POST /v1/ops/tickets/{customer_id}/{ticket_id}/resolve` (group checks enforced in the domain).
Edge JSON-schema models reject unknown fields (e.g. an injected `customer_id`).
Async refund execution: SQS + DLQ + alarm; 15-minute scheduler for expiry and reconciliation
(see ADR 0002).

Build the Lambda package (no Docker, cross-installs ARM64 wheels) before synth/deploy:
```powershell
python scripts/build_lambda.py
```
Run type checks with `MYPYPATH=services/core/src;services/business_api/src`.

## Operations
- `docs/runbook.md`: enabling the pipeline, alarms, common procedures, disaster recovery (what is
  protected, what was tested, what is not).
- `python scripts/restore_drill.py --env dev`: restores a DynamoDB table from point-in-time recovery,
  verifies it against the source and deletes it (measured: 77/77 items, 269 s, one small-table sample).

## Local setup (Windows PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest
ruff check .
cd infra; cdk synth -c env=dev      # run with the venv activated
```

## Deploy (dev) - incurs AWS cost, run deliberately
```powershell
cd infra
cdk bootstrap
cdk deploy --all -c env=dev
```
Use a least-privilege IAM role, not root credentials.

## Live smoke test (deployed dev/staging only)
```powershell
python scripts/smoke_test.py --env dev
```
Creates a fresh customer per run, logs in through Cognito SRP, and drives the real API, SQS and
worker: auth rejection, customer isolation, edge schema validation, idempotent replay,
duplicate-return guard, approval flow, async refund execution (exactly one payout), tickets.
It deletes its test users and data afterwards.

Verified live in `dev`: Lambda ARM64 packaging, Cognito group claims, API Gateway validation,
SQS -> worker execution, daily refund cap (deferral), alarms in OK state, DLQ empty.
Not verified: load/throttling behavior, staging/prod deployment, failure injection.

## Seed demo data
Create a Cognito user, copy its `sub`, then:
```powershell
python scripts/seed_data.py --env dev --customer-id <sub>
```

## Docs
- `docs/adr/` architecture decision records
- `docs/reports/pdf/1-poc-report.pdf`: what was built, tested and measured in dev
- `docs/reports/pdf/2-production-documentation-pack.pdf`: architecture, operations, security, compliance, AI governance, testing and go-live documents for production (rebuild with `python scripts/build_pdfs.py`)
- `cloudformation/prod/`: synthesized prod CloudFormation templates (not deployed)
