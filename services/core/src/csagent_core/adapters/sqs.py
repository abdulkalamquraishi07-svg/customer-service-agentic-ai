"""SQS adapter for the refund-execution queue."""

import json
from typing import Any


class SqsRefundQueue:
    def __init__(self, client: Any, queue_url: str) -> None:
        self._client = client
        self._url = queue_url

    def send(self, customer_id: str, return_id: str) -> None:
        self._client.send_message(
            QueueUrl=self._url,
            MessageBody=json.dumps({"customer_id": customer_id, "return_id": return_id}),
        )
