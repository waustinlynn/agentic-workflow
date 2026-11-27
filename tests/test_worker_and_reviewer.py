"""Unit tests for shared worker task handlers, circuit breaker, and Option A Review Agent."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.github_auth import AppIdentity, GitHubAppCredentials
from shared.github_client import GitHubClientPair, create_github_clients
from shared.labels import AgentLabel
from shared.models import (
    ReviewVerdict,
    RunState,
    RunStatus,
    TaskEnvelope,
    TaskType,
)
from worker.agent_backend import AgentBackend, AgentTurnResult
from worker.handlers import WorkerContext, handle_task
from worker.prompts import PromptAssembler
from worker.state_store import InMemoryRunStateStore
from worker.workspace import WorkspaceManager


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _create_bare_repo(tmp_path: Path, config_yaml: str = "") -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], cwd=bare)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["clone", str(bare), str(seed)], cwd=tmp_path)
    _git(["config", "user.name", "Test"], cwd=seed)
    _git(["config", "user.email", "test@example.com"], cwd=seed)
    (seed / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if config_yaml:
        cfg = seed / ".agentic" / "config.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(config_yaml, encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "chore: seed"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return bare


class _StaticCreds:
    def __init__(self, creds: dict[AppIdentity, GitHubAppCredentials]) -> None:
        self._creds = creds

    def get_credentials(self, identity: AppIdentity) -> GitHubAppCredentials:
        return self._creds[identity]


@pytest.fixture(scope="module")
def app_keys() -> dict[AppIdentity, GitHubAppCredentials]:
    out: dict[AppIdentity, GitHubAppCredentials] = {}
    for idx, identity in enumerate((AppIdentity.CODER, AppIdentity.REVIEWER), start=1):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        out[identity] = GitHubAppCredentials(
            identity=identity, app_id=str(idx * 100), private_key_pem=pem
        )
    return out


class _ScriptedBackend(AgentBackend):
    def __init__(
        self,
        callback: Callable[[str, Path, bool], AgentTurnResult],
    ) -> None:
        self._callback = callback
        self.calls: list[dict[str, Any]] = []

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
        self.calls.append(
            {
                "prompt": prompt,
                "workspace_dir": workspace_dir,
                "schema": schema,
                "conversation_id": conversation_id,
                "read_only": read_only,
            }
        )
        return self._callback(prompt, workspace_dir, read_only)


class _MockGitHubServer:
    def __init__(self) -> None:
        self.issue_labels: dict[int, list[str]] = {
            10: [AgentLabel.READY.value],
            20: [AgentLabel.IN_REVIEW.value],
            30: [AgentLabel.PLAN.value],
            40: [AgentLabel.TRIAGE.value],
        }
        self.pr_head_branch = "agent/issue-10"
        self.ci_conclusion = "success"
        self.ci_status = "completed"
        self.posted_comments: list[tuple[int, str]] = []
        self.reviews_submitted: list[dict[str, Any]] = []
        self.review_auth_headers: list[str] = []
        self.auto_merge_enabled_prs: list[int] = []
        self.created_prs: list[dict[str, Any]] = []
        self.created_sub_issues: list[dict[str, Any]] = []
        self.existing_prs: list[dict[str, Any]] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path.endswith("/installation"):
            token = request.headers["Authorization"].removeprefix("Bearer ")
            # Decode unverified JWT iss claim to distinguish coder ("100") vs reviewer ("200").
            import jwt as pyjwt

            claims = pyjwt.decode(token, options={"verify_signature": False})
            inst_id = 10 if claims["iss"] == "100" else 20
            return httpx.Response(200, json={"id": inst_id})
        if path == "/app/installations/10/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_coder_token", "expires_at": "2099-01-01T00:00:00Z"},
            )
        if path == "/app/installations/20/access_tokens":
            return httpx.Response(
                201,
                json={"token": "ghs_reviewer_token", "expires_at": "2099-01-01T00:00:00Z"},
            )

        if method == "GET" and path.endswith("/issues/10"):
            return httpx.Response(
                200, json={"number": 10, "title": "Update VALUE to 2", "body": "Change VALUE to 2."}
            )
        if method == "GET" and path.endswith("/issues/30"):
            return httpx.Response(
                200, json={"number": 30, "title": "Big feature", "body": "Break this down."}
            )
        if method == "GET" and path.endswith("/issues/40"):
            return httpx.Response(
                200, json={"number": 40, "title": "Triage me", "body": "Verify this issue."}
            )
        if method == "GET" and "/issues/" in path and path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            num = int(path.split("/issues/")[1].split("/")[0])
            body = json.loads(request.content)["body"]
            self.posted_comments.append((num, body))
            return httpx.Response(201, json={"id": len(self.posted_comments), "body": body})
        if method == "GET" and "/issues/" in path and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            return httpx.Response(
                200, json=[{"name": lbl} for lbl in self.issue_labels.get(num, [])]
            )
        if method == "PUT" and "/issues/" in path and path.endswith("/labels"):
            num = int(path.split("/issues/")[1].split("/")[0])
            labels = json.loads(request.content)["labels"]
            self.issue_labels[num] = list(labels)
            return httpx.Response(200, json=[{"name": lbl} for lbl in labels])
        if method == "POST" and path.endswith("/issues"):
            payload = json.loads(request.content)
            child_num = 100 + len(self.created_sub_issues) + 1
            child = {"id": child_num * 10, "number": child_num, "title": payload["title"]}
            self.created_sub_issues.append(child)
            return httpx.Response(201, json=child)
        if method == "POST" and path.endswith("/sub_issues"):
            return httpx.Response(201, json={"linked": True})
        if method == "GET" and path.endswith("/pulls"):
            return httpx.Response(200, json=self.existing_prs)
        if method == "POST" and path.endswith("/pulls"):
            payload = json.loads(request.content)
            pr = {"number": 20, "node_id": "PR_NODE_20", **payload}
            self.created_prs.append(pr)
            return httpx.Response(201, json=pr)
        if method == "GET" and path.endswith("/pulls/20"):
            if request.headers.get("Accept") == "application/vnd.github.v3.diff":
                return httpx.Response(
                    200, text="diff --git a/app.py b/app.py\n-VALUE = 1\n+VALUE = 2\n"
                )
            return httpx.Response(
                200,
                json={
                    "number": 20,
                    "node_id": "PR_NODE_20",
                    "title": "feat: update VALUE",
                    "body": "Closes #10",
                    "labels": [{"name": lbl} for lbl in self.issue_labels.get(20, [])],
                    "head": {"ref": self.pr_head_branch, "sha": "abc123"},
                },
            )
        if method == "GET" and path.endswith("/pulls/20/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "state": "CHANGES_REQUESTED",
                        "user": {"login": "agentic-reviewer[bot]"},
                        "body": "Add unit test for VALUE.",
                    }
                ],
            )
        if method == "GET" and path.endswith("/pulls/20/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "agentic-reviewer[bot]"},
                        "path": "app.py",
                        "line": 1,
                        "body": "Use constant typing.",
                    }
                ],
            )
        if method == "POST" and path.endswith("/pulls/20/reviews"):
            payload = json.loads(request.content)
            self.reviews_submitted.append(payload)
            self.review_auth_headers.append(request.headers["Authorization"])
            return httpx.Response(200, json={"id": 900, "state": payload["event"]})
        if method == "GET" and path.endswith("/check-runs"):
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        {
                            "name": "pytest",
                            "status": self.ci_status,
                            "conclusion": self.ci_conclusion
                            if self.ci_status == "completed"
                            else None,
                            "output": {
                                "title": "Test failure",
                                "summary": "AssertionError: 1 != 2",
                            },
                        }
                    ]
                },
            )
        if method == "GET" and path.endswith("/status"):
            return httpx.Response(200, json={"statuses": []})
        if method == "POST" and path == "/graphql":
            self.auto_merge_enabled_prs.append(20)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "enablePullRequestAutoMerge": {
                            "pullRequest": {"id": "PR_NODE_20", "number": 20}
                        }
                    }
                },
            )
        return httpx.Response(404, text=f"Unhandled {method} {path}")


def _build_clients(
    app_keys: dict[AppIdentity, GitHubAppCredentials],
    server: _MockGitHubServer,
) -> GitHubClientPair:
    http = httpx.Client(transport=httpx.MockTransport(server.handle))
    return create_github_clients(_StaticCreds(app_keys), http_client=http)


class TestWorkerAndReviewerLifecycle:
    def test_code_and_test_then_review_approve_happy_path(
        self,
        tmp_path: Path,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        bare = _create_bare_repo(
            tmp_path,
            config_yaml=(
                "execution:\n"
                "  max_review_rounds: 2\n"
                "  verify_commands:\n"
                "    - 'python3 -c \"import app; assert app.VALUE == 2\"'\n"
                "review:\n"
                "  wait_for_ci: true\n"
                "  auto_merge: true\n"
            ),
        )
        server = _MockGitHubServer()
        store = InMemoryRunStateStore()

        def agent_cb(_prompt: str, ws_dir: Path, read_only: bool) -> AgentTurnResult:
            if not read_only:
                (ws_dir / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
                return AgentTurnResult(
                    text="done",
                    conversation_id="conv-code-1",
                    structured_output={
                        "summary": "Updated VALUE to 2",
                        "files_changed": ["app.py"],
                        "tests_added_or_updated": [],
                        "verify_results": [],
                        "commit_message": "feat: update VALUE to 2",
                        "pr_title": "feat: update VALUE to 2",
                        "pr_body": "Updates VALUE.",
                        "blocked_reason": None,
                    },
                )
            return AgentTurnResult(
                text="approved",
                conversation_id="conv-rev-1",
                structured_output={
                    "verdict": "APPROVE",
                    "summary": "Looks great and CI is green.",
                    "comments": [],
                },
            )

        ctx = WorkerContext(
            github_clients=_build_clients(app_keys, server),
            agent_backend=_ScriptedBackend(agent_cb),
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            state_store=store,
            clone_url_override=str(bare),
        )

        # 1. Coding task
        code_outcome = handle_task(
            TaskEnvelope(
                task_type=TaskType.CODE_AND_TEST,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=10,
                delivery_id="d-code-1",
                actor="waustinlynn",
            ),
            ctx,
        )
        assert code_outcome.status == "completed"
        assert code_outcome.pr_number == 20
        assert server.issue_labels[10] == [AgentLabel.IN_REVIEW.value]
        assert server.issue_labels[20] == [AgentLabel.IN_REVIEW.value]
        assert len(server.created_prs) == 1
        assert "Closes #10" in server.created_prs[0]["body"]
        assert 20 in server.auto_merge_enabled_prs

        # 2. Review task when CI is still pending -> skipped cleanly
        server.ci_status = "in_progress"
        skipped = handle_task(
            TaskEnvelope(
                task_type=TaskType.REVIEW,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=20,
                branch="agent/issue-10",
                delivery_id="d-rev-pending",
                actor="agentic-coder[bot]",
            ),
            ctx,
        )
        assert skipped.status == "skipped"
        assert server.reviews_submitted == []

        # 3. Review task when CI is green -> formal APPROVE submitted by reviewer
        server.ci_status = "completed"
        server.ci_conclusion = "success"
        approved = handle_task(
            TaskEnvelope(
                task_type=TaskType.REVIEW,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=20,
                branch="agent/issue-10",
                delivery_id="d-rev-ok",
                actor="agentic-coder[bot]",
            ),
            ctx,
        )
        assert approved.status == "completed"
        assert len(server.reviews_submitted) == 1
        assert server.reviews_submitted[0]["event"] == ReviewVerdict.APPROVE.value
        assert server.review_auth_headers == ["Bearer ghs_reviewer_token"]
        assert store.get("waustinlynn/demo", 20) is not None
        assert store.get("waustinlynn/demo", 20).status is RunStatus.APPROVED  # type: ignore[union-attr]

    def test_review_request_changes_and_circuit_breaker_needs_human(
        self,
        tmp_path: Path,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        bare = _create_bare_repo(tmp_path, config_yaml="execution:\n  max_review_rounds: 2\n")
        server = _MockGitHubServer()
        server.pr_head_branch = "main"
        store = InMemoryRunStateStore()

        # Pre-seed attempt_count == max_review_rounds so circuit breaker fires immediately.
        store.put(
            RunState(
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=20,
                status=RunStatus.CHANGES_REQUESTED,
                branch="main",
                pr_number=20,
                attempt_count=2,
            )
        )

        backend = _ScriptedBackend(
            lambda _p, _d, _ro: AgentTurnResult(
                text="req",
                structured_output={
                    "verdict": "REQUEST_CHANGES",
                    "summary": "Missing test.",
                    "comments": [
                        {
                            "path": "app.py",
                            "line": 1,
                            "severity": "blocker",
                            "body": "Add unit test.",
                        }
                    ],
                },
            )
        )
        ctx = WorkerContext(
            github_clients=_build_clients(app_keys, server),
            agent_backend=backend,
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            state_store=store,
            clone_url_override=str(bare),
        )

        # Reviewer submits REQUEST_CHANGES -> sets agent:changes-requested
        rev_out = handle_task(
            TaskEnvelope(
                task_type=TaskType.REVIEW,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=20,
                branch="main",
                delivery_id="d-rev-rc",
                actor="agentic-coder[bot]",
            ),
            ctx,
        )
        assert rev_out.status == "completed"
        assert server.reviews_submitted[0]["event"] == "REQUEST_CHANGES"
        assert server.issue_labels[20] == [AgentLabel.CHANGES_REQUESTED.value]

        # address_review sees attempt_count == max_review_rounds -> halts & sets agent:needs-human
        backend_calls_before = len(backend.calls)
        breaker_out = handle_task(
            TaskEnvelope(
                task_type=TaskType.ADDRESS_REVIEW,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=20,
                branch="main",
                delivery_id="d-addr-breaker",
                actor="agentic-reviewer[bot]",
            ),
            ctx,
        )
        assert breaker_out.status == "needs_human"
        assert len(backend.calls) == backend_calls_before  # Agent was NOT invoked
        assert server.issue_labels[20] == [AgentLabel.NEEDS_HUMAN.value]
        assert any("max review iteration limit" in body for _, body in server.posted_comments)
        final_state = store.get("waustinlynn/demo", 20)
        assert final_state is not None and final_state.status is RunStatus.NEEDS_HUMAN

    def test_plan_creates_sub_issues_and_paused_skips(
        self,
        tmp_path: Path,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        bare = _create_bare_repo(tmp_path)
        server = _MockGitHubServer()
        store = InMemoryRunStateStore()

        backend = _ScriptedBackend(
            lambda _p, _d, _ro: AgentTurnResult(
                text="plan",
                conversation_id="conv-plan",
                structured_output={
                    "summary": "Split into 2 atomic tasks.",
                    "sub_issues": [
                        {
                            "title": "Step 1",
                            "description": "Add schema.",
                            "acceptance_criteria": ["Schema validates"],
                            "files_to_touch": ["src/schema.py"],
                            "test_cases": ["Validates good input"],
                            "estimated_minutes": 4,
                        }
                    ],
                    "open_questions": [],
                },
            )
        )
        ctx = WorkerContext(
            github_clients=_build_clients(app_keys, server),
            agent_backend=backend,
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            state_store=store,
            clone_url_override=str(bare),
        )

        out = handle_task(
            TaskEnvelope(
                task_type=TaskType.PLAN,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=30,
                delivery_id="d-plan-1",
                actor="waustinlynn",
            ),
            ctx,
        )
        assert out.status == "completed"
        assert len(server.created_sub_issues) == 1
        assert server.created_sub_issues[0]["title"] == "Step 1"
        assert AgentLabel.PLAN.value not in server.issue_labels[30]

        # Paused issue is skipped immediately without invoking backend.
        server.issue_labels[10] = [AgentLabel.READY.value, AgentLabel.PAUSED.value]
        calls_before = len(backend.calls)
        paused_out = handle_task(
            TaskEnvelope(
                task_type=TaskType.CODE_AND_TEST,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=10,
                delivery_id="d-paused",
                actor="waustinlynn",
            ),
            ctx,
        )
        assert paused_out.status == "skipped"
        assert len(backend.calls) == calls_before

    def test_protected_path_violation_escalates_gracefully_to_needs_human(
        self,
        tmp_path: Path,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
    ) -> None:
        bare = _create_bare_repo(tmp_path)
        server = _MockGitHubServer()
        store = InMemoryRunStateStore()

        def evil_agent(_p: str, ws_dir: Path, _ro: bool) -> AgentTurnResult:
            wf = ws_dir / ".github" / "workflows" / "ci.yml"
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text("# tampered\n", encoding="utf-8")
            return AgentTurnResult(
                text="done",
                structured_output={
                    "summary": "Edited workflow",
                    "files_changed": [".github/workflows/ci.yml"],
                    "tests_added_or_updated": [],
                    "verify_results": [],
                    "commit_message": "chore: edit workflow",
                    "pr_title": "chore: edit workflow",
                    "pr_body": "Closes #10",
                    "blocked_reason": None,
                },
            )

        ctx = WorkerContext(
            github_clients=_build_clients(app_keys, server),
            agent_backend=_ScriptedBackend(evil_agent),
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            state_store=store,
            clone_url_override=str(bare),
        )

        outcome = handle_task(
            TaskEnvelope(
                task_type=TaskType.CODE_AND_TEST,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=10,
                delivery_id="d-evil",
                actor="waustinlynn",
            ),
            ctx,
        )
        assert outcome.status == "needs_human"
        assert "ProtectedPathViolationError" in outcome.summary
        assert server.issue_labels[10] == [AgentLabel.NEEDS_HUMAN.value]
        assert server.created_prs == []

    @pytest.mark.parametrize(
        ("verdict", "expected_label", "expected_outcome_status", "expected_run_status"),
        [
            ("ready", AgentLabel.READY, "completed", RunStatus.QUEUED),
            ("plan", AgentLabel.PLAN, "completed", RunStatus.QUEUED),
            ("needs_human", AgentLabel.NEEDS_HUMAN, "needs_human", RunStatus.NEEDS_HUMAN),
        ],
    )
    def test_triage_routes_issue_by_verdict(
        self,
        tmp_path: Path,
        app_keys: dict[AppIdentity, GitHubAppCredentials],
        verdict: str,
        expected_label: AgentLabel,
        expected_outcome_status: str,
        expected_run_status: RunStatus,
    ) -> None:
        bare = _create_bare_repo(tmp_path)
        server = _MockGitHubServer()
        store = InMemoryRunStateStore()

        payload: dict[str, Any] = {
            "verdict": verdict,
            "summary": f"Triaged as {verdict}.",
            "rationale": f"Verified against app.py -> {verdict}.",
            "affected_paths": ["app.py"],
            "estimated_minutes": 6 if verdict != "needs_human" else None,
            "missing_details": ["Provide stack trace"] if verdict == "needs_human" else [],
        }
        backend = _ScriptedBackend(
            lambda _p, _d, _ro: AgentTurnResult(
                text="triaged",
                conversation_id=f"conv-triage-{verdict}",
                structured_output=payload,
            )
        )
        ctx = WorkerContext(
            github_clients=_build_clients(app_keys, server),
            agent_backend=backend,
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(base_dir=tmp_path / "ws"),
            state_store=store,
            clone_url_override=str(bare),
        )

        outcome = handle_task(
            TaskEnvelope(
                task_type=TaskType.TRIAGE,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=40,
                delivery_id=f"d-triage-{verdict}",
                actor="waustinlynn",
            ),
            ctx,
        )

        assert outcome.status == expected_outcome_status
        assert outcome.task_type is TaskType.TRIAGE
        assert backend.calls[-1]["read_only"] is True
        assert server.issue_labels[40] == [expected_label.value]
        assert len(server.posted_comments) == 1
        comment_issue, comment_body = server.posted_comments[0]
        assert comment_issue == 40
        assert f"Verified against app.py -> {verdict}." in comment_body
        if verdict == "needs_human":
            assert "Provide stack trace" in comment_body
        saved = store.get("waustinlynn/demo", 40)
        assert saved is not None
        assert saved.status is expected_run_status
