"""Unit tests for shared Pydantic models and ``.agentic/config.yaml`` validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from shared.config import (
    ConfigError,
    load_repo_config,
    load_repo_config_or_default,
    parse_repo_config,
)
from shared.labels import AGENT_LABEL_SPECS, ALL_AGENT_LABELS, AgentLabel, is_agent_label
from shared.models import (
    DEFAULT_PROTECTED_PATHS,
    ExecutionTarget,
    MergeMethod,
    RepoConfig,
    ReviewComment,
    ReviewCommentSeverity,
    ReviewResult,
    ReviewVerdict,
    RunState,
    RunStatus,
    RuntimeKind,
    StackType,
    TaskEnvelope,
    TaskType,
    TriageResult,
    TriageVerdict,
)


class TestTaskEnvelope:
    def test_valid_minimal_task_envelope(self) -> None:
        task = TaskEnvelope(
            task_type=TaskType.CODE_AND_TEST,
            repo_full_name="waustinlynn/agentic-workflow",
            issue_or_pr_number=42,
            delivery_id="deliv-123",
            actor="waustinlynn",
        )
        assert task.execution_target is ExecutionTarget.LOCAL
        assert task.runtime is RuntimeKind.BASE
        assert task.branch is None
        assert task.payload == {}

    @pytest.mark.parametrize(
        "bad_repo",
        ["just-a-name", "a/b/c", "../evil", "owner/repo with spaces", ""],
    )
    def test_rejects_invalid_repo_full_name(self, bad_repo: str) -> None:
        with pytest.raises(ValidationError, match="owner/repo"):
            TaskEnvelope(
                task_type=TaskType.PLAN,
                repo_full_name=bad_repo,
                issue_or_pr_number=1,
                delivery_id="d1",
                actor="waustinlynn",
            )

    @pytest.mark.parametrize(
        "bad_branch", ["", "-leading-dash", "a..b", "feat/has space", "a.lock"]
    )
    def test_rejects_invalid_branch(self, bad_branch: str) -> None:
        with pytest.raises(ValidationError, match="branch"):
            TaskEnvelope(
                task_type=TaskType.REVIEW,
                repo_full_name="waustinlynn/demo",
                issue_or_pr_number=5,
                branch=bad_branch,
                delivery_id="d2",
                actor="agentic-coder[bot]",
            )


class TestRunState:
    def test_dynamodb_round_trip(self) -> None:
        state = RunState(
            repo_full_name="waustinlynn/demo",
            issue_or_pr_number=17,
            status=RunStatus.IN_REVIEW,
            branch="agent/issue-17",
            pr_number=21,
            attempt_count=2,
            review_round=1,
            conversation_id="conv-abc",
            last_delivery_id="deliv-999",
        )
        assert state.pk == "REPO#waustinlynn/demo"
        assert state.sk == "ITEM#17"

        item = state.to_dynamodb_item()
        restored = RunState.from_dynamodb_item(item)
        assert restored == state


class TestReviewResult:
    def test_valid_request_changes_with_blocker(self) -> None:
        result = ReviewResult(
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="Missing HMAC verification on webhook handler.",
            comments=[
                ReviewComment(
                    path="src/ingest/handler.py",
                    line=42,
                    severity=ReviewCommentSeverity.BLOCKER,
                    body="Verify X-Hub-Signature-256 using hmac.compare_digest.",
                )
            ],
        )
        assert result.verdict is ReviewVerdict.REQUEST_CHANGES
        assert len(result.comments) == 1

    def test_rejects_approve_with_blocker_comment(self) -> None:
        with pytest.raises(ValidationError, match="blocker"):
            ReviewResult(
                verdict=ReviewVerdict.APPROVE,
                summary="Looks good except for one blocker.",
                comments=[
                    ReviewComment(
                        path="src/app.py",
                        line=10,
                        severity=ReviewCommentSeverity.BLOCKER,
                        body="SQL injection here.",
                    )
                ],
            )

    def test_rejects_parent_traversal_in_comment_path(self) -> None:
        with pytest.raises(ValidationError, match="relative file path"):
            ReviewComment(path="../secrets.txt", line=1, body="oops")


class TestRepoConfig:
    @pytest.mark.parametrize(
        ("yaml_text", "expected_stack", "expected_runtime", "expected_target"),
        [
            (
                """
version: 1
stack: frontend
git:
  default_branch: main
  branch_prefix: agent/
  commit_style: conventional
execution:
  target: lambda
  runtime: node
  max_review_rounds: 3
  timeout_minutes: 10
  setup_commands: ["npm ci"]
  verify_commands: ["npm run lint", "npm test"]
review:
  wait_for_ci: true
  auto_merge: true
  merge_method: squash
  extra_checklist: ["Check accessibility labels on new form inputs"]
safety:
  protected_paths: ["src/auth/**"]
""",
                StackType.FRONTEND,
                RuntimeKind.NODE,
                ExecutionTarget.LAMBDA,
            ),
            (
                """
version: 1
stack: backend
execution:
  target: local
  runtime: python
  verify_commands: ["uv run ruff check .", "uv run pytest"]
review:
  merge_method: rebase
