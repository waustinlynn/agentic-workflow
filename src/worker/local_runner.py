"""Phase 1 local SQS FIFO long-poller daemon with background visibility-timeout heartbeat."""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

import boto3

from shared.github_auth import (
    AppIdentity,
    CredentialProvider,
    EnvCredentialProvider,
    SecretsManagerCredentialProvider,
)
from shared.github_client import create_github_clients
from shared.models import TaskEnvelope
from worker.agent_backend import create_agent_backend
from worker.handlers import TaskOutcome, WorkerContext, handle_task
from worker.prompts import PromptAssembler
from worker.state_store import DynamoDBRunStateStore
from worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class SQSPollerProtocol(Protocol):
    """Minimal subset of ``boto3.client('sqs')`` required by ``LocalRunnerDaemon``."""

    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int,
        WaitTimeSeconds: int,
        AttributeNames: list[str],
    ) -> Mapping[str, Any]: ...

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> Mapping[str, Any]: ...

    def delete_message(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
    ) -> Mapping[str, Any]: ...


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(log_format: str = "console", level: str = "INFO") -> None:
    """Configure structured JSON or readable console logging for the daemon."""
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler()
    if log_format.strip().lower() == "json":
        handler.setFormatter(_JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


class VisibilityHeartbeat:
    """Background daemon thread extending SQS message visibility during long agent runs."""

    def __init__(
        self,
        sqs_client: SQSPollerProtocol,
        *,
        queue_url: str,
        receipt_handle: str,
        visibility_timeout_seconds: int = 120,
        interval_seconds: float = 45.0,
    ) -> None:
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._receipt_handle = receipt_handle
        self._visibility_timeout = visibility_timeout_seconds
        self._interval = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="sqs-visibility-heartbeat",
            daemon=True,
        )
        self.extensions_sent = 0

    def __enter__(self) -> VisibilityHeartbeat:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self._interval):
            try:
                self._sqs.change_message_visibility(
                    QueueUrl=self._queue_url,
                    ReceiptHandle=self._receipt_handle,
                    VisibilityTimeout=self._visibility_timeout,
                )
                self.extensions_sent += 1
            except Exception:
                logger.warning("Failed to extend SQS visibility timeout", exc_info=True)


@dataclass(frozen=True, slots=True)
class LocalRunnerConfig:
    """Runtime settings for ``LocalRunnerDaemon``."""

    sqs_queue_url: str
    wait_time_seconds: int = 20
    visibility_timeout_seconds: int = 120
    heartbeat_interval_seconds: float = 45.0
    max_concurrency: int = 4


