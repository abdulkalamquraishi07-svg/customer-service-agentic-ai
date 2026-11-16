"""Call the DEPLOYED AgentCore runtime as a real client would (Cognito bearer token over HTTPS).

    python scripts/invoke_runtime.py --env dev "I ordered a laptop five days ago..."
Creates a temporary customer with demo orders, checks that unauthenticated and forged-token
calls are refused, sends the prompts in one session, and removes the customer afterwards.
"""

import argparse
import atexit
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime

import boto3

sys.path.insert(0, "scripts")
sys.path.insert(0, "services/core/src")
import smoke_test as s  # noqa: E402
from seed_data import build_orders  # noqa: E402

REGION = "us-east-1"


def runtime_arn(env: str) -> str:
    outputs = boto3.client("cloudformation", region_name=REGION).describe_stacks(
        StackName=f"csagent-{env}-agent"
    )["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "AgentRuntimeArn")


def invoke(arn: str, token: str | None, session_id: str, prompt: str) -> tuple[int, dict]:
    url = (
        f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/"
        f"{urllib.parse.quote(arn, safe='')}/invocations?qualifier=DEFAULT"
    )
    headers = {
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310 - fixed https AWS endpoint
        url, data=json.dumps({"prompt": prompt}).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        return exc.code, {"raw": body}


def forged(token: str) -> str:
    """Same claims, different subject, original signature: must fail signature validation."""
    header, payload, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "someone-else"
    tampered = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{tampered}.{sig}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="dev", choices=["dev", "staging"])
    parser.add_argument("prompts", nargs="+")
    args = parser.parse_args()

    out = s.outputs(args.env)
    pool, client = out["UserPoolId"], out["WebClientId"]
    idp = boto3.client("cognito-idp", region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)
    email = f"runtime.customer.{uuid.uuid4().hex[:8]}@example.com"
    sub, password = s.ensure_user(idp, pool, email, "customers")
    atexit.register(s.cleanup_customer, idp, ddb, args.env, pool, email, sub)
    for item in build_orders(sub, datetime.now(UTC)):
        ddb.Table(f"csagent-{args.env}-orders").put_item(Item=item)
    token = s.login(pool, client, email, password)
    arn = runtime_arn(args.env)
    session_id = f"session-{uuid.uuid4().hex}"  # AgentCore requires >= 33 chars

    ok = True
    status, _ = invoke(arn, None, session_id, "hello")
    print(f"[{'PASS' if status in (401, 403) else 'FAIL'}] no token refused (HTTP {status})")
    ok &= status in (401, 403)
    status, _ = invoke(arn, forged(token), session_id, "hello")
    print(f"[{'PASS' if status in (401, 403) else 'FAIL'}] forged token refused (HTTP {status})")
    ok &= status in (401, 403)

    for prompt in args.prompts:
        started = time.monotonic()
        status, body = invoke(arn, token, session_id, prompt)
        print(f"\nCUSTOMER: {prompt}\nHTTP {status} in {time.monotonic() - started:.1f}s")
        print("ASSISTANT:", body.get("response") or json.dumps(body))
        ok &= status == 200 and "response" in body
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
