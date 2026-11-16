"""SIMULATED payment gateway backed by a DynamoDB ledger.

There is no real payment provider in this project. This adapter honors the same contract a real
PSP integration must (idempotent on the key, returns a reference) so the rest of the system is
exercised realistically. Replace with a real provider adapter for production money movement.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from csagent_core.errors import DependencyFailure


class SimulatedPaymentGateway:
    def __init__(self, table: Any) -> None:
        self._t = table

    def refund(
        self, idempotency_key: str, customer_id: str, amount: Decimal, currency: str
    ) -> str:
        reference = f"SIM-{idempotency_key}"
        try:
            self._t.put_item(
                Item={
                    "idempotency_key": idempotency_key,
                    "reference": reference,
                    "customer_id": customer_id,
                    "amount": amount,
                    "currency": currency,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                return reference  # already paid: idempotent replay
            raise DependencyFailure("payment ledger unavailable") from exc
        return reference
