"""HTTP client for the business API, using the CALLER's own JWT.

Every tool call goes through API Gateway (WAF, throttling, schema validation, Cognito
authorizer) exactly like a call from the customer app, so the agent runtime never needs direct
database access and can never act as a different customer than the token says.
"""

import threading
import time
from typing import Any

import httpx


class ToolFailure(Exception):
    """A failure safe to show to the model (and, paraphrased, to the customer)."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class CircuitBreaker:
    """Opens after N consecutive failures; half-opens after a cool-down."""

    def __init__(self, threshold: int = 5, cooldown_seconds: float = 30.0) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            # half-open after the cool-down: let a request probe
            return time.monotonic() - self._opened_at >= self._cooldown

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = time.monotonic()


# One breaker per process: protects the business API from retry storms.
_BREAKER = CircuitBreaker()

_SAFE_ERRORS = {
    400: "The request was invalid.",
    401: "The customer session is not authenticated.",
    403: "This action is not permitted.",
    404: "That record was not found for this customer.",
    409: "The request conflicts with the current state.",
    422: "The request is not allowed by company policy.",
}


class BusinessApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        breaker: CircuitBreaker | None = None,
        max_retries: int = 2,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={"Authorization": token, "Content-Type": "application/json"},
            transport=transport,
        )
        self._breaker = breaker or _BREAKER
        self._max_retries = max_retries
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not self._breaker.allow():
            raise ToolFailure(
                "The order system is temporarily unavailable.", retryable=True, status=503
            )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        # GETs and keyed POSTs are safe to retry; unkeyed writes never are.
        retriable = method == "GET" or idempotency_key is not None
        attempts = self._max_retries + 1 if retriable else 1

        last: ToolFailure | None = None
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path.lstrip("/"), json=json, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError):
                last = ToolFailure("The order system did not respond.", retryable=True)
            else:
                if resp.status_code < 400:
                    self._breaker.success()
                    return self._json(resp)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last = ToolFailure(
                        "The order system had a temporary problem.",
                        retryable=True,
                        status=resp.status_code,
                    )
                else:
                    # Client errors are answers, not outages: do not trip the breaker or retry.
                    self._breaker.success()
                    raise self._client_error(resp)
            self._breaker.failure()
            if attempt < attempts - 1:
                self._sleep(0.3 * (2**attempt))
        assert last is not None
        raise last

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise ToolFailure("Unexpected response from the order system.", retryable=True) from exc
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _client_error(resp: httpx.Response) -> ToolFailure:
        detail = ""
        try:
            err = resp.json().get("error", {})
            # API messages are written to be safe/non-sensitive (policy reasons etc.).
            detail = str(err.get("message", ""))[:300]
        except (ValueError, AttributeError):
            pass
        base = _SAFE_ERRORS.get(resp.status_code, "The request failed.")
        return ToolFailure(f"{base} {detail}".strip(), retryable=False, status=resp.status_code)

    # ---- typed helpers ----
    def list_orders(self) -> dict[str, Any]:
        return self.request("GET", "v1/orders")

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"v1/orders/{_seg(order_id)}")

    def get_shipment(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"v1/orders/{_seg(order_id)}/shipment")

    def return_eligibility(self, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"v1/orders/{_seg(order_id)}/return-eligibility")

    def create_return(self, order_id: str, reason: str, key: str) -> dict[str, Any]:
        return self.request(
            "POST", "v1/returns", json={"order_id": order_id, "reason": reason}, idempotency_key=key
        )

    def get_return(self, return_id: str) -> dict[str, Any]:
        return self.request("GET", f"v1/returns/{_seg(return_id)}")

    def create_ticket(self, body: dict[str, Any], key: str) -> dict[str, Any]:
        return self.request("POST", "v1/tickets", json=body, idempotency_key=key)

    def escalate_ticket(self, ticket_id: str, summary: str) -> dict[str, Any]:
        return self.request(
            "POST", f"v1/tickets/{_seg(ticket_id)}/escalate", json={"summary": summary}
        )


def _seg(value: str) -> str:
    """A single URL path segment: model-supplied IDs must not be able to alter the path."""
    value = (value or "").strip()
    if not value or len(value) > 64 or not all(c.isalnum() or c in "-_" for c in value):
        raise ToolFailure("The identifier is not valid.")
    return value
