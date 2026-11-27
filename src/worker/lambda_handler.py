"""Phase 2 AWS Lambda SQS FIFO worker entrypoint with soft-timeout guard and headless auth."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import boto3

from shared.github_auth import (
    AppIdentity,
    CredentialProvider,
    EnvCredentialProvider,
    SecretsManagerClientProtocol,
    SecretsManagerCredentialProvider,
)
from shared.github_client import create_github_clients
from shared.models import TaskEnvelope
from worker.agent_backend import AgentBackend, create_agent_backend
from worker.handlers import TaskOutcome, WorkerContext, handle_task
from worker.local_runner import configure_logging
from worker.prompts import PromptAssembler
from worker.state_store import DynamoDBRunStateStore, RunStateStore
from worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

DEFAULT_LAMBDA_TIMEOUT_SECONDS = 900.0
DEFAULT_SOFT_TIMEOUT_BUFFER_SECONDS = 120.0
MIN_SOFT_BUDGET_SECONDS = 10.0

_TMP_CACHE_ENV_VARS: tuple[str, ...] = (
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "AGENTIC_WORKSPACE_DIR",
    "PIP_CACHE_DIR",
    "UV_CACHE_DIR",
    "NPM_CONFIG_CACHE",
    "DOTNET_CLI_HOME",
    "NUGET_PACKAGES",
    "CARGO_HOME",
    "GOPATH",
    "GOCACHE",
    "GOMODCACHE",
    "GOLANGCI_LINT_CACHE",
    "TF_DATA_DIR",
    "TF_PLUGIN_CACHE_DIR",
)


def ensure_tmp_cache_dirs(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Ensure writable ``/tmp`` cache directories exist before running toolchains in Lambda."""
    env = os.environ if environ is None else environ
    created: list[Path] = []
    for var_name in _TMP_CACHE_ENV_VARS:
        raw_path = env.get(var_name, "").strip()
        if not raw_path:
            continue
        path = Path(raw_path)
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
    return created


class LambdaContextProtocol(Protocol):
    """Minimal subset of AWS Lambda's runtime ``context`` object."""

    def get_remaining_time_in_millis(self) -> int: ...


@dataclass(frozen=True, slots=True)
class LambdaWorkerConfig:
    """Runtime settings for Phase 2 AWS Lambda worker invocations."""

    soft_timeout_buffer_seconds: float = DEFAULT_SOFT_TIMEOUT_BUFFER_SECONDS
    default_timeout_seconds: float = DEFAULT_LAMBDA_TIMEOUT_SECONDS
    runtime_name: str = "base"
    cleanup_workspace_root_after_batch: bool = True


class SecretsManagerApiKeyLoader:
    """Loads and caches a headless Antigravity/Gemini API key from AWS Secrets Manager.

    Supports either a raw secret string or a JSON object containing ``api_key``,
    ``antigravity_api_key``, or ``gemini_api_key``.
    """

    def __init__(
        self,
        secret_arn: str,
        *,
        client: SecretsManagerClientProtocol | None = None,
        region_name: str | None = None,
    ) -> None:
        self._secret_arn = secret_arn
        self._client = client
        self._region_name = region_name
        self._cached: str | None = None

    def _get_client(self) -> SecretsManagerClientProtocol:
        if self._client is None:
            self._client = boto3.client("secretsmanager", region_name=self._region_name)
        return self._client

    def load(self) -> str:
        """Return the cached API key, fetching from Secrets Manager on first call."""
        if self._cached is not None:
            return self._cached

        response = self._get_client().get_secret_value(SecretId=self._secret_arn)
        raw = response.get("SecretString")
        if not isinstance(raw, str) or not raw.strip():
            raise RuntimeError(
                f"Antigravity API key secret {self._secret_arn!r} is empty or missing SecretString"
            )

        cleaned = raw.strip()
        if cleaned.startswith("{"):
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Antigravity API key secret {self._secret_arn!r} looks like invalid JSON"
                ) from exc
            if isinstance(parsed, dict):
                for key in ("api_key", "antigravity_api_key", "gemini_api_key"):
                    value = parsed.get(key)
                    if isinstance(value, str) and value.strip():
                        self._cached = value.strip()
                        return self._cached
                raise RuntimeError(
                    f"Antigravity API key JSON secret {self._secret_arn!r} must contain "
                    "'api_key', 'antigravity_api_key', or 'gemini_api_key'"
                )

        self._cached = cleaned
        return self._cached


