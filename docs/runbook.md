# Runbook

Region `us-east-1`. Stacks per environment: `csagent-<env>-{network,data,auth,web-hosting,api,knowledge,agent,web-content}`.
All commands run from the repo root with the virtualenv active.

## 1. Enable the CI/CD pipeline (one-time, by an administrator)
1. Create the GitHub repository and push. Under Settings > Environments create `dev` (and later
   `staging`, `prod`). For `staging` and `prod` add required reviewers and restrict deployment to `main`.
2. Deploy the identity stacks (use credentials of an administrator, not the pipeline):
   ```
   python scripts/build_agent.py ; cd web; npm ci; npm run build; cd ..
   cd infra
   cdk deploy csagent-github-oidc -c github_oidc=true            # once per AWS account
   cdk deploy csagent-dev-cicd -c env=dev -c github_repo=<owner>/<repo>
   ```
3. Copy the stack outputs `DeployRoleArn` / `VerifyRoleArn` into the GitHub environment `dev` as
   variables `AWS_DEPLOY_ROLE_ARN` / `AWS_VERIFY_ROLE_ARN`.
4. Optional pre-flight: `python scripts/check_ci_permissions.py --env dev` simulates every AWS
   call the post-deploy checks make as the verify role (the role itself can only be assumed from
   GitHub). Last run: 34 calls, 0 problems.
5. Set the repository variable `ENABLE_DEV` to `true` and push to `main` (without it, pushes run CI only).
   Staging and prod run only after you set the repository variables
   `ENABLE_STAGING` / `ENABLE_PROD` to `true` (staging adds interface VPC endpoints: roughly
   USD 7 per endpoint per AZ per month, so about USD 100/month before any traffic).

## 2. Alarms (SNS topic `csagent-<env>-alarms`; add `-c alarm_email=you@example.com` to subscribe)
| Alarm | Meaning | First action |
|---|---|---|
| `refunddlqnotempty` | A refund message failed 5 times | Inspect the DLQ message; the refund is NOT executed. Fix the cause, then redrive from the SQS console. Execution is idempotent, so redelivery is safe. |
| `*errors` (Lambda) | Handler exceptions | CloudWatch log group `/aws/lambda/csagent-<env>-<function>`; filter by `request_id`. |
| `api-5xx` | API Gateway 5xx | Same, start with `customerapi`/`opsapi`. |

Agent turn logs (no customer text) are in `/aws/bedrock-agentcore/runtimes/csagent_<env>_supervisor-*-DEFAULT`:
search `turn_completed` (tools, stop reason, tokens, latency) or `kb_retrieve_failed`.

## 3. Common procedures
- **Refund stuck in APPROVED/FAILED:** the scheduler (every 15 min) re-enqueues anything unexecuted for 10 minutes.
  A refund blocked by the daily cap stays APPROVED and runs on the next day's reconciliation.
- **Policy search says "temporarily unavailable" in dev/staging:** the knowledge DB auto-pauses after
  10-60 minutes idle; resume takes 15-75 s (measured). The agent warms it at session start and waits
  up to 90 s. Prod keeps 0.5 ACU running.
- **Change policy documents:** edit `data/knowledge/`, deploy, then `python scripts/sync_knowledge.py --env <env>`
  (the pipeline does this). `pytest` checks that documents match the backend rules (windows, thresholds).
- **Change the model:** set `MODEL_ID` (agent stack) and run `python evals/run_evals.py --env dev` before promoting.
- **Change guardrail policy:** edit the lists in `infra/stacks/agent_stack.py`; a new immutable guardrail
  version is created. Verify with `python evals/guardrail_canary.py --env dev`.
- **Roll back the agent:** redeploy the previous commit (new AgentCore runtime version). Sessions in
  flight keep their microVM until idle timeout (15 min).
- **Reset test data:** `python scripts/create_demo_users.py --env dev` recreates demo users, rotates their permanent passwords and re-seeds their orders.
- **Sign-up check:** `python evals/signup_check.py` registers a throwaway user, checks it got the `customers` group and 4 demo orders, then deletes it. Sign-up emails use the Cognito default sender (about 50 emails/day per account); if codes stop arriving, that quota is the first suspect.
- **Browser check:** `python scripts/browser_repro.py --url <web url> --email <user> --password <pw>` signs in with headless Chromium, sends a message and reports page errors.

## 4. Disaster recovery

| Store | Protection configured | Tested |
|---|---|---|
| DynamoDB (orders, returns, tickets, idempotency, audit, ledger) | Point-in-time recovery (35 days), KMS CMK with rotation, deletion protection and retain policy in staging/prod | **Yes**, see drill below |
| Aurora (knowledge vectors) | Automated backups (1 day dev, 7 days staging/prod), snapshot on delete outside dev | No (vectors are re-creatable: run `sync_knowledge.py` against the S3 documents) |
| S3 documents and web bucket | Versioning, TLS-only, private | No |
| Cognito users | None (no backup mechanism configured) | No. **Gap:** users must be re-provisioned by the business |
| Audit trail | DynamoDB table with PITR and stream | Stream archival to S3 Object Lock is **not implemented** |
| Multi-region | None | None |

**Restore drill** (`python scripts/restore_drill.py --env dev --table audit`, 2026-11-14): restored
`csagent-dev-audit` to its latest restorable time under a temporary name: 77 of 77 items matched the
source, the restored table was KMS-encrypted, and it took **269 s** to become ACTIVE. That is one sample
on a tiny table; do not extrapolate it to a large production table (restore time depends on size).
The drill deletes the temporary table when done.

Restoring for real: PITR always restores into a NEW table (table names are fixed by CDK). Restore under a
temporary name, verify, then copy the needed items back into the live table (or recover individual
records). Stop the refund worker (disable the SQS event source) while repairing returns/ledger data.

Objectives (targets, not yet committed to a customer): RPO of seconds for DynamoDB (PITR); RTO
measured only for the drill above. A regional outage means redeploying the stacks in another region
from CDK and restoring data from backups there; cross-region copies are not configured.

## 5. Cost levers (dev)
Aurora scales to 0 when idle; AgentCore bills per active session second; Nova Pro is billed per token
(an end-to-end eval run is roughly 110k input and 6k output tokens). Tear down unused environments
with `cdk destroy` (dev only; staging/prod retain data stores on purpose).
