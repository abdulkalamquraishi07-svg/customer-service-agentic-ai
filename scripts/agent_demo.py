"""Chat with the agents locally against the DEPLOYED business API and real Bedrock.

    python scripts/agent_demo.py --env dev "I ordered a laptop five days ago and it hasn't arrived"
Creates a temporary customer with demo orders and removes it afterwards.
"""

import argparse
import atexit
import json
import os
import sys
import uuid
from datetime import UTC, datetime

import boto3

sys.path.insert(0, "scripts")
sys.path.insert(0, "services/core/src")
sys.path.insert(0, "services/agents/src")
import smoke_test as s  # noqa: E402
from csagent_agents.config import AgentConfig  # noqa: E402
from csagent_agents.service import AgentService  # noqa: E402
from seed_data import build_orders  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev")
    parser.add_argument("prompts", nargs="+")
    args = parser.parse_args()

    out = s.outputs(args.env)
    pool, client = out["UserPoolId"], out["WebClientId"]
    idp = boto3.client("cognito-idp", region_name=s.REGION)
    ddb = boto3.resource("dynamodb", region_name=s.REGION)

    email = f"demo.customer.{uuid.uuid4().hex[:8]}@example.com"
    sub, password = s.ensure_user(idp, pool, email, "customers")
    atexit.register(s.cleanup_customer, idp, ddb, args.env, pool, email, sub)
    for item in build_orders(sub, datetime.now(UTC)):
        ddb.Table(f"csagent-{args.env}-orders").put_item(Item=item)
    token = s.login(pool, client, email, password)

    os.environ["API_BASE_URL"] = out["ApiUrl"]
    if os.environ.get("KNOWLEDGE_BASE_ID"):
        print("(using knowledge base", os.environ["KNOWLEDGE_BASE_ID"], ")")
    service = AgentService(AgentConfig.from_env())
    session_id = uuid.uuid4().hex

    for prompt in args.prompts:
        print(f"\nCUSTOMER: {prompt}")
        turn = service.handle_turn(prompt=prompt, session_id=session_id, id_token=token)
        print(f"ASSISTANT: {turn['response']}")
        print("TRACE:", json.dumps([(t["tool"], t["status"]) for t in turn["trace"]]))
        print("USAGE:", turn["usage"], "| stop:", turn["stop_reason"])


if __name__ == "__main__":
    main()
