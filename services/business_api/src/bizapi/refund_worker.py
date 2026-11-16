"""SQS consumer that executes approved refunds.

Uses partial batch responses: only failed messages are retried. After the queue's
maxReceiveCount they land in the DLQ (alarmed). Retries are safe because refund execution is
idempotent end to end (state machine + gateway key = return_id).
"""

import json
import logging
from typing import Any

from bizapi.common import log
from bizapi.container import get_container
from csagent_core.context import SYSTEM, CallerContext
from csagent_core.errors import (
    ConcurrentModification,
    DependencyFailure,
    InvalidTransition,
    NotFound,
    PolicyViolation,
)

WORKER = "refund-worker"


def _process(record: dict[str, Any]) -> None:
    body = json.loads(record["body"])
    customer_id, return_id = str(body["customer_id"]), str(body["return_id"])
    ctx = CallerContext(WORKER, frozenset({SYSTEM}), request_id=str(record.get("messageId")))
    get_container().returns.execute_refund(ctx, customer_id, return_id)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            _process(record)
        except (NotFound, InvalidTransition) as exc:
            # Stale or duplicate message: nothing to do. Acknowledge.
            log(logging.INFO, "refund_skipped", message_id=message_id, code=exc.code)
        except PolicyViolation as exc:
            # e.g. daily cap. The reconciler re-enqueues later; do not hot-retry.
            log(logging.WARNING, "refund_deferred", message_id=message_id, reason=str(exc))
        except (DependencyFailure, ConcurrentModification) as exc:
            log(logging.WARNING, "refund_retry", message_id=message_id, code=exc.code)
            failures.append({"itemIdentifier": message_id})
        except (KeyError, ValueError, TypeError):
            # Malformed message: fail it so it reaches the DLQ for inspection.
            log(logging.ERROR, "refund_bad_message", message_id=message_id)
            failures.append({"itemIdentifier": message_id})
        except Exception:
            logging.getLogger("csagent").exception("refund_unexpected message_id=%s", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
