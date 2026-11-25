"""Unit tests for the placeholder Webhook Ingest Lambda handler."""

from __future__ import annotations

import json

from ingest.handler import lambda_handler


def test_placeholder_handler_returns_501_with_delivery_id() -> None:
    response = lambda_handler(
        {"headers": {"X-GitHub-Delivery": "deliv-abc-123"}, "body": "{}"},
        None,
    )
    assert response["statusCode"] == 501
    payload = json.loads(response["body"])
    assert payload["status"] == "not_implemented"
    assert payload["delivery_id"] == "deliv-abc-123"
