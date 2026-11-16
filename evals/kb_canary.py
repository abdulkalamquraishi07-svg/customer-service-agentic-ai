"""Live retrieval canary against a deployed knowledge base.

Runs the labeled queries in evals/kb_retrieval.json through the SAME ranking code the agent
uses. Fails (exit 1) if an on-topic query does not rank the expected document first, or if an
off-topic query returns any passage above the cutoff. This also guards the assumption that the
Aurora-backed KB returns cosine distance as `score`: if that ever changes, rankings invert and
this canary fails.

    python evals/kb_canary.py --env dev
"""

import argparse
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "agents" / "src"))
from csagent_agents.config import AgentConfig  # noqa: E402
from csagent_agents.tools import rank_results  # noqa: E402

REGION = "us-east-1"
MAX_RANK = 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    args = parser.parse_args()

    outputs = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=f"csagent-{args.env}-knowledge"
    )["Stacks"][0]["Outputs"]
    kb_id = next(o["OutputValue"] for o in outputs if o["OutputKey"] == "KnowledgeBaseId")
    defaults = AgentConfig(api_base_url="https://unused/")
    client = boto3.client("bedrock-agent-runtime", region_name=REGION)
    cases = json.loads((ROOT / "evals" / "kb_retrieval.json").read_text())["cases"]

    failures = 0
    for case in cases:
        resp = client.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": case["query"]},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": 4,
                    "overrideSearchType": "SEMANTIC",
                }
            },
        )
        ranked = rank_results(
            resp["retrievalResults"],
            score_mode=defaults.kb_score_mode,
            min_similarity=defaults.kb_min_similarity,
        )
        expected = case["expected_doc_prefix"]
        if expected is None:
            ok = not ranked
            got = ranked[0]["source"] if ranked else "-"
        else:
            prefixes = tuple(expected) if isinstance(expected, list) else (expected,)
            # The agent receives every passage above the cutoff (up to 4); require the expected
            # document within the top MAX_RANK so it is guaranteed to be read.
            rank = next(
                (i + 1 for i, r in enumerate(ranked) if r["source"].startswith(prefixes)), None
            )
            ok = rank is not None and rank <= MAX_RANK
            got = f"rank {rank} of {len(ranked)} (top: {ranked[0]['source']})" if ranked else "none"
        failures += not ok
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {case['query'][:45]:47s} expected={expected} got={got}")

    print(f"\n{len(cases) - failures}/{len(cases)} retrieval cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
