"""Pre-flight check of the CI verify role, using the IAM policy simulator.

    python scripts/check_ci_permissions.py --env dev

The verify role can only be assumed from GitHub Actions (OIDC), so its permissions cannot be
exercised locally. This simulates every AWS call the post-deploy checks make (must be allowed)
and a set of calls that must stay denied (least privilege), against the real resource ARNs.
"""

import argparse
import sys

import boto3

REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    args = parser.parse_args()
    e = args.env

    acct = boto3.client("sts").get_caller_identity()["Account"]
    iam = boto3.client("iam")
    cf = boto3.client("cloudformation", region_name=REGION)
    ddb = boto3.client("dynamodb", region_name=REGION)

    def stack(name: str) -> str:
        return str(cf.describe_stacks(StackName=f"csagent-{e}-{name}")["Stacks"][0]["StackId"])

    def outputs(name: str) -> dict[str, str]:
        out = cf.describe_stacks(StackName=f"csagent-{e}-{name}")["Stacks"][0]["Outputs"]
        return {o["OutputKey"]: o["OutputValue"] for o in out}

    def table(name: str) -> str:
        return f"arn:aws:dynamodb:{REGION}:{acct}:table/csagent-{e}-{name}"

    role = f"arn:aws:iam::{acct}:role/csagent-{e}-ci-verify"
    key = ddb.describe_table(TableName=f"csagent-{e}-orders")["Table"]["SSEDescription"][
        "KMSMasterKeyArn"]
    pool_id = outputs("auth")["UserPoolId"]
    pool = boto3.client("cognito-idp", region_name=REGION).describe_user_pool(
        UserPoolId=pool_id)["UserPool"]["Arn"]
    runtime = outputs("agent")["AgentRuntimeArn"]
    kb_id = outputs("knowledge")["KnowledgeBaseId"]
    kb = f"arn:aws:bedrock:{REGION}:{acct}:knowledge-base/{kb_id}"
    listed = boto3.client("bedrock", region_name=REGION).list_guardrails()["guardrails"]
    guardrails = [g["arn"] for g in listed if g["name"].startswith(f"csagent-{e}-")]
    runtime_logs = (
        f"arn:aws:logs:{REGION}:{acct}:log-group:/aws/bedrock-agentcore/runtimes/"
        f"{runtime.rsplit('/', 1)[1]}-DEFAULT:*"
    )

    must_allow = [
        ("stack outputs", "cloudformation:DescribeStacks",
         [stack(s) for s in ("auth", "api", "agent", "knowledge")]),
        *[("test users", f"cognito-idp:{a}", [pool]) for a in (
            "AdminCreateUser", "AdminSetUserPassword", "AdminAddUserToGroup", "AdminGetUser",
            "AdminDeleteUser")],
        ("seed orders", "dynamodb:PutItem", [table("orders")]),
        ("read evidence", "dynamodb:Scan", [table("ledger"), table("audit")]),
        ("cleanup", "dynamodb:Query", [table("orders"), table("returns"), table("tickets")]),
        ("cleanup", "dynamodb:DeleteItem", [table("orders"), table("returns"), table("tickets")]),
        ("table key", "kms:Decrypt", [key]),
        ("table key", "kms:GenerateDataKey", [key]),
        ("turn logs", "logs:FilterLogEvents", [runtime_logs]),
        ("runtime", "bedrock-agentcore:ListAgentRuntimes", ["*"]),
        ("runtime", "bedrock-agentcore:GetAgentRuntime", [runtime]),
        ("canaries", "bedrock:ApplyGuardrail", guardrails),
        ("canaries", "bedrock:Retrieve", [kb]),
        ("kb sync", "bedrock:StartIngestionJob", [kb]),
        ("kb sync", "bedrock:GetIngestionJob", [kb]),
    ]
    other = "prod" if e != "prod" else "dev"
    must_deny = [
        ("audit tamper", "dynamodb:PutItem", [table("audit")]),
        ("audit delete", "dynamodb:DeleteItem", [table("audit")]),
        ("ledger tamper", "dynamodb:PutItem", [table("ledger")]),
        ("iam", "iam:CreateUser", ["*"]),
        ("stack write", "cloudformation:UpdateStack", [stack("api")]),
        ("other env", "cloudformation:DescribeStacks",
         [f"arn:aws:cloudformation:{REGION}:{acct}:stack/csagent-{other}-api/x"]),
    ]

    problems = total = 0
    for group, want in ((must_allow, "allowed"), (must_deny, "denied")):
        for name, action, resources in group:
            for resource in resources:
                total += 1
                decision = iam.simulate_principal_policy(
                    PolicySourceArn=role, ActionNames=[action], ResourceArns=[resource]
                )["EvaluationResults"][0]["EvalDecision"]
                if (decision == "allowed") != (want == "allowed"):
                    problems += 1
                    print(f"[FAIL] {name:14s} {action:40s} {decision} (want {want}) "
                          f"...{resource[-50:]}")
    if not guardrails:
        problems += 1
        print("[FAIL] no guardrails found for this environment")
    print(f"simulated {total} calls for {role.rsplit('/', 1)[1]}; problems: {problems}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
