"""Phase-1 edge handlers: unauthenticated health check and authenticated whoami.

`whoami` demonstrates the identity-propagation rule the whole platform relies on:
customer identity is taken ONLY from the verified JWT claims injected by the API Gateway
Cognito authorizer, never from the request body or model output.
"""

import json
import os
from typing import Any

ENV_NAME = os.environ.get("APP_ENV", "unknown")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        },
        "body": json.dumps(body),
    }


def health(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return _response(200, {"status": "ok", "env": ENV_NAME})


def whoami(event: dict[str, Any], context: Any) -> dict[str, Any]:
    claims = (event.get("requestContext") or {}).get("authorizer", {}).get("claims") or {}
    customer_id = claims.get("sub")
    if not customer_id:
        return _response(401, {"error": "unauthenticated"})
    groups_raw = claims.get("cognito:groups", "")
    groups = [g for g in str(groups_raw).strip("[]").replace(",", " ").split() if g]
    return _response(200, {"customer_id": customer_id, "groups": groups})
