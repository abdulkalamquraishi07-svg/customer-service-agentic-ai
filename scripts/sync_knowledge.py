"""Start a knowledge base ingestion job and wait for it to finish.

    python scripts/sync_knowledge.py --env dev
Run after deploying changed documents in data/knowledge/.
"""

import argparse
import sys
import time

import boto3

REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging", "prod"])
    args = parser.parse_args()

    cf = boto3.client("cloudformation", region_name=REGION)
    outputs = {
        o["OutputKey"]: o["OutputValue"]
        for o in cf.describe_stacks(StackName=f"csagent-{args.env}-knowledge")["Stacks"][0][
            "Outputs"
        ]
    }
    kb_id, ds_id = outputs["KnowledgeBaseId"], outputs["DataSourceId"]
    agent = boto3.client("bedrock-agent", region_name=REGION)
    job = None
    for attempt in range(12):  # the vector store may be auto-paused (scale-to-zero)
        try:
            job = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)
            break
        except agent.exceptions.ValidationException as exc:
            if "resum" not in str(exc).lower() or attempt == 11:
                raise
            print("vector store is resuming; waiting...")
            time.sleep(10)
    assert job is not None
    job = job["ingestionJob"]
    job_id = job["ingestionJobId"]
    print(f"ingestion job {job_id} started for KB {kb_id}")

    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = job["status"]
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            print(f"status={status} stats={job.get('statistics')}")
            for reason in job.get("failureReasons", []):
                print("failure:", reason)
            return 0 if status == "COMPLETE" else 1
        time.sleep(10)
    print("timed out waiting for ingestion")
    return 1


if __name__ == "__main__":
    sys.exit(main())
