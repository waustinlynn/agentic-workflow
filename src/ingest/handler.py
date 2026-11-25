"""AWS Lambda entrypoint for GitHub webhook verification, routing, and SQS FIFO enqueue."""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import boto3

from ingest.registry import DynamoDBRepoRegistry, RepoRegistry
from ingest.router import route_webhook_event
from ingest.security import verify_github_signature
from shared.github_auth import AppIdentity, GitHubAppAuth, SecretsManagerCredentialProvider
from shared.github_client import BootstrapLabelsResult, GitHubClient

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SQSSenderProtocol(Protocol):
    """Minimal subset of ``boto3.client('sqs')`` used to enqueue FIFO tasks."""

    def send_message(
        self,
        *,
        QueueUrl: str,
        MessageBody: str,
        MessageGroupId: str,
        MessageDeduplicationId: str,
    ) -> Mapping[str, Any]: ...


def _json_response(status_code: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(dict(body)),
    }


def _decode_body(event: Mapping[str, Any]) -> bytes:
    raw = event.get("body")
    if raw is None:
        return b""
    if not isinstance(raw, str):
        raise ValueError("event body must be a string")
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return raw.encode("utf-8")


class IngestService:
    """Dependency-injected webhook processor shared by Lambda and offline unit tests."""

    def __init__(
        self,
        *,
        webhook_secret_provider: Callable[[], str],
        registry: RepoRegistry,
        sqs_client: SQSSenderProtocol,
        sqs_queue_url: str,
        allowed_actors: frozenset[str],
        label_bootstrapper: Callable[[str], BootstrapLabelsResult] | None = None,
    ) -> None:
        self._webhook_secret_provider = webhook_secret_provider
        self._registry = registry
        self._sqs_client = sqs_client
        self._sqs_queue_url = sqs_queue_url
        self._allowed_actors = allowed_actors
        self._label_bootstrapper = label_bootstrapper

    def handle_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Validate HMAC signature, enforce public-repo guardrails, and dispatch to SQS FIFO."""
        raw_headers = event.get("headers") or {}
        headers = {str(k).lower(): str(v) for k, v in raw_headers.items()}

        try:
            raw_body = _decode_body(event)
        except ValueError as exc:
            logger.warning("Failed to decode webhook body: %s", exc)
            return _json_response(400, {"status": "bad_request", "error": "invalid body encoding"})

        signature = headers.get("x-hub-signature-256")
        secret = self._webhook_secret_provider()
        if not verify_github_signature(raw_body, signature, secret):
            logger.warning(
                "Rejected webhook with invalid signature (delivery_id=%s)",
                headers.get("x-github-delivery", ""),
            )
            return _json_response(401, {"status": "unauthorized", "error": "invalid signature"})

        event_name = headers.get("x-github-event", "").strip()
        delivery_id = headers.get("x-github-delivery", "").strip()
        if not event_name or not delivery_id:
            return _json_response(
                400,
                {
                    "status": "bad_request",
                    "error": "missing X-GitHub-Event or X-GitHub-Delivery header",
                },
            )

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_response(400, {"status": "bad_request", "error": "invalid JSON payload"})

        if not isinstance(payload, dict):
            return _json_response(
                400,
                {"status": "bad_request", "error": "webhook payload must be a JSON object"},
            )

        decision = route_webhook_event(
            event_name,
            delivery_id,
            payload,
            allowed_actors=self._allowed_actors,
            registry=self._registry,
        )

        if decision.action == "ping":
            logger.info("Handled GitHub ping delivery_id=%s", delivery_id)
            return _json_response(
                200,
                {"status": "ok", "event": "ping", "delivery_id": delivery_id},
            )

        if decision.action == "ignore":
            logger.info(
                "Ignored webhook event=%s delivery_id=%s reason=%s",
                event_name,
                delivery_id,
                decision.reason,
            )
            return _json_response(
                202,
                {
                    "status": "ignored",
                    "reason": decision.reason,
                    "delivery_id": delivery_id,
                },
            )

        if decision.action == "bootstrap_labels":
            if self._label_bootstrapper is None:
                logger.warning(
                    "Skipping label bootstrap for %s: no label_bootstrapper configured",
                    decision.bootstrap_repos,
                )
                return _json_response(
                    202,
                    {
                        "status": "ignored",
                        "reason": "label bootstrapper not configured",
                        "delivery_id": delivery_id,
                    },
                )

            results: dict[str, dict[str, list[str]]] = {}
            for repo in decision.bootstrap_repos:
                res = self._label_bootstrapper(repo)
                results[repo] = {
                    "created": res.created,
                    "updated": res.updated,
                    "unchanged": res.unchanged,
                }
                logger.info(
                    "Bootstrapped labels for repo=%s created=%d updated=%d unchanged=%d",
                    repo,
                    len(res.created),
                    len(res.updated),
                    len(res.unchanged),
                )
            return _json_response(
                200,
                {
                    "status": "bootstrapped",
                    "delivery_id": delivery_id,
                    "repositories": results,
                },
            )

        assert decision.task is not None and decision.registry_entry is not None
        sqs_resp = self._sqs_client.send_message(
            QueueUrl=self._sqs_queue_url,
            MessageBody=decision.task.model_dump_json(),
            MessageGroupId=decision.registry_entry.message_group_id,
            MessageDeduplicationId=delivery_id,
        )
        message_id = str(sqs_resp.get("MessageId", ""))
        logger.info(
            "Enqueued task=%s repo=%s item=%d group=%s delivery_id=%s sqs_message_id=%s",
            decision.task.task_type.value,
            decision.task.repo_full_name,
            decision.task.issue_or_pr_number,
            decision.registry_entry.message_group_id,
            delivery_id,
            message_id,
        )
        return _json_response(
            202,
            {
                "status": "enqueued",
                "task_type": decision.task.task_type.value,
                "repo_full_name": decision.task.repo_full_name,
                "message_group_id": decision.registry_entry.message_group_id,
                "deduplication_id": delivery_id,
                "message_id": message_id,
            },
        )


class _CachedWebhookSecret:
    def __init__(self, secrets_client: Any, secret_arn: str) -> None:
        self._client = secrets_client
        self._secret_arn = secret_arn
        self._cached: str | None = None

    def __call__(self) -> str:
        if self._cached is None:
            resp = self._client.get_secret_value(SecretId=self._secret_arn)
            secret_str = str(resp.get("SecretString", "")).strip()
            if not secret_str:
                raise RuntimeError(f"Webhook secret {self._secret_arn!r} is empty")
            self._cached = secret_str
        return self._cached


_DEFAULT_SERVICE: IngestService | None = None


def _get_default_service() -> IngestService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is not None:
        return _DEFAULT_SERVICE

    queue_url = os.environ["SQS_QUEUE_URL"]
    registry_table = os.environ["REPO_REGISTRY_TABLE"]
    webhook_secret_arn = os.environ["WEBHOOK_SECRET_ARN"]
    coder_app_secret_arn = os.environ.get("CODER_APP_SECRET_ARN", "").strip()
    allowed_raw = os.environ.get("ALLOWED_ACTORS", "")
    allowed_actors = frozenset(a.strip() for a in allowed_raw.split(",") if a.strip())

    secrets_client = boto3.client("secretsmanager")
    dynamodb_client = boto3.client("dynamodb")
    sqs_client = boto3.client("sqs")

    label_bootstrapper: Callable[[str], BootstrapLabelsResult] | None = None
    if coder_app_secret_arn:
        cred_provider = SecretsManagerCredentialProvider(
            {AppIdentity.CODER: coder_app_secret_arn},
            client=secrets_client,
        )
        github_client = GitHubClient(GitHubAppAuth(AppIdentity.CODER, cred_provider))
        label_bootstrapper = github_client.bootstrap_labels

    _DEFAULT_SERVICE = IngestService(
        webhook_secret_provider=_CachedWebhookSecret(secrets_client, webhook_secret_arn),
        registry=DynamoDBRepoRegistry(dynamodb_client, registry_table),
        sqs_client=sqs_client,
        sqs_queue_url=queue_url,
        allowed_actors=allowed_actors,
        label_bootstrapper=label_bootstrapper,
    )
    return _DEFAULT_SERVICE


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AWS Lambda entrypoint invoked by API Gateway HTTP API v2.0."""
    return _get_default_service().handle_event(event)
