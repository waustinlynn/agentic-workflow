"""AWS Lambda entrypoint for GitHub webhook ingestion (placeholder until Epic 3)."""

from __future__ import annotations

import json
from typing import Any


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Handle API Gateway HTTP API v2.0 webhook requests.

    Full HMAC signature verification, allowlist enforcement, and repo-scoped
    SQS FIFO routing will be implemented in Epic 3 (#11-#13).
    """
    raw_headers = event.get("headers") or {}
    headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}
    delivery_id = headers.get("x-github-delivery", "")

    return {
        "statusCode": 501,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "status": "not_implemented",
                "message": "Webhook ingest Lambda deployed; full handler lands in Epic 3.",
                "delivery_id": delivery_id,
            }
        ),
    }
