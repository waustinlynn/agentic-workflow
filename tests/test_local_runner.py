"""Unit tests for the Phase 1 Local SQS FIFO Runner Daemon and visibility heartbeat."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from shared.models import TaskEnvelope, TaskType
from worker.handlers import TaskOutcome
from worker.local_runner import LocalRunnerConfig, LocalRunnerDaemon, VisibilityHeartbeat


class _FakeSQSQueue:
    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self._batches = list(batches)
        self._lock = threading.Lock()
        self.deleted: list[str] = []
        self.visibility_extensions: list[tuple[str, int]] = []

    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int,
        WaitTimeSeconds: int,
        AttributeNames: list[str],
    ) -> Mapping[str, Any]:
        with self._lock:
            if not self._batches:
                return {"Messages": []}
            return {"Messages": self._batches.pop(0)}

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> Mapping[str, Any]:
        with self._lock:
            self.visibility_extensions.append((ReceiptHandle, VisibilityTimeout))
        return {}

    def delete_message(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
    ) -> Mapping[str, Any]:
        with self._lock:
            self.deleted.append(ReceiptHandle)
        return {}


def _sqs_msg(msg_id: str, group_id: str, repo: str, item: int) -> dict[str, Any]:
    env = TaskEnvelope(
        task_type=TaskType.CODE_AND_TEST,
        repo_full_name=repo,
        issue_or_pr_number=item,
        delivery_id=f"deliv-{msg_id}",
        actor="waustinlynn",
    )
    return {
        "MessageId": msg_id,
        "ReceiptHandle": f"rh-{msg_id}",
        "Attributes": {"MessageGroupId": group_id},
        "Body": env.model_dump_json(),
    }


class TestVisibilityHeartbeat:
    def test_extends_visibility_periodically_and_stops_cleanly(self) -> None:
        fake_sqs = _FakeSQSQueue([])
        with VisibilityHeartbeat(
            fake_sqs,
            queue_url="https://sqs.example/q.fifo",
            receipt_handle="rh-1",
            visibility_timeout_seconds=120,
            interval_seconds=0.03,
        ) as hb:
            time.sleep(0.11)

        assert hb.extensions_sent >= 2
        assert all(rh == "rh-1" and vt == 120 for rh, vt in fake_sqs.visibility_extensions)


class TestLocalRunnerDaemon:
    def test_runs_different_repos_concurrently_same_repo_sequentially_and_deletes(self) -> None:
        batches = [
            [
                _sqs_msg("m1", "repo-a", "waustinlynn/repo-a", 1),
                _sqs_msg("m2", "repo-a", "waustinlynn/repo-a", 2),
                _sqs_msg("m3", "repo-b", "waustinlynn/repo-b", 3),
            ]
        ]
        fake_sqs = _FakeSQSQueue(batches)

        state_lock = threading.Lock()
        active_by_repo: dict[str, int] = {}
        max_active_by_repo: dict[str, int] = {}
        max_total_active = [0]
        total_active = [0]

        def executor(envelope: TaskEnvelope) -> TaskOutcome:
            repo = envelope.repo_full_name
            with state_lock:
                active_by_repo[repo] = active_by_repo.get(repo, 0) + 1
                max_active_by_repo[repo] = max(
                    max_active_by_repo.get(repo, 0),
                    active_by_repo[repo],
                )
                total_active[0] += 1
                max_total_active[0] = max(max_total_active[0], total_active[0])

            time.sleep(0.08)

            with state_lock:
                active_by_repo[repo] -= 1
                total_active[0] -= 1

            return TaskOutcome(
                status="completed",
                task_type=envelope.task_type,
                repo_full_name=repo,
                issue_or_pr_number=envelope.issue_or_pr_number,
                summary="done",
            )

        daemon = LocalRunnerDaemon(
            sqs_client=fake_sqs,
            config=LocalRunnerConfig(
                sqs_queue_url="https://sqs.example/q.fifo",
                wait_time_seconds=0,
                visibility_timeout_seconds=120,
                heartbeat_interval_seconds=0.03,
                max_concurrency=3,
            ),
            task_executor=executor,
        )
        daemon.run_forever(max_polls=1)

        # All 3 messages processed and deleted.
        assert sorted(fake_sqs.deleted) == ["rh-m1", "rh-m2", "rh-m3"]
        # Heartbeat extended visibility during execution.
        assert len(fake_sqs.visibility_extensions) >= 2
        # Same repo strictly sequential (never > 1 active for repo-a).
        assert max_active_by_repo["waustinlynn/repo-a"] == 1
        # Different repos ran concurrently (repo-a and repo-b overlapped).
        assert max_total_active[0] >= 2
