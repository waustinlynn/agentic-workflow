"""Comprehensive offline unit tests for Webhook Ingest security, routing, and SQS FIFO."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from ingest.handler import IngestService, _parse_runtime_queue_urls
from ingest.registry import DynamoDBRepoRegistry, RepoRegistryEntry
from ingest.router import resolve_runtime_queue_key
from ingest.security import verify_github_signature
from shared.github_client import BootstrapLabelsResult
from shared.models import ExecutionTarget, RuntimeKind, TaskEnvelope, TaskType

WEBHOOK_SECRET = "test-webhook-secret-32bytes"
ALLOWED_ACTORS = frozenset({"waustinlynn", "agentic-coder[bot]", "agentic-reviewer[bot]"})
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/agentic-workflow-prod-tasks.fifo"


class _FakeRepoRegistry:
    def __init__(self, entries: dict[str, RepoRegistryEntry]) -> None:
        self._entries = entries

    def get_repo(self, repo_full_name: str) -> RepoRegistryEntry | None:
        return self._entries.get(repo_full_name)


@dataclass
class _SentMessage:
    queue_url: str
    message_body: str
    message_group_id: str
    message_deduplication_id: str


class _FakeSQSClient:
    def __init__(self) -> None:
        self.messages: list[_SentMessage] = []

    def send_message(
        self,
        *,
        QueueUrl: str,
        MessageBody: str,
        MessageGroupId: str,
        MessageDeduplicationId: str,
    ) -> Mapping[str, Any]:
        self.messages.append(
            _SentMessage(
                queue_url=QueueUrl,
                message_body=MessageBody,
                message_group_id=MessageGroupId,
                message_deduplication_id=MessageDeduplicationId,
            )
        )
        return {"MessageId": f"msg-{len(self.messages)}"}


class _FakeDynamoDBClient:
    def __init__(self, items: dict[str, Mapping[str, Any]]) -> None:
        self._items = items

    def get_item(self, *, TableName: str, Key: Mapping[str, Any]) -> Mapping[str, Any]:
        repo = Key["repo_full_name"]["S"]
        item = self._items.get(repo)
        return {"Item": item} if item is not None else {}


def _sign(body: bytes, secret: str = WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _make_apigw_event(
    *,
    event_name: str,
    delivery_id: str,
    payload: Mapping[str, Any],
    signature: str | None = None,
    base64_encode: bool = False,
) -> dict[str, Any]:
    raw_bytes = json.dumps(payload).encode("utf-8")
    sig = _sign(raw_bytes) if signature is None else signature
    body_str = (
        base64.b64encode(raw_bytes).decode("ascii") if base64_encode else raw_bytes.decode("utf-8")
    )
    headers: dict[str, str] = {
        "X-GitHub-Event": event_name,
        "X-GitHub-Delivery": delivery_id,
    }
    if sig != "<omit>":
        headers["X-Hub-Signature-256"] = sig
    return {
        "headers": headers,
        "body": body_str,
        "isBase64Encoded": base64_encode,
    }


def _default_registry() -> _FakeRepoRegistry:
    return _FakeRepoRegistry(
        {
            "waustinlynn/repo-a": RepoRegistryEntry(
                repo_full_name="waustinlynn/repo-a",
                message_group_id="repo-waustinlynn-repo-a",
                execution_target=ExecutionTarget.LOCAL,
                runtime=RuntimeKind.PYTHON,
                enabled=True,
            ),
            "waustinlynn/repo-b": RepoRegistryEntry(
                repo_full_name="waustinlynn/repo-b",
                message_group_id="repo-waustinlynn-repo-b",
                execution_target=ExecutionTarget.LAMBDA,
                runtime=RuntimeKind.NODE,
                enabled=True,
            ),
            "waustinlynn/disabled-repo": RepoRegistryEntry(
                repo_full_name="waustinlynn/disabled-repo",
                message_group_id="repo-disabled",
                enabled=False,
            ),
        }
    )


def _make_service(
    *,
    sqs: _FakeSQSClient | None = None,
    bootstrapped: list[str] | None = None,
) -> tuple[IngestService, _FakeSQSClient, list[str]]:
    sqs_client = sqs or _FakeSQSClient()
    bootstrapped_repos = bootstrapped if bootstrapped is not None else []

    def _bootstrap(repo: str) -> BootstrapLabelsResult:
        bootstrapped_repos.append(repo)
        return BootstrapLabelsResult(created=["agent:ready"], updated=[], unchanged=[])

    service = IngestService(
        webhook_secret_provider=lambda: WEBHOOK_SECRET,
        registry=_default_registry(),
        sqs_client=sqs_client,
        sqs_queue_url=QUEUE_URL,
        allowed_actors=ALLOWED_ACTORS,
        label_bootstrapper=_bootstrap,
    )
    return service, sqs_client, bootstrapped_repos


class TestSignatureVerification:
    def test_valid_and_tampered_signatures(self) -> None:
        raw = b'{"zen": "Keep it logically awesome."}'
        valid = _sign(raw)
        assert verify_github_signature(raw, valid, WEBHOOK_SECRET) is True
        assert verify_github_signature(raw + b" ", valid, WEBHOOK_SECRET) is False
        assert verify_github_signature(raw, "sha1=abc", WEBHOOK_SECRET) is False
        assert verify_github_signature(raw, None, WEBHOOK_SECRET) is False

    @pytest.mark.parametrize("bad_sig", ["<omit>", "", "sha256=deadbeef", "invalid"])
    def test_invalid_signature_returns_401(self, bad_sig: str) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="ping",
            delivery_id="deliv-bad",
            payload={"zen": "hi"},
            signature=bad_sig,
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 401
        assert sqs.messages == []

    def test_ping_event_with_base64_body_returns_200(self) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="ping",
            delivery_id="deliv-ping-1",
            payload={"zen": "Responsive is better than fast."},
            base64_encode=True,
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body == {"status": "ok", "event": "ping", "delivery_id": "deliv-ping-1"}
        assert sqs.messages == []


class TestPublicRepoSecurityBoundaries:
    def test_rejects_unregistered_and_disabled_repos(self) -> None:
        service, sqs, _ = _make_service()
        for repo in ("waustinlynn/unknown-repo", "waustinlynn/disabled-repo"):
            event = _make_apigw_event(
                event_name="issues",
                delivery_id=f"deliv-{repo}",
                payload={
                    "action": "labeled",
                    "repository": {"full_name": repo},
                    "sender": {"login": "waustinlynn"},
                    "label": {"name": "agent:ready"},
                    "issue": {"number": 1, "title": "Test", "labels": [{"name": "agent:ready"}]},
                },
            )
            resp = service.handle_event(event)
            assert resp["statusCode"] == 202
            assert json.loads(resp["body"])["status"] == "ignored"
        assert sqs.messages == []

    def test_rejects_unauthorized_external_user_on_public_repo(self) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="issues",
            delivery_id="deliv-ext-user",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "random-stranger"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 5, "title": "Prompt injection", "labels": []},
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 202
        assert "unauthorized actor" in json.loads(resp["body"])["reason"]
        assert sqs.messages == []

    @pytest.mark.parametrize(
        "head_repo",
        [
            {"full_name": "attacker/repo-a", "fork": True},
            None,  # Deleted fork
        ],
    )
    def test_rejects_external_fork_pull_request_review(
        self,
        head_repo: dict[str, Any] | None,
    ) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="pull_request_review",
            delivery_id="deliv-fork-pr",
            payload={
                "action": "submitted",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "waustinlynn"},
                "review": {
                    "id": 99,
                    "state": "changes_requested",
                    "user": {"login": "waustinlynn"},
                },
                "pull_request": {
                    "number": 12,
                    "labels": [],
                    "head": {"ref": "agent/issue-5", "sha": "abc", "repo": head_repo},
                },
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 202
        assert "fork" in json.loads(resp["body"])["reason"]
        assert sqs.messages == []

    def test_respects_agent_paused_kill_switch_on_issue_and_pr(self) -> None:
        service, sqs, _ = _make_service()
        paused_issue = _make_apigw_event(
            event_name="issues",
            delivery_id="deliv-paused-issue",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {
                    "number": 8,
                    "title": "Paused issue",
                    "labels": [{"name": "agent:ready"}, {"name": "agent:paused"}],
                },
            },
        )
        paused_pr = _make_apigw_event(
            event_name="pull_request_review",
            delivery_id="deliv-paused-pr",
            payload={
                "action": "submitted",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "agentic-reviewer[bot]"},
                "review": {
                    "id": 100,
                    "state": "changes_requested",
                    "user": {"login": "agentic-reviewer[bot]"},
                },
                "pull_request": {
                    "number": 9,
                    "labels": [{"name": "agent:paused"}],
                    "head": {
                        "ref": "agent/issue-8",
                        "sha": "def",
                        "repo": {"full_name": "waustinlynn/repo-a"},
                    },
                },
            },
        )
        for ev in (paused_issue, paused_pr):
            resp = service.handle_event(ev)
            assert resp["statusCode"] == 202
            assert "paused" in json.loads(resp["body"])["reason"]
        assert sqs.messages == []


class TestEventRoutingAndFifoGrouping:
    @pytest.mark.parametrize(
        ("label_name", "expected_task"),
        [
            ("agent:plan", TaskType.PLAN),
            ("agent:ready", TaskType.CODE_AND_TEST),
        ],
    )
    def test_routes_issue_labels(self, label_name: str, expected_task: TaskType) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="issues",
            delivery_id=f"deliv-{label_name}",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": label_name},
                "issue": {
                    "number": 42,
                    "title": "Add caching",
                    "labels": [{"name": label_name}],
                },
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 202
        assert len(sqs.messages) == 1
        sent = sqs.messages[0]
        assert sent.queue_url == QUEUE_URL
        assert sent.message_group_id == "repo-waustinlynn-repo-a"
        assert sent.message_deduplication_id == f"deliv-{label_name}"

        envelope = TaskEnvelope.model_validate_json(sent.message_body)
        assert envelope.task_type is expected_task
        assert envelope.repo_full_name == "waustinlynn/repo-a"
        assert envelope.issue_or_pr_number == 42
        assert envelope.execution_target is ExecutionTarget.LOCAL
        assert envelope.runtime is RuntimeKind.PYTHON

    def test_routes_changes_requested_review_to_address_review(self) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="pull_request_review",
            delivery_id="deliv-review-1",
            payload={
                "action": "submitted",
                "repository": {"full_name": "waustinlynn/repo-b"},
                "sender": {"login": "agentic-reviewer[bot]"},
                "review": {
                    "id": 77,
                    "state": "changes_requested",
                    "user": {"login": "agentic-reviewer[bot]"},
                },
                "pull_request": {
                    "number": 15,
                    "labels": [{"name": "agent:in-review"}],
                    "head": {
                        "ref": "agent/issue-10",
                        "sha": "111222",
                        "repo": {"full_name": "waustinlynn/repo-b"},
                    },
                },
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 202
        assert len(sqs.messages) == 1
        sent = sqs.messages[0]
        assert sent.message_group_id == "repo-waustinlynn-repo-b"
        assert sent.message_deduplication_id == "deliv-review-1"

        envelope = TaskEnvelope.model_validate_json(sent.message_body)
        assert envelope.task_type is TaskType.ADDRESS_REVIEW
        assert envelope.branch == "agent/issue-10"
        assert envelope.execution_target is ExecutionTarget.LAMBDA
        assert envelope.runtime is RuntimeKind.NODE

    @pytest.mark.parametrize(
        ("conclusion", "expected_task"),
        [
            ("success", TaskType.REVIEW),
            ("failure", TaskType.FIX_CI),
        ],
    )
    def test_routes_check_suite_completed_on_agent_branch(
        self,
        conclusion: str,
        expected_task: TaskType,
    ) -> None:
        service, sqs, _ = _make_service()
        event = _make_apigw_event(
            event_name="check_suite",
            delivery_id=f"deliv-ci-{conclusion}",
            payload={
                "action": "completed",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "agentic-coder[bot]"},
                "check_suite": {
                    "id": 500,
                    "conclusion": conclusion,
                    "head_branch": "agent/issue-3",
                    "head_sha": "abc999",
                    "pull_requests": [
                        {
                            "number": 19,
                            "head": {"ref": "agent/issue-3", "repo": {"id": 100}},
                            "base": {"ref": "main", "repo": {"id": 100}},
                        }
                    ],
                },
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 202
        assert len(sqs.messages) == 1
        envelope = TaskEnvelope.model_validate_json(sqs.messages[0].message_body)
        assert envelope.task_type is expected_task
        assert envelope.issue_or_pr_number == 19
        assert envelope.branch == "agent/issue-3"

    def test_ignores_check_suite_for_non_agent_branch_or_fork(self) -> None:
        service, sqs, _ = _make_service()
        non_agent = _make_apigw_event(
            event_name="check_suite",
            delivery_id="deliv-ci-human",
            payload={
                "action": "completed",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "waustinlynn"},
                "check_suite": {
                    "id": 501,
                    "conclusion": "success",
                    "head_branch": "feat/manual-work",
                    "pull_requests": [
                        {
                            "number": 20,
                            "head": {"ref": "feat/manual-work", "repo": {"id": 100}},
                            "base": {"ref": "main", "repo": {"id": 100}},
                        }
                    ],
                },
            },
        )
        fork_ci = _make_apigw_event(
            event_name="check_suite",
            delivery_id="deliv-ci-fork",
            payload={
                "action": "completed",
                "repository": {"full_name": "waustinlynn/repo-a"},
                "sender": {"login": "waustinlynn"},
                "check_suite": {
                    "id": 502,
                    "conclusion": "success",
                    "head_branch": "agent/spoof",
                    "pull_requests": [
                        {
                            "number": 21,
                            "head": {"ref": "agent/spoof", "repo": {"id": 999}},
                            "base": {"ref": "main", "repo": {"id": 100}},
                        }
                    ],
                },
            },
        )
        assert service.handle_event(non_agent)["statusCode"] == 202
        assert service.handle_event(fork_ci)["statusCode"] == 202
        assert sqs.messages == []

    def test_same_repo_shares_group_id_and_different_repos_get_distinct_group_ids(self) -> None:
        service, sqs, _ = _make_service()
        for idx, repo in enumerate(
            ("waustinlynn/repo-a", "waustinlynn/repo-a", "waustinlynn/repo-b"),
            start=1,
        ):
            event = _make_apigw_event(
                event_name="issues",
                delivery_id=f"deliv-{idx}",
                payload={
                    "action": "labeled",
                    "repository": {"full_name": repo},
                    "sender": {"login": "waustinlynn"},
                    "label": {"name": "agent:ready"},
                    "issue": {"number": idx, "title": f"Task {idx}", "labels": []},
                },
            )
            assert service.handle_event(event)["statusCode"] == 202

        assert len(sqs.messages) == 3
        assert sqs.messages[0].message_group_id == sqs.messages[1].message_group_id
        assert sqs.messages[0].message_group_id != sqs.messages[2].message_group_id
        assert [m.message_deduplication_id for m in sqs.messages] == [
            "deliv-1",
            "deliv-2",
            "deliv-3",
        ]

    def test_installation_repositories_bootstraps_only_registered_repos(self) -> None:
        service, sqs, bootstrapped = _make_service()
        event = _make_apigw_event(
            event_name="installation_repositories",
            delivery_id="deliv-install-1",
            payload={
                "action": "added",
                "sender": {"login": "waustinlynn"},
                "repositories_added": [
                    {"full_name": "waustinlynn/repo-a"},
                    {"full_name": "waustinlynn/unregistered"},
                    {"full_name": "waustinlynn/disabled-repo"},
                    {"full_name": "waustinlynn/repo-b"},
                ],
            },
        )
        resp = service.handle_event(event)
        assert resp["statusCode"] == 200
        assert bootstrapped == ["waustinlynn/repo-a", "waustinlynn/repo-b"]
        assert sqs.messages == []


class TestDynamoDBRepoRegistry:
    def test_deserializes_dynamodb_item_and_handles_missing_repo(self) -> None:
        fake_ddb = _FakeDynamoDBClient(
            {
                "waustinlynn/agentic-workflow": {
                    "repo_full_name": {"S": "waustinlynn/agentic-workflow"},
                    "message_group_id": {"S": "repo-waustinlynn-agentic-workflow"},
                    "execution_target": {"S": "local"},
                    "runtime": {"S": "python"},
                    "enabled": {"BOOL": True},
                }
            }
        )
        registry = DynamoDBRepoRegistry(fake_ddb, "agentic-workflow-prod-repo-registry")
        entry = registry.get_repo("waustinlynn/agentic-workflow")
        assert entry is not None
        assert entry.message_group_id == "repo-waustinlynn-agentic-workflow"
        assert entry.execution_target is ExecutionTarget.LOCAL
        assert entry.runtime is RuntimeKind.PYTHON
        assert entry.enabled is True
        assert registry.get_repo("waustinlynn/missing") is None


class TestPhase2RuntimeQueueRouting:
    RUNTIME_QUEUES: ClassVar[dict[str, str]] = {
        "base": "https://sqs.us-east-1.amazonaws.com/123456789012/worker-base.fifo",
        "python": "https://sqs.us-east-1.amazonaws.com/123456789012/worker-python.fifo",
        "node": "https://sqs.us-east-1.amazonaws.com/123456789012/worker-node.fifo",
        "infra": "https://sqs.us-east-1.amazonaws.com/123456789012/worker-infra.fifo",
    }

    @pytest.mark.parametrize(
        ("target", "runtime", "task_type", "expected_key"),
        [
            (ExecutionTarget.LOCAL, RuntimeKind.PYTHON, TaskType.CODE_AND_TEST, None),
            (ExecutionTarget.LOCAL, RuntimeKind.NODE, TaskType.REVIEW, None),
            (ExecutionTarget.LAMBDA, RuntimeKind.NODE, TaskType.REVIEW, "base"),
            (ExecutionTarget.LAMBDA, RuntimeKind.PYTHON, TaskType.PLAN, "base"),
            (ExecutionTarget.LAMBDA, RuntimeKind.BASE, TaskType.CODE_AND_TEST, "base"),
            (ExecutionTarget.LAMBDA, RuntimeKind.CI_VERIFIED, TaskType.CODE_AND_TEST, "base"),
            (ExecutionTarget.LAMBDA, RuntimeKind.CI_VERIFIED, TaskType.FIX_CI, "base"),
            (ExecutionTarget.LAMBDA, RuntimeKind.PYTHON, TaskType.CODE_AND_TEST, "python"),
            (ExecutionTarget.LAMBDA, RuntimeKind.NODE, TaskType.ADDRESS_REVIEW, "node"),
            (ExecutionTarget.LAMBDA, RuntimeKind.INFRA, TaskType.FIX_CI, "infra"),
            (ExecutionTarget.LAMBDA, RuntimeKind.RUST, TaskType.CODE_AND_TEST, "rust"),
        ],
    )
    def test_resolve_runtime_queue_key(
        self,
        target: ExecutionTarget,
        runtime: RuntimeKind,
        task_type: TaskType,
        expected_key: str | None,
    ) -> None:
        task = TaskEnvelope(
            task_type=task_type,
            repo_full_name="waustinlynn/demo",
            issue_or_pr_number=1,
            delivery_id="d-1",
            actor="waustinlynn",
            execution_target=target,
            runtime=runtime,
        )
        assert resolve_runtime_queue_key(task) == expected_key

    def test_ingest_dispatches_to_runtime_specific_and_base_fifo_queues(self) -> None:
        registry = _FakeRepoRegistry(
            {
                "waustinlynn/local-py": RepoRegistryEntry(
                    repo_full_name="waustinlynn/local-py",
                    message_group_id="repo-local-py",
                    execution_target=ExecutionTarget.LOCAL,
                    runtime=RuntimeKind.PYTHON,
                ),
                "waustinlynn/lambda-node": RepoRegistryEntry(
                    repo_full_name="waustinlynn/lambda-node",
                    message_group_id="repo-lambda-node",
                    execution_target=ExecutionTarget.LAMBDA,
                    runtime=RuntimeKind.NODE,
                ),
                "waustinlynn/lambda-civerified": RepoRegistryEntry(
                    repo_full_name="waustinlynn/lambda-civerified",
                    message_group_id="repo-lambda-ci",
                    execution_target=ExecutionTarget.LAMBDA,
                    runtime=RuntimeKind.CI_VERIFIED,
                ),
                "waustinlynn/lambda-rust": RepoRegistryEntry(
                    repo_full_name="waustinlynn/lambda-rust",
                    message_group_id="repo-lambda-rust",
                    execution_target=ExecutionTarget.LAMBDA,
                    runtime=RuntimeKind.RUST,
                ),
            }
        )
        sqs = _FakeSQSClient()
        service = IngestService(
            webhook_secret_provider=lambda: WEBHOOK_SECRET,
            registry=registry,
            sqs_client=sqs,
            sqs_queue_url=QUEUE_URL,
            runtime_queue_urls=self.RUNTIME_QUEUES,
            allowed_actors=ALLOWED_ACTORS,
        )

        # 1. Local target stays on primary local queue.
        ev_local = _make_apigw_event(
            event_name="issues",
            delivery_id="d-local",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/local-py"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 1, "title": "Local work", "labels": []},
            },
        )
        resp_local = service.handle_event(ev_local)
        assert json.loads(resp_local["body"])["queue_route"] == "local"
        assert sqs.messages[-1].queue_url == QUEUE_URL

        # 2. Lambda node coding -> worker-node.fifo
        ev_node_code = _make_apigw_event(
            event_name="issues",
            delivery_id="d-node-code",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/lambda-node"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 2, "title": "Node work", "labels": []},
            },
        )
        resp_node = service.handle_event(ev_node_code)
        assert json.loads(resp_node["body"])["queue_route"] == "lambda:node"
        assert sqs.messages[-1].queue_url == self.RUNTIME_QUEUES["node"]

        # 3. Lambda node review -> worker-base.fifo (read-only lightweight image)
        ev_node_review = _make_apigw_event(
            event_name="check_suite",
            delivery_id="d-node-review",
            payload={
                "action": "completed",
                "repository": {"full_name": "waustinlynn/lambda-node"},
                "sender": {"login": "agentic-coder[bot]"},
                "check_suite": {
                    "id": 900,
                    "conclusion": "success",
                    "head_branch": "agent/issue-2",
                    "head_sha": "abc",
                    "pull_requests": [
                        {
                            "number": 10,
                            "head": {"ref": "agent/issue-2", "repo": {"id": 1}},
                            "base": {"ref": "main", "repo": {"id": 1}},
                        }
                    ],
                },
            },
        )
        resp_rev = service.handle_event(ev_node_review)
        assert json.loads(resp_rev["body"])["queue_route"] == "lambda:base"
        assert sqs.messages[-1].queue_url == self.RUNTIME_QUEUES["base"]

        # 4. Lambda ci-verified -> worker-base.fifo
        ev_ci_verified = _make_apigw_event(
            event_name="issues",
            delivery_id="d-ci-verified",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/lambda-civerified"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 3, "title": "Mobile task", "labels": []},
            },
        )
        resp_ci = service.handle_event(ev_ci_verified)
        assert json.loads(resp_ci["body"])["queue_route"] == "lambda:base"
        assert sqs.messages[-1].queue_url == self.RUNTIME_QUEUES["base"]

        # 5. Unprovisioned stack runtime (rust) gracefully falls back to worker-base.fifo
        ev_rust = _make_apigw_event(
            event_name="issues",
            delivery_id="d-rust",
            payload={
                "action": "labeled",
                "repository": {"full_name": "waustinlynn/lambda-rust"},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 4, "title": "Rust task", "labels": []},
            },
        )
        resp_rust = service.handle_event(ev_rust)
        assert json.loads(resp_rust["body"])["queue_route"] == "lambda:base"
        assert sqs.messages[-1].queue_url == self.RUNTIME_QUEUES["base"]

    def test_parse_runtime_queue_urls(self) -> None:
        assert _parse_runtime_queue_urls("") == {}
        assert _parse_runtime_queue_urls('{"BASE": "https://q1", "python": "https://q2"}') == {
            "base": "https://q1",
            "python": "https://q2",
        }
        with pytest.raises(ValueError, match="JSON object"):
            _parse_runtime_queue_urls('["not-a-dict"]')
