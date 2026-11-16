"""Scheduled maintenance (EventBridge, every 15 minutes).

1. Expire approvals past their deadline.
2. Reconcile: re-enqueue APPROVED/FAILED refunds that have sat unexecuted (lost message,
   failed enqueue). Safe because execution is idempotent.
"""

import logging
from datetime import timedelta
from typing import Any

from bizapi.common import log
from bizapi.container import get_container

STUCK_AFTER = timedelta(minutes=10)


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    c = get_container()
    expired = c.returns.expire_pending()

    requeued = 0
    for record in c.returns.list_unexecuted(STUCK_AFTER):
        try:
            c.queue.send(record.customer_id, record.return_id)
            requeued += 1
        except Exception:
            log(logging.WARNING, "requeue_failed", return_id=record.return_id)

    log(logging.INFO, "scheduler_run", expired=expired, requeued=requeued)
    return {"expired": expired, "requeued": requeued}
