"""Offline closed-loop E2E test: Ingest -> SQS FIFO -> LocalRunner -> Coder -> Reviewer."""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ingest.handler import IngestService
from ingest.registry import RepoRegistryEntry
from shared.github_auth import AppIdentity, GitHubAppCredentials
from shared.github_client import create_github_clients
from shared.labels import AgentLabel
from shared.models import ExecutionTarget, RuntimeKind
from worker.agent_backend import AgentBackend, AgentTurnResult
from worker.handlers import WorkerContext, handle_task
from worker.local_runner import LocalRunnerConfig, LocalRunnerDaemon
from worker.prompts import PromptAssembler
from worker.state_store import InMemoryRunStateStore
from worker.workspace import WorkspaceManager

SECRET = "e2e-webhook-secret"
REPO = "waustinlynn/sandbox"
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/000000000000/agentic-tasks.fifo"


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _create_sandbox_bare_repo(tmp_path: Path) -> Path:
    bare = tmp_path / "sandbox.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], cwd=bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["clone", str(bare), str(seed)], cwd=tmp_path)
    _git(["config", "user.name", "Seed"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    (seed / "math_ops.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8"
    )
    cfg = seed / ".agentic" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "version: 1\nstack: backend\nexecution:\n  max_review_rounds: 3\n  verify_commands:\n"
        "    - 'python3 -c \"import math_ops; assert math_ops.sub(5, 2) == 3\"'\n"
        "review:\n  wait_for_ci: true\n  auto_merge: true\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "chore: initial sandbox"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return bare


class _InMemoryFifoQueue:
    """In-memory SQS FIFO queue connecting IngestService and LocalRunnerDaemon."""

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.seen_dedup_ids: set[str] = set()
        self.deleted_handles: list[str] = []

    def send_message(
        self,
        *,
        QueueUrl: str,
        MessageBody: str,
        MessageGroupId: str,
        MessageDeduplicationId: str,
    ) -> Mapping[str, Any]:
        if MessageDeduplicationId in self.seen_dedup_ids:
            return {"MessageId": f"dedup-{MessageDeduplicationId}"}
        self.seen_dedup_ids.add(MessageDeduplicationId)
        msg_id = f"msg-{len(self.seen_dedup_ids)}"
        self.queue.append(
            {
                "MessageId": msg_id,
                "ReceiptHandle": f"rh-{msg_id}",
                "Attributes": {"MessageGroupId": MessageGroupId},
                "Body": MessageBody,
            }
        )
        return {"MessageId": msg_id}

    def receive_message(
        self,
        *,
        QueueUrl: str,
        MaxNumberOfMessages: int,
        WaitTimeSeconds: int,
        AttributeNames: list[str],
    ) -> Mapping[str, Any]:
        if not self.queue:
            return {"Messages": []}
        batch = self.queue[:MaxNumberOfMessages]
        del self.queue[:MaxNumberOfMessages]
        return {"Messages": batch}

    def change_message_visibility(
        self,
        *,
        QueueUrl: str,
        ReceiptHandle: str,
        VisibilityTimeout: int,
    ) -> Mapping[str, Any]:
        return {}

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> Mapping[str, Any]:
        self.deleted_handles.append(ReceiptHandle)
        return {}


class _StaticRegistry:
    def __init__(self, entry: RepoRegistryEntry) -> None:
        self._entry = entry

    def get_repo(self, repo_full_name: str) -> RepoRegistryEntry | None:
        return self._entry if repo_full_name == self._entry.repo_full_name else None


class _StaticCreds:
    def __init__(self, creds: dict[AppIdentity, GitHubAppCredentials]) -> None:
        self._creds = creds

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        return self._creds[identity]


def _signed_webhook(
    event_name: str, delivery_id: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    raw = json.dumps(payload)
    sig = "sha256=" + hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return {
        "headers": {
            "X-GitHub-Event": event_name,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": sig,
        },
        "body": raw,
        "isBase64Encoded": False,
    }


class _ClosedLoopAgent(AgentBackend):
    """Simulates a 2-pass coding & review loop (REQUEST_CHANGES -> fix -> APPROVE)."""

    def __init__(self) -> None:
        self.review_calls = 0

    def run_turn(
        self,
        prompt: str,
        workspace_dir: Path,
        *,
        schema: dict[str, Any] | Path | None = None,
        conversation_id: str | None = None,
        timeout: float = 600.0,
        read_only: bool = False,
    ) -> AgentTurnResult:
        if "Task: Code and Test" in prompt:
            (workspace_dir / "math_ops.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n    return a - b\n",
                encoding="utf-8",
            )
            return AgentTurnResult(
                text="coded",
                conversation_id="conv-1",
                structured_output={
                    "summary": "Added sub(a, b)",
                    "files_changed": ["math_ops.py"],
                    "tests_added_or_updated": [],
                    "verify_results": [],
                    "commit_message": "feat: add sub helper",
                    "pr_title": "feat: add sub helper",
                    "pr_body": "Closes #1",
                    "blocked_reason": None,
                },
            )

        if "Task: Automated Code Review" in prompt:
            self.review_calls += 1
            if self.review_calls == 1:
                return AgentTurnResult(
                    text="request changes",
                    conversation_id="conv-rev-1",
                    structured_output={
                        "verdict": "REQUEST_CHANGES",
                        "summary": "Add docstring to sub().",
                        "comments": [
                            {
                                "path": "math_ops.py",
                                "line": 4,
                                "severity": "blocker",
                                "body": "Document sub().",
                            }
                        ],
                    },
                )
            return AgentTurnResult(
                text="approve",
                conversation_id="conv-rev-2",
                structured_output={
                    "verdict": "APPROVE",
                    "summary": "Docstring added; ready to merge.",
                    "comments": [],
                },
            )

        # Task: Address PR Review Feedback
        (workspace_dir / "math_ops.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            'def sub(a: int, b: int) -> int:\n    """Subtract b from a."""\n    return a - b\n',
            encoding="utf-8",
        )
        return AgentTurnResult(
            text="addressed",
            conversation_id=conversation_id or "conv-1",
            structured_output={
                "summary": "Added docstring to sub().",
                "addressed_comments": ["Added docstring to math_ops.sub"],
                "verify_results": [],
                "commit_message": "docs: document sub helper",
                "blocked_reason": None,
            },
        )


def test_full_closed_loop_issue_to_pr_review_fix_approve_automerge(tmp_path: Path) -> None:
    bare = _create_sandbox_bare_repo(tmp_path)
    fifo = _InMemoryFifoQueue()
    registry = _StaticRegistry(
        RepoRegistryEntry(
            repo_full_name=REPO,
            message_group_id="repo-waustinlynn-sandbox",
            execution_target=ExecutionTarget.LOCAL,
            runtime=RuntimeKind.PYTHON,
        )
    )
    ingest = IngestService(
        webhook_secret_provider=lambda: SECRET,
        registry=registry,
        sqs_client=fifo,
        sqs_queue_url=QUEUE_URL,
        allowed_actors=frozenset({"waustinlynn", "agentic-coder[bot]", "agentic-reviewer[bot]"}),
    )

    labels_by_item: dict[int, list[str]] = {1: [AgentLabel.READY.value], 2: []}
    reviews_log: list[dict[str, Any]] = []
    auto_merged = [False]

    def gh_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/installation"):
            token = request.headers["Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(token, options={"verify_signature": False})
            return httpx.Response(200, json={"id": 10 if claims["iss"] == "100" else 20})
        if "/access_tokens" in path:
            return httpx.Response(
                201, json={"token": "ghs_tok", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if method == "GET" and path == f"/repos/{REPO}/issues/1":
            return httpx.Response(
                200, json={"number": 1, "title": "Add sub", "body": "Add sub(a, b)."}
            )
        if method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 1})
        if method == "GET" and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            return httpx.Response(200, json=[{"name": lbl} for lbl in labels_by_item.get(num, [])])
        if method == "PUT" and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            labels_by_item[num] = list(json.loads(request.content)["labels"])
            return httpx.Response(200, json=[{"name": lbl} for lbl in labels_by_item[num]])
        if method == "GET" and path == f"/repos/{REPO}/pulls":
            return httpx.Response(200, json=[])
        if method == "POST" and path == f"/repos/{REPO}/pulls":
            return httpx.Response(201, json={"number": 2, "node_id": "PR_2"})
        if method == "GET" and path == f"/repos/{REPO}/pulls/2":
            if request.headers.get("Accept") == "application/vnd.github.v3.diff":
                return httpx.Response(
                    200, text="diff --git a/math_ops.py b/math_ops.py\n+def sub..."
                )
            return httpx.Response(
                200,
                json={
                    "number": 2,
                    "node_id": "PR_2",
                    "title": "feat: add sub helper",
                    "body": "Closes #1",
                    "labels": [{"name": lbl} for lbl in labels_by_item.get(2, [])],
                    "head": {"ref": "agent/issue-1", "sha": "sha1"},
                },
            )
        if method == "GET" and path == f"/repos/{REPO}/pulls/2/reviews":
            return httpx.Response(
                200,
                json=[
                    {
                        "state": "CHANGES_REQUESTED",
                        "user": {"login": "agentic-reviewer[bot]"},
                        "body": "Add docstring.",
                    }
                ],
            )
        if method == "POST" and path == f"/repos/{REPO}/pulls/2/reviews":
            body = json.loads(request.content)
            reviews_log.append(body)
            return httpx.Response(200, json={"id": len(reviews_log), "state": body["event"]})
        if method == "GET" and path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {"name": "verify", "status": "completed", "conclusion": "success"}
                    ]
                },
            )
        if method == "GET" and path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if method == "POST" and path == "/graphql":
            auto_merged[0] = True
            return httpx.Response(
                200,
                json={
                    "data": {
                        "enablePullRequestAutoMerge": {"pullRequest": {"id": "PR_2", "number": 2}}
                    }
                },
            )
        return httpx.Response(404, text=f"Unhandled {method} {path}")

    creds: dict[AppIdentity, GitHubAppCredentials] = {}
    for idx, ident in enumerate((AppIdentity.CODER, AppIdentity.REVIEWER), start=1):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        creds[ident] = GitHubAppCredentials(ident, str(idx * 100), pem)

    clients = create_github_clients(
        _StaticCreds(creds),
        http_client=httpx.Client(transport=httpx.MockTransport(gh_handler)),
    )
    worker_ctx = WorkerContext(
        github_clients=clients,
        agent_backend=_ClosedLoopAgent(),
        prompt_assembler=PromptAssembler(),
        workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
        state_store=InMemoryRunStateStore(),
        clone_url_override=str(bare),
    )
    daemon = LocalRunnerDaemon(
        sqs_client=fifo,
        config=LocalRunnerConfig(sqs_queue_url=QUEUE_URL, wait_time_seconds=0, max_concurrency=1),
        task_executor=lambda env: handle_task(env, worker_ctx),
    )

    # Stage 1: Issue labeled `agent:ready` -> Ingest -> SQS -> LocalRunner -> PR #2 opened
    r1 = ingest.handle_event(
        _signed_webhook(
            "issues",
            "d-1",
            {
                "action": "labeled",
                "repository": {"full_name": REPO},
                "sender": {"login": "waustinlynn"},
                "label": {"name": "agent:ready"},
                "issue": {"number": 1, "title": "Add sub", "labels": [{"name": "agent:ready"}]},
            },
        )
    )
    assert r1["statusCode"] == 202
    daemon.run_forever(max_polls=1)
    assert labels_by_item[2] == [AgentLabel.IN_REVIEW.value]

    # Stage 2: CI completes green -> Ingest -> Review Agent -> REQUEST_CHANGES
    r2 = ingest.handle_event(
        _signed_webhook(
            "check_suite",
            "d-2",
            {
                "action": "completed",
                "repository": {"full_name": REPO},
                "sender": {"login": "agentic-coder[bot]"},
                "check_suite": {
                    "id": 10,
                    "conclusion": "success",
                    "head_branch": "agent/issue-1",
                    "head_sha": "sha1",
                    "pull_requests": [
                        {
                            "number": 2,
                            "head": {"ref": "agent/issue-1", "repo": {"id": 1}},
                            "base": {"ref": "main", "repo": {"id": 1}},
                        }
                    ],
                },
            },
        )
    )
    assert r2["statusCode"] == 202
    daemon.run_forever(max_polls=1)
    assert reviews_log[-1]["event"] == "REQUEST_CHANGES"
    assert labels_by_item[2] == [AgentLabel.CHANGES_REQUESTED.value]

    # Stage 3: REQUEST_CHANGES webhook -> Ingest -> Coding agent pushes fix commit
    r3 = ingest.handle_event(
        _signed_webhook(
            "pull_request_review",
            "d-3",
            {
                "action": "submitted",
                "repository": {"full_name": REPO},
                "sender": {"login": "agentic-reviewer[bot]"},
                "review": {
                    "id": 1,
                    "state": "changes_requested",
                    "user": {"login": "agentic-reviewer[bot]"},
                },
                "pull_request": {
                    "number": 2,
                    "labels": [{"name": "agent:changes-requested"}],
                    "head": {"ref": "agent/issue-1", "sha": "sha1", "repo": {"full_name": REPO}},
                },
            },
        )
    )
    assert r3["statusCode"] == 202
    daemon.run_forever(max_polls=1)
    assert labels_by_item[2] == [AgentLabel.IN_REVIEW.value]

    # Stage 4: CI green on fix commit -> Review Agent -> APPROVE -> auto-merge enabled
    r4 = ingest.handle_event(
        _signed_webhook(
            "check_suite",
            "d-4",
            {
                "action": "completed",
                "repository": {"full_name": REPO},
                "sender": {"login": "agentic-coder[bot]"},
                "check_suite": {
                    "id": 11,
                    "conclusion": "success",
                    "head_branch": "agent/issue-1",
                    "head_sha": "sha2",
                    "pull_requests": [
                        {
                            "number": 2,
                            "head": {"ref": "agent/issue-1", "repo": {"id": 1}},
                            "base": {"ref": "main", "repo": {"id": 1}},
                        }
                    ],
                },
            },
        )
    )
    assert r4["statusCode"] == 202
    daemon.run_forever(max_polls=1)

    assert [r["event"] for r in reviews_log] == ["REQUEST_CHANGES", "APPROVE"]
    assert auto_merged[0] is True
    assert len(fifo.deleted_handles) == 4
    # Verify both commits were pushed to the bare sandbox origin branch.
    log_out = _git(["log", "--oneline", "agent/issue-1"], cwd=bare)
    assert "feat: add sub helper" in log_out
    assert "docs: document sub helper" in log_out
