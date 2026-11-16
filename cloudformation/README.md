# CloudFormation templates (prod)

Generated with `cdk synth -c env=prod` from `infra/` (aws-cdk-lib 2.170.0). These are the templates
for the **prod** environment as configured in `infra/config.py`: 3 Availability Zones, Lambdas and
AgentCore in the VPC with interface endpoints, Aurora minimum 0.5 ACU, retained data stores,
deletion protection, 365-day logs, self sign-up off.

They have **not been deployed**. They are kept as a reviewable record of the production
infrastructure.

| Template | Contents |
|---|---|
| csagent-prod-network | VPC, isolated subnets, gateway and interface endpoints, flow logs |
| csagent-prod-data | KMS key, DynamoDB tables (PITR, deletion protection) |
| csagent-prod-auth | Cognito user pool, web client, groups |
| csagent-prod-web-hosting | S3 bucket, CloudFront distribution, security headers |
| csagent-prod-api | API Gateway, WAF, Lambda functions, SQS + DLQ, scheduler, alarms |
| csagent-prod-knowledge | Aurora Serverless v2 (pgvector), Bedrock Knowledge Base, S3 documents |
| csagent-prod-agent | AgentCore runtime, guardrails, rate-limit table, dashboard, alarms |
| csagent-prod-web-content | Web app deployment and runtime config |

## Deploying
Use CDK, not these files directly. The templates reference code packages (Lambda bundle, agent
zip, web build) that CDK uploads to the bootstrap bucket at deploy time:

```
python scripts/build_lambda.py ; python scripts/build_agent.py ; cd web ; npm ci ; npm run build ; cd ..
cd infra ; cdk deploy --all -c env=prod
```

In practice prod is deployed only by the GitHub Actions pipeline after staging passes (see
`docs/runbook.md` and `docs/reports/pdf/2-production-documentation-pack.pdf`).

Regenerate after infrastructure changes:
`cd infra ; cdk synth -c env=prod -o ../cdk.out.prod -q` and copy `*.template.json` here.