class LocalRunnerDaemon:
    """Long-polls SQS FIFO: sequential per repo group, concurrent across distinct repos."""

    def __init__(
        self,
        *,
        sqs_client: SQSPollerProtocol,
        config: LocalRunnerConfig,
        task_executor: Callable[[TaskEnvelope], TaskOutcome],
    ) -> None:
        self._sqs = sqs_client
        self._config = config
        self._task_executor = task_executor
        self._stop_event = threading.Event()
        self._group_locks_guard = threading.Lock()
        self._group_locks: dict[str, threading.Lock] = {}
        self._in_flight: set[Future[None]] = set()
        self._in_flight_guard = threading.Lock()

    def request_shutdown(self) -> None:
        """Signal the polling loop to stop fetching new messages and drain in-flight tasks."""
        logger.info("Shutdown requested; draining in-flight tasks...")
        self._stop_event.set()

    def _get_group_lock(self, message_group_id: str) -> threading.Lock:
        with self._group_locks_guard:
            lock = self._group_locks.get(message_group_id)
            if lock is None:
                lock = threading.Lock()
                self._group_locks[message_group_id] = lock
            return lock

    def _available_slots(self) -> int:
        with self._in_flight_guard:
            done = {f for f in self._in_flight if f.done()}
            self._in_flight.difference_update(done)
            return max(0, self._config.max_concurrency - len(self._in_flight))

    def _process_message(self, message: Mapping[str, Any]) -> None:
        receipt_handle = str(message["ReceiptHandle"])
        message_id = str(message.get("MessageId") or "")
        attrs = message.get("Attributes") or {}
        group_id = str(attrs.get("MessageGroupId") or "default")

        group_lock = self._get_group_lock(group_id)
        with (
            group_lock,
            VisibilityHeartbeat(
                self._sqs,
                queue_url=self._config.sqs_queue_url,
                receipt_handle=receipt_handle,
                visibility_timeout_seconds=self._config.visibility_timeout_seconds,
                interval_seconds=self._config.heartbeat_interval_seconds,
            ),
        ):
            envelope = TaskEnvelope.model_validate_json(str(message["Body"]))
            logger.info(
                "Starting task=%s repo=%s item=#%d group=%s sqs_message_id=%s",
                envelope.task_type.value,
                envelope.repo_full_name,
                envelope.issue_or_pr_number,
                group_id,
                message_id,
            )
            outcome = self._task_executor(envelope)
            logger.info(
                "Finished task=%s repo=%s item=#%d status=%s summary=%s",
                outcome.task_type.value,
                outcome.repo_full_name,
                outcome.issue_or_pr_number,
                outcome.status,
                outcome.summary[:200],
            )
            self._sqs.delete_message(
                QueueUrl=self._config.sqs_queue_url,
                ReceiptHandle=receipt_handle,
            )

    def poll_once(self, executor: ThreadPoolExecutor) -> int:
        """Fetch up to available worker slots from SQS FIFO and submit them to ``executor``."""
        slots = self._available_slots()
        if slots <= 0:
            self._stop_event.wait(timeout=0.5)
            return 0

        response = self._sqs.receive_message(
            QueueUrl=self._config.sqs_queue_url,
            MaxNumberOfMessages=min(slots, 10),
            WaitTimeSeconds=self._config.wait_time_seconds,
            AttributeNames=["MessageGroupId"],
        )
        messages = response.get("Messages") or []
        for msg in messages:
            future = executor.submit(self._process_message, msg)
            with self._in_flight_guard:
                self._in_flight.add(future)
        return len(messages)

    def run_forever(self, *, max_polls: int | None = None) -> None:
        """Run the SQS long-polling loop until ``request_shutdown()`` or ``max_polls``."""
        polls = 0
        with ThreadPoolExecutor(
            max_workers=self._config.max_concurrency,
            thread_name_prefix="agentic-worker",
        ) as executor:
            while not self._stop_event.is_set():
                if max_polls is not None and polls >= max_polls:
                    break
                polls += 1
                try:
                    self.poll_once(executor)
                except Exception:
                    logger.exception("Error polling SQS queue; backing off 2s")
                    self._stop_event.wait(timeout=2.0)

            # Drain all in-flight tasks before exiting cleanly.
            with self._in_flight_guard:
                pending = list(self._in_flight)
            for future in pending:
                future.result()


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


def main() -> None:
    """CLI entrypoint for running the Phase 1 local worker daemon."""
    env = os.environ
    configure_logging(
        log_format=env.get("LOG_FORMAT", "console"),
        level=env.get("LOG_LEVEL", "INFO"),
    )

    region = env.get("AWS_REGION", "us-east-1")
    queue_url = env["SQS_QUEUE_URL"]
    run_state_table = env["RUN_STATE_TABLE"]

    cred_provider = _build_credential_provider(env, region)
    github_clients = create_github_clients(cred_provider)
    worker_ctx = WorkerContext(
        github_clients=github_clients,
        agent_backend=create_agent_backend(env),
        prompt_assembler=PromptAssembler(),
        workspace_manager=WorkspaceManager(),
        state_store=DynamoDBRunStateStore(run_state_table, region_name=region),
    )

    daemon_cfg = LocalRunnerConfig(
        sqs_queue_url=queue_url,
        wait_time_seconds=int(env.get("SQS_WAIT_TIME_SECONDS", "20")),
        visibility_timeout_seconds=int(env.get("SQS_VISIBILITY_TIMEOUT_SECONDS", "120")),
        heartbeat_interval_seconds=float(env.get("SQS_HEARTBEAT_INTERVAL_SECONDS", "45")),
        max_concurrency=int(env.get("LOCAL_RUNNER_CONCURRENCY", "4")),
    )

    sqs_client = boto3.client("sqs", region_name=region)
    daemon = LocalRunnerDaemon(
        sqs_client=sqs_client,
        config=daemon_cfg,
        task_executor=lambda envelope: handle_task(envelope, worker_ctx),
    )

    def _handle_signal(signum: int, _frame: Any) -> None:
        logger.info("Received signal %d; stopping local runner", signum)
        daemon.request_shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Starting agentic local runner (region=%s, concurrency=%d)",
        region,
        daemon_cfg.max_concurrency,
    )
    try:
        daemon.run_forever()
    finally:
        github_clients.close()


if __name__ == "__main__":
    main()
