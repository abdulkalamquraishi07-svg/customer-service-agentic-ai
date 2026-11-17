# ADR 0006: CI/CD with GitHub Actions, OIDC and environment-gated promotion

Status: Accepted (pipeline written and statically verified; not yet run on GitHub)

## Context
Deployments so far were run from a laptop with root credentials. A production system needs
repeatable, reviewed, least-privilege deployments with automated quality gates.

## Decision
1. **CI (`ci.yml`, pull requests and as the first gate of every deployment)** needs no AWS access:
   ruff, workflow lint (actionlint), mypy, 294 Python tests, web typecheck/tests/build,
   dependency audits (pip-audit, npm audit), and an offline `cdk synth` of dev, staging and prod.
2. **CD (`deploy.yml`)**: main -> CI -> dev deploy -> dev verification -> staging -> prod. Each step
   runs in the GitHub *environment* of the same name, so branch rules and required reviewers are
   enforced by GitHub. Deployments are serialized (`concurrency: deploy`, no cancellation).
   Each environment deploys only when its repository variable is true (ENABLE_DEV, ENABLE_STAGING, ENABLE_PROD); with none set, pushes run CI only.
3. **No stored AWS keys.** Jobs authenticate with GitHub's OIDC token. Two roles per environment
   (stack `csagent-<env>-cicd`), each trusting exactly one subject,
   `repo:<owner>/<name>:environment:<env>` (no wildcards, enforced by a test):
   - deploy: may only `sts:AssumeRole` into CDK's bootstrap roles.
   - verify: only the data-plane actions the smoke test, canaries and evals use, scoped to that
     environment's resources; cannot write the audit, ledger or idempotency tables.
4. **Verification gates promotion.** After each deployment: health check, knowledge-base sync,
   smoke test, retrieval canary, guardrail canary and the end-to-end evals (pass rate >= 90% and
   zero critical failures). These create test customers, so prod gets only the health check and
   the knowledge sync.
5. **Supply chain.** Every third-party action is pinned to a commit SHA; `pull_request_target` is
   forbidden; checkouts do not persist credentials; no `secrets:` are used. Policy tests in
   `infra/tests/test_workflows.py` (mutation-checked) enforce all of this.
6. **Reproducible builds.** Dependencies are pinned. The agent zip uses fixed timestamps so
   identical inputs give identical bytes; otherwise every run would look like new code to CDK and
   create a new AgentCore runtime version.
7. The pipeline never deploys its own identity stack: an administrator deploys
   `csagent-github-oidc` (once per account) and `csagent-<env>-cicd`.

## Known trade-offs and gaps
- CDK's deploy role acts through CloudFormation's execution role, which is broad. The effective
  control against misuse is the GitHub environment (protected branches, required reviewers), not
  the IAM policy on the deploy role.
- `--require-approval never` is used because the approval prompt cannot run non-interactively.
  IAM and security-group changes are therefore reviewed in the pull request (and the
  staging/prod reviewer), not at deploy time.
- Rollback = CloudFormation automatic rollback on failure, or redeploying the previous commit.
  There is no canary or blue/green traffic shifting for the agent runtime.
- `GHSA-464c-974j-9xm6` (aws-cdk-lib, CodeBuild only) is ignored with a CI guard that fails if
  CodeBuild is ever used. Upgrading CDK (2.170 -> 2.253+) is a separate, reviewed change.
- The workflows are validated with actionlint and policy tests but have not executed on GitHub:
  the first real run may expose environment-specific problems (for example runner permissions).
