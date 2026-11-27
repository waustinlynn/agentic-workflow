"""Unit tests for Phase 2 AWS Lambda worker handler, soft-timeout guard, and headless auth."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from shared.github_client import GitHubClientPair
from shared.models import (
    ExecutionTarget,
    RunStatus,
    RuntimeKind,
    TaskEnvelope,
    TaskType,
)
from worker.agent_backend import AgentBackend, AgentTurnResult, AgyCliBackend
from worker.handlers import (
    TaskDeadlineExceededError,
    TaskOutcome,
    WorkerContext,
    compute_step_timeout,
    handle_task,
)
from worker.lambda_handler import (
    MIN_SOFT_BUDGET_SECONDS,
    LambdaWorkerConfig,
    LambdaWorkerService,
    SecretsManagerApiKeyLoader,
    _build_headless_agent_backend,
    compute_deadline_monotonic,
    ensure_tmp_cache_dirs,
)
from worker.prompts import PromptAssembler, resolve_default_prompts_dir
from worker.state_store import InMemoryRunStateStore
from worker.workspace import WorkspaceManager


class _FakeSecretsClient:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        self._secrets = dict(secrets)
        self.calls: list[str] = []

    def get_secret_value(self, *, SecretId: str) -> Mapping[str, Any]:
        self.calls.append(SecretId)
        return {"SecretString": self._secrets[SecretId]}


class _FakeLambdaContext:
    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


class _RecordingAgentBackend(AgentBackend):
    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self._output = output or {}
        self.observed_timeouts: list[float] = []

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
        self.observed_timeouts.append(timeout)
        return AgentTurnResult(
            text=json.dumps(self._output),
            structured_output=self._output,
            conversation_id="conv-lambda-1",
        )


class _StubGitHubClient:
    def __init__(self) -> None:
        self.labels_swapped: list[tuple[int, list[str]]] = []
        self.comments: list[tuple[int, str]] = []

    def list_labels(self, repo: str, number: int) -> list[str]:
        return []

    def swap_agent_labels(
        self,
        repo: str,
        number: int,
        *,
        add: list[Any] | None = None,
        remove: list[Any] | None = None,
        clear_other_agent_labels: bool = False,
    ) -> list[str]:
        added = [str(a) for a in (add or [])]
        self.labels_swapped.append((number, added))
        return added

    def post_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        self.comments.append((number, body))
        return {"id": len(self.comments)}

    def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return {"number": number, "title": "Implement widget", "body": "Add a widget"}

    def list_issue_comments(self, repo: str, number: int) -> list[dict[str, Any]]:
        return []

    def create_sub_issue(self, repo: str, parent: int, *, title: str, body: str) -> dict[str, Any]:
        return {"number": 101, "title": title}


def _init_bare_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    work = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", str(origin), str(work)], check=True)
    (work / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=tester",
            "-c",
            "user.email=tester@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=work,
        check=True,
    )
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=work, check=True)
    return origin


class TestSecretsManagerApiKeyLoader:
    def test_loads_and_caches_plain_secret(self) -> None:
        client = _FakeSecretsClient({"arn:secret:key": "  AIza-plain-key  "})
        loader = SecretsManagerApiKeyLoader("arn:secret:key", client=client)

        assert loader.load() == "AIza-plain-key"
        assert loader.load() == "AIza-plain-key"
        assert len(client.calls) == 1

    @pytest.mark.parametrize("field_name", ["api_key", "antigravity_api_key", "gemini_api_key"])
    def test_loads_json_secret_variants(self, field_name: str) -> None:
        client = _FakeSecretsClient({"arn:secret:json": json.dumps({field_name: "secret-123"})})
        loader = SecretsManagerApiKeyLoader("arn:secret:json", client=client)
        assert loader.load() == "secret-123"

    def test_rejects_empty_or_invalid_json_secret(self) -> None:
        empty_loader = SecretsManagerApiKeyLoader(
            "arn:empty",
            client=_FakeSecretsClient({"arn:empty": "   "}),
        )
        with pytest.raises(RuntimeError, match="empty"):
            empty_loader.load()

        bad_json_loader = SecretsManagerApiKeyLoader(
            "arn:bad",
            client=_FakeSecretsClient({"arn:bad": "{not-json"}),
        )
        with pytest.raises(RuntimeError, match="invalid JSON"):
            bad_json_loader.load()

        missing_key_loader = SecretsManagerApiKeyLoader(
            "arn:missing",
            client=_FakeSecretsClient({"arn:missing": '{"other": "val"}'}),
        )
        with pytest.raises(RuntimeError, match="must contain"):
            missing_key_loader.load()

    def test_build_headless_backend_loads_secret_when_env_key_absent(self) -> None:
        client = _FakeSecretsClient({"arn:secret:agy": "headless-key-xyz"})
        backend = _build_headless_agent_backend(
            {
                "AGENT_BACKEND": "agy_cli",
                "ANTIGRAVITY_API_KEY_SECRET_ARN": "arn:secret:agy",
            },
            region="us-east-1",
            secrets_client=client,
        )
        assert isinstance(backend, AgyCliBackend)
        assert backend._api_key == "headless-key-xyz"
        assert client.calls == ["arn:secret:agy"]

    def test_build_headless_backend_prefers_direct_env_key(self) -> None:
        client = _FakeSecretsClient({"arn:secret:agy": "from-sm"})
        backend = _build_headless_agent_backend(
            {
                "AGENT_BACKEND": "agy_cli",
                "ANTIGRAVITY_API_KEY": "direct-env-key",
                "ANTIGRAVITY_API_KEY_SECRET_ARN": "arn:secret:agy",
            },
            region="us-east-1",
            secrets_client=client,
        )
        assert isinstance(backend, AgyCliBackend)
        assert backend._api_key == "direct-env-key"
        assert client.calls == []


class TestSoftTimeoutAndDeadlines:
    def test_compute_deadline_monotonic_respects_context_and_buffer(self) -> None:
        cfg = LambdaWorkerConfig(soft_timeout_buffer_seconds=120.0, default_timeout_seconds=900.0)
        deadline = compute_deadline_monotonic(
            _FakeLambdaContext(remaining_ms=600_000),
            config=cfg,
            clock=lambda: 1000.0,
        )
        assert deadline == pytest.approx(1000.0 + 480.0)

    def test_compute_deadline_monotonic_clamps_minimum_budget(self) -> None:
        cfg = LambdaWorkerConfig(soft_timeout_buffer_seconds=120.0)
        deadline = compute_deadline_monotonic(
            _FakeLambdaContext(remaining_ms=30_000),
            config=cfg,
            clock=lambda: 500.0,
        )
        assert deadline == pytest.approx(500.0 + MIN_SOFT_BUDGET_SECONDS)

    def test_compute_step_timeout_caps_or_raises(self) -> None:
        assert compute_step_timeout(720.0, None) == 720.0
        assert compute_step_timeout(720.0, 1200.0, clock=lambda: 1000.0) == pytest.approx(200.0)
        assert compute_step_timeout(60.0, 1200.0, clock=lambda: 1000.0) == pytest.approx(60.0)

        with pytest.raises(TaskDeadlineExceededError, match="Soft execution deadline reached"):
            compute_step_timeout(60.0, 1000.5, clock=lambda: 1000.0, step_name="verify_commands")

    def test_handle_task_caps_agent_timeout_and_escalates_on_exhausted_deadline(
        self,
        tmp_path: Path,
    ) -> None:
        origin = _init_bare_repo(tmp_path)
        coder_gh = _StubGitHubClient()
        reviewer_gh = _StubGitHubClient()
        clients = GitHubClientPair(coder=coder_gh, reviewer=reviewer_gh)  # type: ignore[arg-type]
        state_store = InMemoryRunStateStore()

        # 1. Capped timeout passes through to agent backend.
        backend = _RecordingAgentBackend(
            {
                "summary": "Planned 1 sub-issue",
                "sub_issues": [
                    {
                        "title": "Sub 1",
                        "description": "Do step 1",
                        "acceptance_criteria": ["Works"],
                        "files_to_touch": ["README.md"],
                        "test_cases": ["Test 1"],
                        "estimated_minutes": 4,
                    }
                ],
                "open_questions": [],
            }
        )
        ctx = WorkerContext(
            github_clients=clients,
            agent_backend=backend,
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(tmp_path / "ws"),
            state_store=state_store,
            clone_url_override=str(origin),
            deadline_monotonic=1150.0,
            monotonic_clock=lambda: 1000.0,
        )
        envelope = TaskEnvelope(
            task_type=TaskType.PLAN,
            repo_full_name="waustinlynn/demo",
            issue_or_pr_number=7,
            delivery_id="deliv-cap-1",
            actor="waustinlynn",
            execution_target=ExecutionTarget.LAMBDA,
            runtime=RuntimeKind.BASE,
        )
        outcome = handle_task(envelope, ctx)
        assert outcome.status == "completed"
        assert backend.observed_timeouts == [pytest.approx(150.0)]

        # 2. Already-exhausted soft deadline escalates cleanly to agent:needs-human.
        expired_ctx = WorkerContext(
            github_clients=clients,
            agent_backend=_RecordingAgentBackend(),
            prompt_assembler=PromptAssembler(),
            workspace_manager=WorkspaceManager(tmp_path / "ws"),
            state_store=state_store,
            clone_url_override=str(origin),
            deadline_monotonic=999.0,
            monotonic_clock=lambda: 1000.0,
        )
        expired_outcome = handle_task(envelope, expired_ctx)
        assert expired_outcome.status == "needs_human"
        assert "Soft execution deadline reached" in expired_outcome.summary
        saved = state_store.get("waustinlynn/demo", 7)
        assert saved is not None and saved.status is RunStatus.NEEDS_HUMAN
        assert any("agent:needs-human" in labels for _, labels in coder_gh.labels_swapped)


class TestLambdaWorkerService:
    def test_processes_valid_sqs_record_and_sweeps_tmp_workspaces(self, tmp_path: Path) -> None:
        ws_root = tmp_path / "agentic-workspaces"
        stale_dir = ws_root / "stale-checkout-abc"
        stale_dir.mkdir(parents=True)
        (stale_dir / "junk.txt").write_text("leftover", encoding="utf-8")

        ws_mgr = WorkspaceManager(ws_root)
        seen: list[tuple[TaskEnvelope, float | None]] = []

        def _fake_ctx_factory(deadline: float) -> WorkerContext:
            return WorkerContext(
                github_clients=None,  # type: ignore[arg-type]
                agent_backend=_RecordingAgentBackend(),
                prompt_assembler=PromptAssembler(),
                workspace_manager=ws_mgr,
                state_store=InMemoryRunStateStore(),
                deadline_monotonic=deadline,
            )

        def _fake_executor(env: TaskEnvelope, ctx: WorkerContext) -> TaskOutcome:
            seen.append((env, ctx.deadline_monotonic))
            return TaskOutcome(
                status="completed",
                task_type=env.task_type,
                repo_full_name=env.repo_full_name,
                issue_or_pr_number=env.issue_or_pr_number,
                summary="ok",
            )

        service = LambdaWorkerService(
            context_factory=_fake_ctx_factory,
            workspace_manager=ws_mgr,
            config=LambdaWorkerConfig(soft_timeout_buffer_seconds=120.0, runtime_name="python"),
            task_executor=_fake_executor,
            clock=lambda: 2000.0,
        )
        envelope = TaskEnvelope(
            task_type=TaskType.CODE_AND_TEST,
            repo_full_name="waustinlynn/demo",
            issue_or_pr_number=12,
            delivery_id="deliv-lambda-1",
            actor="waustinlynn",
            execution_target=ExecutionTarget.LAMBDA,
            runtime=RuntimeKind.PYTHON,
        )
        event = {
            "Records": [
                {
                    "messageId": "msg-1",
                    "body": envelope.model_dump_json(),
                }
            ]
        }
        result = service.handle_sqs_event(event, _FakeLambdaContext(remaining_ms=900_000))
        assert result == {"batchItemFailures": []}
        assert len(seen) == 1
        assert seen[0][0].issue_or_pr_number == 12
        assert seen[0][1] == pytest.approx(2000.0 + 780.0)
        assert not stale_dir.exists()

    def test_reports_batch_item_failures_for_poison_pill_and_unhandled_errors(
        self,
        tmp_path: Path,
    ) -> None:
        ws_mgr = WorkspaceManager(tmp_path / "ws")

        def _ctx_factory(deadline: float) -> WorkerContext:
            return WorkerContext(
                github_clients=None,  # type: ignore[arg-type]
                agent_backend=_RecordingAgentBackend(),
                prompt_assembler=PromptAssembler(),
                workspace_manager=ws_mgr,
                state_store=InMemoryRunStateStore(),
                deadline_monotonic=deadline,
            )

        def _flaky_executor(env: TaskEnvelope, _ctx: WorkerContext) -> TaskOutcome:
            if env.issue_or_pr_number == 99:
                raise RuntimeError("DynamoDB unavailable")
            return TaskOutcome(
                status="completed",
                task_type=env.task_type,
                repo_full_name=env.repo_full_name,
                issue_or_pr_number=env.issue_or_pr_number,
                summary="done",
            )

        service = LambdaWorkerService(
            context_factory=_ctx_factory,
            workspace_manager=ws_mgr,
            task_executor=_flaky_executor,
        )
        good_env = TaskEnvelope(
            task_type=TaskType.REVIEW,
            repo_full_name="waustinlynn/demo",
            issue_or_pr_number=5,
            delivery_id="d-good",
            actor="waustinlynn",
            execution_target=ExecutionTarget.LAMBDA,
            runtime=RuntimeKind.BASE,
        )
        bad_env = good_env.model_copy(update={"issue_or_pr_number": 99, "delivery_id": "d-bad"})
        event = {
            "Records": [
                {"messageId": "msg-malformed", "body": "{not valid json"},
                {"messageId": "msg-good", "body": good_env.model_dump_json()},
                {"messageId": "msg-infra-err", "body": bad_env.model_dump_json()},
            ]
        }
        result = service.handle_sqs_event(event)
        assert result == {
            "batchItemFailures": [
                {"itemIdentifier": "msg-malformed"},
                {"itemIdentifier": "msg-infra-err"},
            ]
        }


def test_resolve_default_prompts_dir_honors_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    custom = tmp_path / "lambda-prompts"
    custom.mkdir()
    monkeypatch.setenv("AGENTIC_PROMPTS_DIR", str(custom))
    assert resolve_default_prompts_dir() == custom


def test_ensure_tmp_cache_dirs_creates_configured_directories(tmp_path: Path) -> None:
    tf_cache = tmp_path / ".terraform.d" / "plugin-cache"
    uv_cache = tmp_path / ".cache" / "uv"
    created = ensure_tmp_cache_dirs(
        {
            "TF_PLUGIN_CACHE_DIR": str(tf_cache),
            "UV_CACHE_DIR": str(uv_cache),
            "NPM_CONFIG_CACHE": "",
        }
    )
    assert tf_cache.is_dir()
    assert uv_cache.is_dir()
    assert set(created) == {tf_cache, uv_cache}