def compute_deadline_monotonic(
    context: LambdaContextProtocol | None,
    *,
    config: LambdaWorkerConfig,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """Compute the soft execution deadline (monotonic timestamp) before Lambda hard-kills."""
    remaining_seconds = config.default_timeout_seconds
    if context is not None:
        get_remaining = getattr(context, "get_remaining_time_in_millis", None)
        if callable(get_remaining):
            remaining_ms = float(get_remaining())
            remaining_seconds = max(0.0, remaining_ms / 1000.0)

    budget_seconds = max(
        MIN_SOFT_BUDGET_SECONDS,
        remaining_seconds - config.soft_timeout_buffer_seconds,
    )
    return clock() + budget_seconds


class LambdaWorkerService:
    """Dependency-injected SQS FIFO batch processor for Phase 2 AWS Lambda container workers."""

    def __init__(
        self,
        *,
        context_factory: Callable[[float], WorkerContext],
        workspace_manager: WorkspaceManager | None = None,
        config: LambdaWorkerConfig | None = None,
        task_executor: Callable[[TaskEnvelope, WorkerContext], TaskOutcome] = handle_task,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._context_factory = context_factory
        self._workspace_manager = workspace_manager
        self._config = config or LambdaWorkerConfig()
        self._task_executor = task_executor
        self._clock = clock

    def handle_sqs_event(
        self,
        event: Mapping[str, Any],
        context: LambdaContextProtocol | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        """Process an SQS FIFO Lambda event using ``ReportBatchItemFailures`` semantics."""
        records = event.get("Records")
        if not isinstance(records, list):
            return {"batchItemFailures": []}

        failures: list[dict[str, str]] = []
        try:
            for record in records:
                message_id = str(record.get("messageId") or "")
                try:
                    raw_body = record.get("body")
                    if not isinstance(raw_body, str) or not raw_body.strip():
                        raise ValueError("SQS record is missing a non-empty string body")

                    envelope = TaskEnvelope.model_validate_json(raw_body)
                    deadline = compute_deadline_monotonic(
                        context,
                        config=self._config,
                        clock=self._clock,
                    )
                    worker_ctx = self._context_factory(deadline)

                    logger.info(
                        "Lambda worker[%s] starting task=%s repo=%s item=#%d message_id=%s",
                        self._config.runtime_name,
                        envelope.task_type.value,
                        envelope.repo_full_name,
                        envelope.issue_or_pr_number,
                        message_id,
                    )
                    outcome = self._task_executor(envelope, worker_ctx)
                    logger.info(
                        "Lambda worker[%s] finished task=%s repo=%s item=#%d status=%s summary=%s",
                        self._config.runtime_name,
                        outcome.task_type.value,
                        outcome.repo_full_name,
                        outcome.issue_or_pr_number,
                        outcome.status,
                        outcome.summary[:200],
                    )
                except Exception:
                    logger.exception(
                        "Lambda worker[%s] failed processing SQS message_id=%s; "
                        "reporting batch item failure",
                        self._config.runtime_name,
                        message_id,
                    )
                    if message_id:
                        failures.append({"itemIdentifier": message_id})
        finally:
            if (
                self._config.cleanup_workspace_root_after_batch
                and self._workspace_manager is not None
            ):
                removed = self._workspace_manager.cleanup_all()
                if removed:
                    logger.info("Swept %d leftover workspace entries under /tmp", removed)

        return {"batchItemFailures": failures}


def _build_credential_provider(environ: Mapping[str, str], region: str) -> CredentialProvider:
    coder_arn = environ.get("GITHUB_CODER_SECRET_ARN", "").strip()
    reviewer_arn = environ.get("GITHUB_REVIEWER_SECRET_ARN", "").strip()
    if coder_arn and reviewer_arn:
        return SecretsManagerCredentialProvider(
            {
                AppIdentity.CODER: coder_arn,
                AppIdentity.REVIEWER: reviewer_arn,
            },
            region_name=region,
        )
    return EnvCredentialProvider(environ)


def _build_headless_agent_backend(
    environ: Mapping[str, str],
    *,
    region: str,
    secrets_client: SecretsManagerClientProtocol | None = None,
) -> AgentBackend:
    effective_env = dict(environ)
    api_key_secret_arn = environ.get("ANTIGRAVITY_API_KEY_SECRET_ARN", "").strip()
    has_direct_key = bool(
        effective_env.get("ANTIGRAVITY_API_KEY", "").strip()
        or effective_env.get("GEMINI_API_KEY", "").strip()
    )
    if api_key_secret_arn and not has_direct_key:
        loader = SecretsManagerApiKeyLoader(
            api_key_secret_arn,
            client=secrets_client,
            region_name=region,
        )
        effective_env["ANTIGRAVITY_API_KEY"] = loader.load()

    return create_agent_backend(effective_env)


_DEFAULT_SERVICE: LambdaWorkerService | None = None


def _get_default_service() -> LambdaWorkerService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is not None:
        return _DEFAULT_SERVICE

    env = os.environ
    configure_logging(
        log_format=env.get("LOG_FORMAT", "json"),
        level=env.get("LOG_LEVEL", "INFO"),
    )
    if env.get("WORKER_RUNTIME", "").strip().lower() in {"base", "infra"}:
        env.setdefault("TF_PLUGIN_CACHE_DIR", "/tmp/.terraform.d/plugin-cache")
    ensure_tmp_cache_dirs(env)

    region = env.get("AWS_REGION", "us-east-1")
    run_state_table = env["RUN_STATE_TABLE"]
    workspace_dir = Path(env.get("AGENTIC_WORKSPACE_DIR", "/tmp/agentic-workspaces"))

    secrets_client = boto3.client("secretsmanager", region_name=region)
    cred_provider = _build_credential_provider(env, region)
    github_clients = create_github_clients(cred_provider)
    agent_backend = _build_headless_agent_backend(
        env,
        region=region,
        secrets_client=secrets_client,
    )
    prompt_assembler = PromptAssembler()
    workspace_manager = WorkspaceManager(base_dir=workspace_dir)
    state_store: RunStateStore = DynamoDBRunStateStore(run_state_table, region_name=region)

    config = LambdaWorkerConfig(
        soft_timeout_buffer_seconds=float(
            env.get("LAMBDA_SOFT_TIMEOUT_BUFFER_SECONDS", str(DEFAULT_SOFT_TIMEOUT_BUFFER_SECONDS))
        ),
        default_timeout_seconds=float(
            env.get("LAMBDA_TIMEOUT_SECONDS", str(DEFAULT_LAMBDA_TIMEOUT_SECONDS))
        ),
        runtime_name=env.get("WORKER_RUNTIME", "base").strip() or "base",
    )

    def _make_context(deadline_monotonic: float) -> WorkerContext:
        return WorkerContext(
            github_clients=github_clients,
            agent_backend=agent_backend,
            prompt_assembler=prompt_assembler,
            workspace_manager=workspace_manager,
            state_store=state_store,
            deadline_monotonic=deadline_monotonic,
        )

    _DEFAULT_SERVICE = LambdaWorkerService(
        context_factory=_make_context,
        workspace_manager=workspace_manager,
        config=config,
    )
    return _DEFAULT_SERVICE


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, list[dict[str, str]]]:
    """AWS Lambda SQS FIFO entrypoint shared across ``worker-base`` and stack images."""
    return _get_default_service().handle_sqs_event(event, context)