""",
                StackType.BACKEND,
                RuntimeKind.PYTHON,
                ExecutionTarget.LOCAL,
            ),
            (
                """
version: 1
stack: mobile
execution:
  target: lambda
  runtime: ci-verified
  timeout_minutes: 12
review:
  wait_for_ci: true
  auto_merge: false
""",
                StackType.MOBILE,
                RuntimeKind.CI_VERIFIED,
                ExecutionTarget.LAMBDA,
            ),
            (
                """
version: 1
stack: infra
execution:
  target: lambda
  runtime: infra
  working_directory: terraform
  verify_commands: ["terraform fmt -check", "terraform validate"]
safety:
  protected_paths: ["prod/**"]
""",
                StackType.INFRA,
                RuntimeKind.INFRA,
                ExecutionTarget.LAMBDA,
            ),
        ],
    )
    def test_valid_configs_across_stacks(
        self,
        yaml_text: str,
        expected_stack: StackType,
        expected_runtime: RuntimeKind,
        expected_target: ExecutionTarget,
    ) -> None:
        cfg = parse_repo_config(yaml_text)
        assert cfg.stack is expected_stack
        assert cfg.execution.runtime is expected_runtime
        assert cfg.execution.target is expected_target
        for baseline in DEFAULT_PROTECTED_PATHS:
            assert baseline in cfg.safety.effective_protected_paths

    def test_empty_yaml_uses_sensible_defaults(self) -> None:
        cfg = parse_repo_config("")
        assert cfg == RepoConfig()
        assert cfg.git.default_branch == "main"
        assert cfg.review.merge_method is MergeMethod.SQUASH
        assert cfg.safety.allow_external_forks is False

    def test_rejects_unknown_field_typo(self) -> None:
        with pytest.raises(ConfigError, match="verfy_commands"):
            parse_repo_config("execution:\n  verfy_commands: ['pytest']\n")

    def test_rejects_lambda_timeout_exceeding_ceiling(self) -> None:
        with pytest.raises(ConfigError, match="timeout_minutes"):
            parse_repo_config("execution:\n  target: lambda\n  timeout_minutes: 20\n")

    def test_rejects_external_forks_enabled(self) -> None:
        with pytest.raises(ConfigError, match="allow_external_forks"):
            parse_repo_config("safety:\n  allow_external_forks: true\n")

    def test_rejects_unsupported_version(self) -> None:
        with pytest.raises(ConfigError, match="version"):
            parse_repo_config("version: 2\n")

    def test_rejects_malformed_yaml_and_non_mapping(self) -> None:
        with pytest.raises(ConfigError, match="Malformed YAML"):
            parse_repo_config("execution: [unclosed")
        with pytest.raises(ConfigError, match="YAML mapping"):
            parse_repo_config("- just\n- a\n- list\n")

    def test_load_repo_config_from_disk_and_fallback(self, tmp_path: Path) -> None:
        assert load_repo_config_or_default(tmp_path) == RepoConfig()

        cfg_file = tmp_path / ".agentic" / "config.yaml"
        cfg_file.parent.mkdir(parents=True)
        cfg_file.write_text("stack: frontend\n", encoding="utf-8")

        loaded = load_repo_config(cfg_file)
        assert loaded.stack is StackType.FRONTEND
        assert load_repo_config_or_default(tmp_path).stack is StackType.FRONTEND

        with pytest.raises(ConfigError, match="not found"):
            load_repo_config(tmp_path / "missing.yaml")


class TestLabels:
    def test_label_specs_cover_all_enum_values(self) -> None:
        spec_names = {spec.name.value for spec in AGENT_LABEL_SPECS}
        assert spec_names == ALL_AGENT_LABELS
        assert AgentLabel.TRIAGE.value in ALL_AGENT_LABELS
        assert AgentLabel.READY.value in ALL_AGENT_LABELS
        assert is_agent_label("agent:triage") is True
        assert is_agent_label("agent:ready") is True
        assert is_agent_label("bug") is False


class TestTriageResult:
    def test_valid_triage_verdicts(self) -> None:
        ready = TriageResult(
            verdict=TriageVerdict.READY,
            summary="Clear bug in health endpoint.",
            rationale="Confirmed missing 503 check in src/api/health.py.",
            affected_paths=["src/api/health.py"],
            estimated_minutes=5,
        )
        assert ready.verdict is TriageVerdict.READY
        assert ready.missing_details == []

        needs_human = TriageResult(
            verdict=TriageVerdict.NEEDS_HUMAN,
            summary="Cannot reproduce without error logs.",
            rationale="Issue lacks stack trace and affected endpoint.",
            missing_details=["Attach failing command output", "Specify target environment"],
        )
        assert needs_human.verdict is TriageVerdict.NEEDS_HUMAN
        assert len(needs_human.missing_details) == 2

    def test_rejects_blank_summary_or_rationale(self) -> None:
        with pytest.raises(ValidationError, match="blank"):
            TriageResult(
                verdict=TriageVerdict.PLAN,
                summary="   ",
                rationale="Large refactor.",
            )
        with pytest.raises(ValidationError, match="blank"):
            TriageResult(
                verdict=TriageVerdict.NEEDS_HUMAN,
                summary="Needs clarification",
                rationale="   ",
            )
