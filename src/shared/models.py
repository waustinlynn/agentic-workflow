"""Shared Pydantic v2 schemas for ingest, local runner, reviewer, and Lambda workers."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INVALID_BRANCH_CHARS_RE = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]")

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    ".github/workflows/**",
    ".agentic/**",
    "CODEOWNERS",
)

LAMBDA_MAX_SAFE_TIMEOUT_MINUTES = 14


def _validate_repo_full_name(value: str) -> str:
    cleaned = value.strip()
    if not REPO_FULL_NAME_RE.match(cleaned) or ".." in cleaned:
        raise ValueError(
            f"repo_full_name must be in 'owner/repo' format without traversal; got {value!r}"
        )
    return cleaned


def _validate_git_branch(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("branch name cannot be empty or whitespace")
    if (
        cleaned.startswith("-")
        or cleaned.startswith("/")
        or cleaned.endswith("/")
        or cleaned.endswith(".lock")
        or ".." in cleaned
        or "//" in cleaned
        or INVALID_BRANCH_CHARS_RE.search(cleaned)
    ):
        raise ValueError(f"invalid git branch name: {value!r}")
    return cleaned


class TaskType(StrEnum):
    """Supported task kinds dispatched over SQS FIFO."""

    PLAN = "plan"
    CODE_AND_TEST = "code_and_test"
    REVIEW = "review"
    ADDRESS_REVIEW = "address_review"
    FIX_CI = "fix_ci"


class ExecutionTarget(StrEnum):
    """Where a worker task executes."""

    LOCAL = "local"
    LAMBDA = "lambda"


class RuntimeKind(StrEnum):
    """Worker container/toolchain runtime profile."""

    BASE = "base"
    PYTHON = "python"
    NODE = "node"
    DOTNET = "dotnet"
    RUST = "rust"
    GO = "go"
    INFRA = "infra"
    CI_VERIFIED = "ci-verified"


class StackType(StrEnum):
    """High-level stack classification for an onboarded repository."""

    FRONTEND = "frontend"
    BACKEND = "backend"
    FULLSTACK = "fullstack"
    MOBILE = "mobile"
    INFRA = "infra"
    LIBRARY = "library"
    MONOREPO = "monorepo"
    OTHER = "other"


class CommitStyle(StrEnum):
    """Commit message convention for an onboarded repository."""

    CONVENTIONAL = "conventional"
    FREEFORM = "freeform"


class MergeMethod(StrEnum):
    """GitHub pull request merge strategy."""

    SQUASH = "squash"
    MERGE = "merge"
    REBASE = "rebase"


class RunStatus(StrEnum):
    """Lifecycle status tracked per (repo, issue_or_pr) in DynamoDB."""

    QUEUED = "queued"
    PLANNING = "planning"
    CODING = "coding"
    IN_REVIEW = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    MERGED = "merged"
    NEEDS_HUMAN = "needs_human"
    PAUSED = "paused"
    FAILED = "failed"


class ReviewVerdict(StrEnum):
    """Formal GitHub pull request review decision."""

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"


class ReviewCommentSeverity(StrEnum):
    """Severity tag on an inline review comment."""

    BLOCKER = "blocker"
    WARNING = "warning"
    NIT = "nit"


class TaskEnvelope(BaseModel):
    """Transport-agnostic task payload enqueued by ingest and consumed by workers."""

    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    repo_full_name: str
    issue_or_pr_number: int = Field(gt=0)
    branch: str | None = None
    delivery_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    execution_target: ExecutionTarget = ExecutionTarget.LOCAL
    runtime: RuntimeKind = RuntimeKind.BASE
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("repo_full_name")
    @classmethod
    def validate_repo_full_name(cls, value: str) -> str:
        return _validate_repo_full_name(value)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        return _validate_git_branch(value)

    @field_validator("delivery_id", "actor")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("value must not be blank")
        return cleaned


class GitConfig(BaseModel):
    """Git branch and commit settings inside ``.agentic/config.yaml``."""

    model_config = ConfigDict(extra="forbid")

    default_branch: str = "main"
    branch_prefix: str = "agent/"
    commit_style: CommitStyle = CommitStyle.CONVENTIONAL

    @field_validator("default_branch")
    @classmethod
    def validate_default_branch(cls, value: str) -> str:
        validated = _validate_git_branch(value)
        assert validated is not None
        return validated

    @field_validator("branch_prefix")
    @classmethod
    def validate_branch_prefix(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.startswith("-") or ".." in cleaned or " " in cleaned:
            raise ValueError(f"invalid branch_prefix: {value!r}")
        return cleaned


class ExecutionConfig(BaseModel):
    """Worker execution budget, runtime, and verification commands."""

    model_config = ConfigDict(extra="forbid")

    target: ExecutionTarget = ExecutionTarget.LOCAL
    runtime: RuntimeKind = RuntimeKind.BASE
    max_review_rounds: int = Field(default=3, ge=1, le=10)
    timeout_minutes: int = Field(default=12, ge=1, le=60)
    working_directory: str = "."
    setup_commands: list[str] = Field(default_factory=list)
    verify_commands: list[str] = Field(default_factory=list)

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
            raise ValueError(f"working_directory must be a relative path inside repo: {value!r}")
        return cleaned

    @field_validator("setup_commands", "verify_commands")
    @classmethod
    def validate_commands(cls, commands: list[str]) -> list[str]:
        cleaned: list[str] = []
        for cmd in commands:
            stripped = cmd.strip()
            if not stripped:
                raise ValueError("command entries must not be blank")
            cleaned.append(stripped)
        return cleaned

    @model_validator(mode="after")
    def validate_lambda_timeout_budget(self) -> ExecutionConfig:
        if (
            self.target is ExecutionTarget.LAMBDA
            and self.timeout_minutes > LAMBDA_MAX_SAFE_TIMEOUT_MINUTES
        ):
            raise ValueError(
                f"execution.timeout_minutes ({self.timeout_minutes}) must be <= "
                f"{LAMBDA_MAX_SAFE_TIMEOUT_MINUTES} when execution.target is 'lambda'"
            )
        return self


class ReviewConfig(BaseModel):
    """Automated review and merge behavior for an onboarded repository."""

    model_config = ConfigDict(extra="forbid")

    wait_for_ci: bool = True
    auto_merge: bool = True
    merge_method: MergeMethod = MergeMethod.SQUASH
    extra_checklist: list[str] = Field(default_factory=list)

    @field_validator("extra_checklist")
    @classmethod
    def validate_checklist(cls, items: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in items:
            stripped = item.strip()
            if not stripped:
                raise ValueError("extra_checklist items must not be blank")
            cleaned.append(stripped)
        return cleaned


class SafetyConfig(BaseModel):
    """Repository safety boundaries enforced before committing or pushing."""

    model_config = ConfigDict(extra="forbid")

    protected_paths: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_PATHS))
    allow_external_forks: Literal[False] = False

    @field_validator("protected_paths")
    @classmethod
    def validate_protected_paths(cls, paths: list[str]) -> list[str]:
        cleaned: list[str] = []
        for path in paths:
            stripped = path.strip()
            if not stripped:
                raise ValueError("protected_paths entries must not be blank")
            cleaned.append(stripped)
        return cleaned

    @property
    def effective_protected_paths(self) -> tuple[str, ...]:
        """Always include baseline framework protected paths alongside repo overrides."""
        seen: set[str] = set()
        ordered: list[str] = []
        for pattern in (*DEFAULT_PROTECTED_PATHS, *self.protected_paths):
            if pattern not in seen:
                seen.add(pattern)
                ordered.append(pattern)
        return tuple(ordered)


class RepoConfig(BaseModel):
    """Schema for ``.agentic/config.yaml`` (v1) in any onboarded repository."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    stack: StackType = StackType.BACKEND
    git: GitConfig = Field(default_factory=GitConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


class RunState(BaseModel):
    """DynamoDB-backed workflow state for a single ``(repo, issue_or_pr)``."""

    model_config = ConfigDict(extra="forbid")

    repo_full_name: str
    issue_or_pr_number: int = Field(gt=0)
    status: RunStatus = RunStatus.QUEUED
    branch: str | None = None
    pr_number: int | None = Field(default=None, gt=0)
    attempt_count: int = Field(default=0, ge=0)
    review_round: int = Field(default=0, ge=0)
    conversation_id: str | None = None
    last_delivery_id: str | None = None
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("repo_full_name")
    @classmethod
    def validate_repo_full_name(cls, value: str) -> str:
        return _validate_repo_full_name(value)

    @field_validator("branch")
    @classmethod
    def validate_branch(cls, value: str | None) -> str | None:
        return _validate_git_branch(value)

    @property
    def pk(self) -> str:
        """DynamoDB partition key."""
        return f"REPO#{self.repo_full_name}"

    @property
    def sk(self) -> str:
        """DynamoDB sort key."""
        return f"ITEM#{self.issue_or_pr_number}"

    def to_dynamodb_item(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for a DynamoDB Table ``put_item`` call."""
        item: dict[str, Any] = {
            "pk": self.pk,
            "sk": self.sk,
            "repo_full_name": self.repo_full_name,
            "issue_or_pr_number": self.issue_or_pr_number,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "review_round": self.review_round,
            "updated_at": self.updated_at.isoformat(),
        }
        for optional_key in (
            "branch",
            "pr_number",
            "conversation_id",
            "last_delivery_id",
            "last_error",
        ):
            value = getattr(self, optional_key)
            if value is not None:
                item[optional_key] = value
        return item

    @classmethod
    def from_dynamodb_item(cls, item: dict[str, Any]) -> RunState:
        """Deserialize from a DynamoDB Table ``get_item`` dictionary."""
        data = {k: v for k, v in item.items() if k not in {"pk", "sk"}}
        return cls.model_validate(data)


class ReviewComment(BaseModel):
    """Inline file/line review comment emitted by the Review Agent."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    side: Literal["LEFT", "RIGHT"] = "RIGHT"
    severity: ReviewCommentSeverity = ReviewCommentSeverity.WARNING
    body: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
            raise ValueError(f"comment path must be a relative file path: {value!r}")
        return cleaned

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("comment body must not be blank")
        return cleaned


class ReviewResult(BaseModel):
    """Structured JSON output produced by the Option A Review Agent."""

    model_config = ConfigDict(extra="forbid")

    verdict: ReviewVerdict
    summary: str = Field(min_length=1)
    comments: list[ReviewComment] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("summary must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_verdict_matches_blockers(self) -> ReviewResult:
        has_blocker = any(c.severity is ReviewCommentSeverity.BLOCKER for c in self.comments)
        if self.verdict is ReviewVerdict.APPROVE and has_blocker:
            raise ValueError("ReviewResult cannot have verdict 'APPROVE' with blocker comments")
        return self


class CIStatusSummary(BaseModel):
    """Aggregated GitHub check runs and commit statuses for a git ref."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["pending", "success", "failure"]
    total_checks: int = Field(ge=0)
    failed_checks: list[str] = Field(default_factory=list)
    pending_checks: list[str] = Field(default_factory=list)
