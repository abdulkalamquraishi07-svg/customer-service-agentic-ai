"""Show real agent replies with their grounding scores (shadow mode, nothing is blocked).

    python evals/grounding_probe.py --env dev "question 1" "question 2" ...

Runs the agents locally against the deployed API, knowledge base and guardrails, as a fresh test
customer, and prints each reply with its score. Use it to calibrate GROUNDING_THRESHOLD on real
replies rather than hand-written ones.
"""

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
for sub in ("scripts", "services/core/src", "services/agents/src"):
    sys.path.insert(0, str(ROOT / sub))
import smoke_test as s  # noqa: E402
from seed_data import build_orders  # noqa: E402

REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    parser.add_argument("questions", nargs="+")
    args = parser.parse_args()

    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    rt = next(r for r in control.list_agent_runtimes()["agentRuntimes"]
              if r["agentRuntimeName"] == f"csagent_{args.env}_supervisor")
    runtime_env = control.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])[
        "environmentVariables"]
    for key in ("API_BASE_URL", "MODEL_ID", "KNOWLEDGE_BASE_ID", "GUARDRAIL_ID",
                "GUARDRAIL_VERSION", "GROUNDING_GUARDRAIL_ID", "GROUNDING_GUARDRAIL_VERSION",
                "GROUNDING_THRESHOLD"):
        os.environ[key] = runtime_env[key]
    os.environ["GROUNDING_MODE"] = "shadow"  # see every reply; block nothing
    os.environ.pop("RATE_LIMIT_TABLE", None)

    from csagent_agents.config import AgentConfig
    from csagent_agents.service import AgentService

    out = s.outputs(args.env)
    idp = boto3.client("cognito-idp", region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)
    email = f"probe.{uuid.uuid4().hex[:6]}@example.com"
    sub, password = s.ensure_user(idp, out["UserPoolId"], email, "customers")
    try:
        for item in build_orders(sub, datetime.now(UTC)):
            ddb.Table(f"csagent-{args.env}-orders").put_item(Item=item)
        token = s.login(out["UserPoolId"], out["WebClientId"], email, password)
        service = AgentService(AgentConfig.from_env())
        for question in args.questions:
            turn = service.handle_turn(
                prompt=question, session_id=f"probe-{uuid.uuid4().hex}", id_token=token
            )
            check = next((t for t in turn["trace"] if t["tool"] == "grounding_check"), None)
            score = check["score"] if check else None
            label = "not checked" if check is None else f"score {score:.2f} ({check['status']})"
            print(f"\nQ: {question}\n   {label} | tools: "
                  f"{[t['tool'] for t in turn['trace'] if t['tool'] != 'grounding_check']}")
            print(f"   A: {turn['response'][:400]}")
        return 0
    finally:
        s.cleanup_customer(idp, ddb, args.env, out["UserPoolId"], email, sub)


if __name__ == "__main__":
    raise SystemExit(main())
