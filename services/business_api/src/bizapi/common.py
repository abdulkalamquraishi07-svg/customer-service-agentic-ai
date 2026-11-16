"""Shared HTTP plumbing for API Gateway (REST, Cognito authorizer) Lambda handlers."""

import dataclasses
import json
import logging
import os
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from csagent_core.context import CallerContext
from csagent_core.errors import (
    Conflict,
    DependencyFailure,
    DomainError,
    Forbidden,
    NotFound,
    PolicyViolation,
    ValidationFailed,
)
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

logger = logging.getLogger("csagent")
logger.setLevel(logging.INFO)

MAX_BODY_BYTES = 16 * 1024

STATUS_BY_ERROR: list[tuple[type[DomainError], int]] = [
    (NotFound, 404),
    (Forbidden, 403),
    (ValidationFailed, 400),
    (PolicyViolation, 422),
    (Conflict, 409),  # includes concurrent modification, idempotency, invalid transition
    (DependencyFailure, 503),
]

Handler = Callable[[CallerContext, dict[str, Any]], tuple[int, Any]]


def log(level: int, message: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"message": message, **fields}, default=str))


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)  # money as string: no float rounding
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    raise TypeError(f"not serializable: {type(value)}")


def allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS") or os.environ.get("ALLOWED_ORIGIN") or ""
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def cors_origin(event: dict[str, Any] | None) -> str | None:
    """Echo the request Origin only if it is on the allowlist (no wildcard)."""
    origin = header(event or {}, "Origin")
    if origin and origin.rstrip("/") in allowed_origins():
        return origin.rstrip("/")
    return None


def response(status: int, body: Any, origin: str | None = None) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "Vary": "Origin",
    }
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, default=_json_default),
    }


def error_response(
    status: int, code: str, message: str, request_id: str, origin: str | None = None
) -> dict[str, Any]:
    body = {"error": {"code": code, "message": message}, "request_id": request_id}
    return response(status, body, origin)


def parse_groups(raw: Any) -> frozenset[str]:
    """Cognito renders groups as 'a', 'a,b' or '[a b]' depending on the token/authorizer."""
    cleaned = str(raw or "").strip("[]").replace(",", " ")
    return frozenset(g for g in cleaned.split() if g)


def context_from_event(event: dict[str, Any]) -> CallerContext:
    """Identity comes ONLY from claims verified by the API Gateway Cognito authorizer."""
    rc = event.get("requestContext") or {}
    claims = (rc.get("authorizer") or {}).get("claims") or {}
    subject = claims.get("sub")
    if not subject:
        raise Forbidden("unauthenticated")
    return CallerContext(
        subject=str(subject),
        groups=parse_groups(claims.get("cognito:groups")),
        request_id=str(rc.get("requestId") or "unknown"),
    )


def parse_body(event: dict[str, Any], model: type[BaseModel]) -> Any:
    raw = event.get("body")
    if raw is None or raw == "":
        raise ValidationFailed("request body required")
    if len(raw.encode()) > MAX_BODY_BYTES:
        raise ValidationFailed("request body too large")
    try:
        return model.model_validate_json(raw)
    except PydanticValidationError as exc:
        fields = ", ".join(".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors())
        raise ValidationFailed(f"invalid request: {fields}") from exc


def header(event: dict[str, Any], name: str) -> str | None:
    wanted = name.lower()
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == wanted:
            return str(value)
    return None


def path_param(event: dict[str, Any], name: str) -> str:
    """A required, sane path parameter. Malformed paths are client errors, never 500s."""
    value = (event.get("pathParameters") or {}).get(name)
    if not value or len(value) > 128:
        raise ValidationFailed(f"invalid path parameter: {name}")
    return str(value)


def require_idempotency_key(event: dict[str, Any]) -> str:
    key = header(event, "Idempotency-Key")
    if not key:
        raise ValidationFailed("Idempotency-Key header required")
    return key


def dispatch(
    event: dict[str, Any], routes: Mapping[tuple[str, str], Handler]
) -> dict[str, Any]:
    """Route, authenticate, run, and map domain errors to HTTP. Never leaks internals."""
    request_id = str((event.get("requestContext") or {}).get("requestId") or "unknown")
    origin = cors_origin(event)
    handler = routes.get((event.get("httpMethod", ""), event.get("resource", "")))
    if handler is None:
        return error_response(404, "not_found", "route not found", request_id, origin)
    try:
        ctx = context_from_event(event)
        status, body = handler(ctx, event)
        log(logging.INFO, "request", request_id=request_id, route=event.get("resource"),
            status=status)
        return response(status, body, origin)
    except DomainError as exc:
        status = next((s for cls, s in STATUS_BY_ERROR if isinstance(exc, cls)), 400)
        log(logging.INFO, "domain_error", request_id=request_id, code=exc.code, status=status)
        return error_response(status, exc.code, str(exc), request_id, origin)
    except Exception:
        logger.exception("unhandled error request_id=%s", request_id)
        return error_response(500, "internal_error", "internal error", request_id, origin)
