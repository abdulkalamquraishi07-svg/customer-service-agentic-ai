"""Sample orders for demo customers (non-production only).

Templates live in demo_orders.json with dates relative to "now", so a freshly seeded account
always has an overdue delivery, a recent delivery inside the return window, and so on.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

TEMPLATES_FILE = Path(__file__).with_name("demo_orders.json")


def build_orders(customer_id: str, now: datetime) -> list[dict[str, Any]]:
    templates = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"), parse_float=Decimal)
    orders = []
    for t in templates:
        ordered_at = now - timedelta(days=t.pop("ordered_days_ago"))
        promised = ordered_at + timedelta(days=t.pop("promised_delivery_days_from_order"))
        delivered_days_ago = t.pop("delivered_days_ago", None)
        item = {
            "customer_id": customer_id,
            "ordered_at": ordered_at.isoformat(),
            "promised_delivery_at": promised.isoformat(),
            "version": 1,  # optimistic-locking counter
            **t,
        }
        if delivered_days_ago is not None:
            item["delivered_at"] = (now - timedelta(days=delivered_days_ago)).isoformat()
        # DynamoDB rejects None values in some clients; drop them.
        orders.append({k: v for k, v in item.items() if v is not None})
    return orders
