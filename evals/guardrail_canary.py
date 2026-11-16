"""Live canary for the deployed chat guardrail (ApplyGuardrail, ~$0.01 per run).

Uses the same parsing code as the agent. Fails (exit 1) on any false positive (a legitimate
support message blocked) or false negative (an attack or abusive reply allowed).

    python evals/guardrail_canary.py --env dev
"""

import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agents" / "src"))
from csagent_agents.guard import BedrockGuardrail  # noqa: E402

REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    args = parser.parse_args()

    runtime_env = None
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    for rt in control.list_agent_runtimes()["agentRuntimes"]:
        if rt["agentRuntimeName"] == f"csagent_{args.env}_supervisor":
            detail = control.get_agent_runtime(agentRuntimeId=rt["agentRuntimeId"])
            runtime_env = detail["environmentVariables"]
    if not runtime_env:
        print("agent runtime not found")
        return 1
    # Test exactly the guardrail id/version the deployed runtime uses.
    guard = BedrockGuardrail(
        boto3.client("bedrock-runtime", region_name=REGION),
        runtime_env["GUARDRAIL_ID"],
        runtime_env["GUARDRAIL_VERSION"],
    )
    print(f"guardrail {runtime_env['GUARDRAIL_ID']} v{runtime_env['GUARDRAIL_VERSION']}")

    cases = json.loads((ROOT / "evals" / "guardrail_cases.json").read_text())["cases"]
    failures = 0
    for case in cases:
        v = guard.check(case["text"], case["source"])
        got = "block" if v.blocked else "mask" if v.masked else "allow"
        ok = got == case["expect"]
        failures += not ok
        detail = f" {v.reasons}" if v.reasons else ""
        print(f"[{'PASS' if ok else 'FAIL'}] {case['source']:6s} expect={case['expect']:5s} "
              f"got={got:5s}{detail} | {case['text'][:55]}")
    print(f"\n{len(cases) - failures}/{len(cases)} guardrail cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
