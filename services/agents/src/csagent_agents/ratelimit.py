"""Per-customer rate limits and a daily token budget for the agent path.

The browser calls the AgentCore runtime directly, so API Gateway throttling does not protect it:
without this, one signed-in user could script requests and run up the Bedrock bill. Counters live
in a small DynamoDB table (fixed windows, TTL-expired). The three window counters are updated in
ONE transaction, so a rejected request never half-consumes quota.

Failure policy: if the limiter itself is unavailable, requests fail closed (a clear error, no
model call). Cost protection is the point of this component; the business tools need the same
DynamoDB anyway.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from botocore.exceptions import ClientError

from csagent_agents.service_errors import RequestRejected

logger = logging.getLogger("csagent.agent")

# (name, key code, window seconds)
WINDOWS = (("minute", "m", 60), ("hour", "h", 3600), ("day", "d", 86400))
EXPIRY_GRACE_SECONDS = 3600


def _duration(seconds: int) -> str:
    if seconds < 120:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    minutes = seconds // 60
    return f"{minutes} minutes" if minutes < 120 else f"{minutes // 60} hours"


class RateLimited(RequestRejected):
    """Customer exceeded a limit. The message is safe to show them."""

    def __init__(self, scope: str, retry_after: int) -> None:
        self.scope = scope
        self.retry_after = max(1, retry_after)
        if scope == "tokens":
            message = (
                "You've reached today's usage limit for the assistant. Please try again "
                "tomorrow, or contact us to speak with a human representative."
            )
        else:
            wait = _duration(self.retry_after)
            message = f"You're sending messages too quickly. Please wait {wait} and try again."
        super().__init__(message)


class LimiterUnavailable(Exception):
    """The counter store failed; callers fail closed."""


@dataclass(frozen=True)
class Limits:
    per_minute: int = 6
    per_hour: int = 60
    per_day: int = 250
    daily_tokens: int = 600_000

    def for_window(self, name: str) -> int:
        return {"minute": self.per_minute, "hour": self.per_hour, "day": self.per_day}[name]


class Limiter(Protocol):
    def acquire(self, customer_id: str) -> None: ...
    def record_tokens(self, customer_id: str, tokens: int) -> None: ...


class NoLimiter:
    """Local development only (no table configured)."""

    def acquire(self, customer_id: str) -> None:
        return None

    def record_tokens(self, customer_id: str, tokens: int) -> None:
        return None


class DynamoRateLimiter:
    def __init__(
        self,
        client: Any,
        table_name: str,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._table = table_name
        self._limits = limits or Limits()
        self._clock = clock

    @staticmethod
    def _key(customer_id: str, code: str, window: int) -> dict[str, Any]:
        return {"bucket": {"S": f"{customer_id}#{code}#{window}"}}

    def _check_token_budget(self, customer_id: str, now: int) -> None:
        day = now // 86400
        try:
            item = self._client.get_item(
                TableName=self._table, Key=self._key(customer_id, "tok", day)
            ).get("Item")
        except ClientError as exc:
            raise LimiterUnavailable(exc.response["Error"]["Code"]) from exc
        used = int(item["tokens"]["N"]) if item and "tokens" in item else 0
        if used >= self._limits.daily_tokens:
            raise RateLimited("tokens", (day + 1) * 86400 - now)

    def acquire(self, customer_id: str) -> None:
        now = int(self._clock())
        self._check_token_budget(customer_id, now)  # before counting a turn it would reject
        items = []
        for name, code, seconds in WINDOWS:
            window = now // seconds
            items.append(
                {
                    "Update": {
                        "TableName": self._table,
                        "Key": self._key(customer_id, code, window),
                        "UpdateExpression": "SET expires_at = :exp ADD #c :one",
                        "ConditionExpression": "attribute_not_exists(#c) OR #c < :limit",
                        "ExpressionAttributeNames": {"#c": "count"},
                        "ExpressionAttributeValues": {
                            ":one": {"N": "1"},
                            ":limit": {"N": str(self._limits.for_window(name))},
                            ":exp": {"N": str((window + 1) * seconds + EXPIRY_GRACE_SECONDS)},
                        },
                    }
                }
            )
        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "TransactionCanceledException":
                reasons = exc.response.get("CancellationReasons", [])
                for (name, _, seconds), reason in zip(WINDOWS, reasons, strict=False):
                    if reason.get("Code") == "ConditionalCheckFailed":
                        raise RateLimited(name, seconds - now % seconds) from exc
            raise LimiterUnavailable(code) from exc

    def record_tokens(self, customer_id: str, tokens: int) -> None:
        """Best effort accounting after a turn; never fails the customer's request."""
        if tokens <= 0:
            return
        day = int(self._clock()) // 86400
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(customer_id, "tok", day),
                UpdateExpression="SET expires_at = :exp ADD tokens :n",
                ExpressionAttributeValues={
                    ":n": {"N": str(int(tokens))},
                    ":exp": {"N": str((day + 1) * 86400 + EXPIRY_GRACE_SECONDS)},
                },
            )
        except ClientError:
            logger.warning("token accounting failed (budget slightly under-counted)")
