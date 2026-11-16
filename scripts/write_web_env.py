"""Write web/.env.local from deployed stack outputs, for `npm run dev` against an environment.

    python scripts/write_web_env.py --env dev
The file is git-ignored. Only dev allows the localhost origin (CORS), so local runs target dev.
"""

import argparse
from pathlib import Path

import boto3

REGION = "us-east-1"
ROOT = Path(__file__).resolve().parents[1]


def outputs(cf, stack: str) -> dict[str, str]:
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)[
        "Stacks"][0]["Outputs"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev"])
    args = parser.parse_args()
    cf = boto3.client("cloudformation", region_name=REGION)
    auth = outputs(cf, f"csagent-{args.env}-auth")
    api = outputs(cf, f"csagent-{args.env}-api")
    agent = outputs(cf, f"csagent-{args.env}-agent")
    values = {
        "VITE_ENVIRONMENT": args.env,
        "VITE_REGION": REGION,
        "VITE_USER_POOL_ID": auth["UserPoolId"],
        "VITE_USER_POOL_CLIENT_ID": auth["WebClientId"],
        "VITE_API_URL": api["ApiUrl"],
        "VITE_AGENT_RUNTIME_ARN": agent["AgentRuntimeArn"],
        "VITE_SELF_SIGN_UP": "true" if args.env == "dev" else "false",
    }
    path = ROOT / "web" / ".env.local"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
