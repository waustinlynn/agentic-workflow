"""Shared task execution core called identically by Phase 1 local runner and Phase 2 Lambda."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from reviewer.handler import run_review_task
from shared.github_client import GitHubAPIError, GitHubClientPair
from shared.labels import AgentLabel
from shared.models import (
    AddressReviewResult,
    CodeTaskResult,
    FixCIResult,
    PlanResult,
    RunState,
    RunStatus,
    TaskEnvelope,
    TaskType,
)
from worker.agent_backend import AgentBackend
from worker.prompts import PromptAssembler, PromptContext
from worker.state_store import RunStateStore
from worker.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """Normalized result returned by ``handle_task`` for logging and SQS deletion control."""

    status: Literal["completed", "skipped", "needs_human"]
    task_type: TaskType
    repo_full_name: str
    issue_or_pr_number: int
    summary: str
    pr_number: int | None = None
    branch: str | None = None
    conversation_id: str | None = None


@dataclass(slots=True)
class WorkerContext:
    """Dependency-injected collaborators for ``handle_task``."""

    github_clients: GitHubClientPair
    agent_backend: AgentBackend
    prompt_assembler: PromptAssembler
    workspace_manager: WorkspaceManager
    state_store: RunStateStore
    clone_url_override: str | None = None
    create_plan_sub_issues: bool = True


def _load_or_init_state(
    state_store: RunStateStore,
    envelope: TaskEnvelope,
) -> RunState:
    existing = state_store.get(envelope.repo_full_name, envelope.issue_or_pr_number)
    if existing is not None:
        return existing
    return RunState(
        repo_full_name=envelope.repo_full_name,
        issue_or_pr_number=envelope.issue_or_pr_number,
        branch=envelope.branch,
    )


def _save_state(
    state_store: RunStateStore,
    state: RunState,
    *,
    status: RunStatus,
    delivery_id: str,
    branch: str | None = None,
    pr_number: int | None = None,
    conversation_id: str | None = None,
    last_error: str | None = None,
) -> None:
    state.status = status
    state.last_delivery_id = delivery_id
    if branch is not None:
        state.branch = branch
    if pr_number is not None:
        state.pr_number = pr_number
    if conversation_id is not None:
        state.conversation_id = conversation_id
    state.last_error = last_error
    state.updated_at = datetime.now(UTC)
    state_store.put(state)


def _is_paused(ctx: WorkerContext, repo: str, item_number: int) -> bool:
    labels = ctx.github_clients.coder.list_labels(repo, item_number)
    return any(lbl.lower() == AgentLabel.PAUSED.value for lbl in labels)


def _escalate_needs_human(
    ctx: WorkerContext,
    envelope: TaskEnvelope,
    state: RunState,
    *,
    reason: str,
) -> TaskOutcome:
    repo = envelope.repo_full_name
    item_number = envelope.issue_or_pr_number
    coder = ctx.github_clients.coder

    coder.swap_agent_labels(
        repo,
        item_number,
        add=[AgentLabel.NEEDS_HUMAN],
        clear_other_agent_labels=True,
    )
    coder.post_comment(
        repo,
        item_number,
        f"### Human Intervention Needed (`agent:needs-human`)\n\n"
        f"**Task:** `{envelope.task_type.value}`\n\n"
        f"{reason}",
    )
    _save_state(
        ctx.state_store,
        state,
        status=RunStatus.NEEDS_HUMAN,
        delivery_id=envelope.delivery_id,
        last_error=reason[:1000],
    )
    return TaskOutcome(
        status="needs_human",
        task_type=envelope.task_type,
        repo_full_name=repo,
        issue_or_pr_number=item_number,
        summary=reason,
        pr_number=state.pr_number,
        branch=state.branch,
        conversation_id=state.conversation_id,
    )


def _format_comments(raw_comments: list[dict[str, Any]]) -> list[str]:
    formatted: list[str] = []
    for comment in raw_comments:
        user = comment.get("user") or {}
        author = str(user.get("login") or "unknown")
        body = str(comment.get("body") or "").strip()
        if body:
            formatted.append(f"@{author}:\n{body}")
    return formatted


def _format_review_feedback(
    reviews: list[dict[str, Any]],
    inline_comments: list[dict[str, Any]],
) -> str:
    sections: list[str] = []
    for rev in reviews:
        if str(rev.get("state") or "").upper() != "CHANGES_REQUESTED":
            continue
        user = rev.get("user") or {}
        author = str(user.get("login") or "reviewer")
        body = str(rev.get("body") or "").strip()
        sections.append(f"Review by @{author} (CHANGES_REQUESTED):\n{body}")

    for comment in inline_comments:
        user = comment.get("user") or {}
        author = str(user.get("login") or "reviewer")
        path = str(comment.get("path") or "")
        line = comment.get("line") or comment.get("original_line") or "?"
        body = str(comment.get("body") or "").strip()
        sections.append(f"Inline comment by @{author} on `{path}:{line}`:\n{body}")

    return "\n\n---\n\n".join(sections)


def _handle_code_and_test(
    envelope: TaskEnvelope,
    ctx: WorkerContext,
    state: RunState,
) -> TaskOutcome:
    repo = envelope.repo_full_name
    issue_num = envelope.issue_or_pr_number
    coder = ctx.github_clients.coder

    coder.swap_agent_labels(
        repo,
        issue_num,
        add=[AgentLabel.IN_PROGRESS],
        clear_other_agent_labels=True,
    )
    state.attempt_count += 1
    _save_state(ctx.state_store, state, status=RunStatus.CODING, delivery_id=envelope.delivery_id)

    issue = coder.get_issue(repo, issue_num)
    comments = _format_comments(coder.list_issue_comments(repo, issue_num))
    token = coder.auth.get_installation_token(repo) if ctx.clone_url_override is None else None

    with ctx.workspace_manager.checkout(
        repo,
        branch=envelope.branch,
        issue_number=issue_num,
        token=token,
        clone_url=ctx.clone_url_override,
    ) as ws:
        cfg = ws.repo_config
        ws.run_setup_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))

        prompt = ctx.prompt_assembler.render(
            PromptContext(
                task_type=TaskType.CODE_AND_TEST,
                repo_full_name=repo,
                issue_or_pr_number=issue_num,
                repo_config=cfg,
                repo_root=ws.repo_dir,
                issue_title=str(issue.get("title") or ""),
                issue_body=str(issue.get("body") or ""),
                comments=comments,
            )
        )
        turn = ctx.agent_backend.run_turn(
            prompt,
            ws.repo_dir,
            schema=ctx.prompt_assembler.get_schema_path(TaskType.CODE_AND_TEST),
            conversation_id=state.conversation_id,
            timeout=float(cfg.execution.timeout_minutes * 60),
            read_only=False,
        )
        if turn.structured_output is None:
            raise ValueError("Coding agent did not return structured CodeTaskResult JSON")
        result = CodeTaskResult.model_validate(turn.structured_output)

        if result.blocked_reason:
            return _escalate_needs_human(ctx, envelope, state, reason=result.blocked_reason)

        ws.run_verify_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))
        pushed_sha = ws.commit_and_push(result.commit_message)
        if pushed_sha is None:
            return _escalate_needs_human(
                ctx,
                envelope,
                state,
                reason="Agent finished without producing any file changes to commit.",
            )

        owner = repo.split("/")[0]
        existing_prs = coder.list_pull_requests(repo, head=f"{owner}:{ws.branch}", state="open")
        if existing_prs:
            pr_number = int(existing_prs[0]["number"])
        else:
            pr_body = result.pr_body
            if f"#{issue_num}" not in pr_body:
                pr_body = f"{pr_body}\n\nCloses #{issue_num}"
            created_pr = coder.create_pull_request(
                repo,
                title=result.pr_title,
                body=pr_body,
                head=ws.branch,
                base=cfg.git.default_branch,
            )
            pr_number = int(created_pr["number"])

        if cfg.review.auto_merge:
            try:
                coder.enable_auto_merge(repo, pr_number, merge_method=cfg.review.merge_method)
            except GitHubAPIError as exc:
                logger.warning("Could not enable auto-merge on %s#%d: %s", repo, pr_number, exc)

        coder.swap_agent_labels(
            repo,
            issue_num,
            add=[AgentLabel.IN_REVIEW],
            clear_other_agent_labels=True,
        )
        coder.swap_agent_labels(
            repo,
            pr_number,
            add=[AgentLabel.IN_REVIEW],
            clear_other_agent_labels=True,
        )
        _save_state(
            ctx.state_store,
            state,
            status=RunStatus.IN_REVIEW,
            delivery_id=envelope.delivery_id,
            branch=ws.branch,
            pr_number=pr_number,
            conversation_id=turn.conversation_id,
        )
        return TaskOutcome(
            status="completed",
            task_type=TaskType.CODE_AND_TEST,
            repo_full_name=repo,
            issue_or_pr_number=issue_num,
            summary=result.summary,
            pr_number=pr_number,
            branch=ws.branch,
            conversation_id=turn.conversation_id,
        )


def _handle_address_review(
    envelope: TaskEnvelope,
    ctx: WorkerContext,
    state: RunState,
) -> TaskOutcome:
    repo = envelope.repo_full_name
    pr_number = envelope.issue_or_pr_number
    coder = ctx.github_clients.coder

    pr = coder.get_pull_request(repo, pr_number)
    head = pr.get("head") or {}
    branch = str(head.get("ref") or envelope.branch or "")
    if not branch:
        raise ValueError(f"Pull request {repo}#{pr_number} is missing head.ref")

    token = coder.auth.get_installation_token(repo) if ctx.clone_url_override is None else None
    with ctx.workspace_manager.checkout(
        repo,
        branch=branch,
        token=token,
        clone_url=ctx.clone_url_override,
    ) as ws:
        cfg = ws.repo_config

        # Circuit breaker: halt loops once attempt_count reaches max_review_rounds.
        if state.attempt_count >= cfg.execution.max_review_rounds:
            return _escalate_needs_human(
                ctx,
                envelope,
                state,
                reason=(
                    f"Reached max review iteration limit ({cfg.execution.max_review_rounds} "
                    f"attempts) on PR #{pr_number}. Halting automated loop for human review."
                ),
            )

        coder.swap_agent_labels(
            repo,
            pr_number,
            add=[AgentLabel.IN_PROGRESS],
            clear_other_agent_labels=True,
        )
        state.attempt_count += 1
        _save_state(
            ctx.state_store,
            state,
            status=RunStatus.CODING,
            delivery_id=envelope.delivery_id,
            branch=branch,
            pr_number=pr_number,
        )

        ws.run_setup_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))

        reviews = coder.list_pull_request_reviews(repo, pr_number)
        inline_comments = coder.list_pull_request_review_comments(repo, pr_number)
        issue_comments = _format_comments(coder.list_issue_comments(repo, pr_number))
        pr_diff = coder.get_pull_request_diff(repo, pr_number)

        prompt = ctx.prompt_assembler.render(
            PromptContext(
                task_type=TaskType.ADDRESS_REVIEW,
                repo_full_name=repo,
                issue_or_pr_number=pr_number,
                repo_config=cfg,
                repo_root=ws.repo_dir,
                issue_title=str(pr.get("title") or ""),
                issue_body=str(pr.get("body") or ""),
                comments=issue_comments,
                pr_diff=pr_diff,
                review_feedback=_format_review_feedback(reviews, inline_comments),
            )
        )
        turn = ctx.agent_backend.run_turn(
            prompt,
            ws.repo_dir,
            schema=ctx.prompt_assembler.get_schema_path(TaskType.ADDRESS_REVIEW),
            conversation_id=state.conversation_id,
            timeout=float(cfg.execution.timeout_minutes * 60),
            read_only=False,
        )
        if turn.structured_output is None:
            raise ValueError("Agent did not return structured AddressReviewResult JSON")
        result = AddressReviewResult.model_validate(turn.structured_output)

        if result.blocked_reason:
            return _escalate_needs_human(ctx, envelope, state, reason=result.blocked_reason)

        ws.run_verify_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))
        ws.commit_and_push(result.commit_message)

        addressed_bullets = "\n".join(f"- {item}" for item in result.addressed_comments)
        comment_body = f"### Review Feedback Addressed\n\n{result.summary}"
        if addressed_bullets:
            comment_body = f"{comment_body}\n\n{addressed_bullets}"
        coder.post_comment(repo, pr_number, comment_body)

        coder.swap_agent_labels(
            repo,
            pr_number,
            add=[AgentLabel.IN_REVIEW],
            clear_other_agent_labels=True,
        )
        _save_state(
            ctx.state_store,
            state,
            status=RunStatus.IN_REVIEW,
            delivery_id=envelope.delivery_id,
            branch=branch,
            pr_number=pr_number,
            conversation_id=turn.conversation_id,
        )
        return TaskOutcome(
            status="completed",
            task_type=TaskType.ADDRESS_REVIEW,
            repo_full_name=repo,
            issue_or_pr_number=pr_number,
            summary=result.summary,
            pr_number=pr_number,
            branch=branch,
            conversation_id=turn.conversation_id,
        )


def _handle_fix_ci(
    envelope: TaskEnvelope,
    ctx: WorkerContext,
    state: RunState,
) -> TaskOutcome:
    repo = envelope.repo_full_name
    pr_number = envelope.issue_or_pr_number
    coder = ctx.github_clients.coder

    pr = coder.get_pull_request(repo, pr_number)
    head = pr.get("head") or {}
    branch = str(head.get("ref") or envelope.branch or "")
    head_sha = str(head.get("sha") or envelope.payload.get("head_sha") or "")
    if not branch:
        raise ValueError(f"Pull request {repo}#{pr_number} is missing head.ref")

    token = coder.auth.get_installation_token(repo) if ctx.clone_url_override is None else None
    with ctx.workspace_manager.checkout(
        repo,
        branch=branch,
        token=token,
        clone_url=ctx.clone_url_override,
    ) as ws:
        cfg = ws.repo_config
        if state.attempt_count >= cfg.execution.max_review_rounds:
            return _escalate_needs_human(
                ctx,
                envelope,
                state,
                reason=(
                    f"Reached max automated retry limit ({cfg.execution.max_review_rounds}) "
                    f"while fixing CI on PR #{pr_number}."
                ),
            )

        coder.swap_agent_labels(
            repo,
            pr_number,
            add=[AgentLabel.IN_PROGRESS],
            clear_other_agent_labels=True,
        )
        state.attempt_count += 1
        _save_state(
            ctx.state_store,
            state,
            status=RunStatus.CODING,
            delivery_id=envelope.delivery_id,
            branch=branch,
            pr_number=pr_number,
        )

        ws.run_setup_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))
        ci_logs = coder.get_failed_ci_summary(repo, head_sha) if head_sha else ""
        pr_diff = coder.get_pull_request_diff(repo, pr_number)

        prompt = ctx.prompt_assembler.render(
            PromptContext(
                task_type=TaskType.FIX_CI,
                repo_full_name=repo,
                issue_or_pr_number=pr_number,
                repo_config=cfg,
                repo_root=ws.repo_dir,
                pr_diff=pr_diff,
                ci_logs=ci_logs or "No check-run output text returned by GitHub.",
            )
        )
        turn = ctx.agent_backend.run_turn(
            prompt,
            ws.repo_dir,
            schema=ctx.prompt_assembler.get_schema_path(TaskType.FIX_CI),
            conversation_id=state.conversation_id,
            timeout=float(cfg.execution.timeout_minutes * 60),
            read_only=False,
        )
        if turn.structured_output is None:
            raise ValueError("Agent did not return structured FixCIResult JSON")
        result = FixCIResult.model_validate(turn.structured_output)

        if result.blocked_reason:
            return _escalate_needs_human(ctx, envelope, state, reason=result.blocked_reason)

        ws.run_verify_commands(timeout_per_command=float(cfg.execution.timeout_minutes * 60))
        ws.commit_and_push(result.commit_message)

        coder.post_comment(
            repo,
            pr_number,
            f"### CI Fix Pushed\n\n**Root cause:** {result.root_cause}\n\n{result.summary}",
        )
        coder.swap_agent_labels(
            repo,
            pr_number,
            add=[AgentLabel.IN_REVIEW],
            clear_other_agent_labels=True,
        )
        _save_state(
            ctx.state_store,
            state,
            status=RunStatus.IN_REVIEW,
            delivery_id=envelope.delivery_id,
            branch=branch,
            pr_number=pr_number,
            conversation_id=turn.conversation_id,
        )
        return TaskOutcome(
            status="completed",
            task_type=TaskType.FIX_CI,
            repo_full_name=repo,
            issue_or_pr_number=pr_number,
            summary=result.summary,
            pr_number=pr_number,
            branch=branch,
            conversation_id=turn.conversation_id,
        )


def _handle_plan(
    envelope: TaskEnvelope,
    ctx: WorkerContext,
    state: RunState,
) -> TaskOutcome:
    repo = envelope.repo_full_name
    issue_num = envelope.issue_or_pr_number
    coder = ctx.github_clients.coder

    coder.swap_agent_labels(
        repo,
        issue_num,
        add=[AgentLabel.IN_PROGRESS],
        clear_other_agent_labels=True,
    )
    state.attempt_count += 1
    _save_state(ctx.state_store, state, status=RunStatus.PLANNING, delivery_id=envelope.delivery_id)

    issue = coder.get_issue(repo, issue_num)
    comments = _format_comments(coder.list_issue_comments(repo, issue_num))
    token = coder.auth.get_installation_token(repo) if ctx.clone_url_override is None else None

    with ctx.workspace_manager.checkout(
        repo,
        branch="main",
        token=token,
        clone_url=ctx.clone_url_override,
    ) as ws:
        cfg = ws.repo_config
        prompt = ctx.prompt_assembler.render(
            PromptContext(
                task_type=TaskType.PLAN,
                repo_full_name=repo,
                issue_or_pr_number=issue_num,
                repo_config=cfg,
                repo_root=ws.repo_dir,
                issue_title=str(issue.get("title") or ""),
                issue_body=str(issue.get("body") or ""),
                comments=comments,
            )
        )
        turn = ctx.agent_backend.run_turn(
            prompt,
            ws.repo_dir,
            schema=ctx.prompt_assembler.get_schema_path(TaskType.PLAN),
            conversation_id=state.conversation_id,
            timeout=float(cfg.execution.timeout_minutes * 60),
            read_only=True,
        )
        if turn.structured_output is None:
            raise ValueError("Planning agent did not return structured PlanResult JSON")
        plan = PlanResult.model_validate(turn.structured_output)

    created_links: list[str] = []
    for sub in plan.sub_issues:
        ac_lines = "\n".join(f"- [ ] {ac}" for ac in sub.acceptance_criteria)
        test_lines = "\n".join(f"- {tc}" for tc in sub.test_cases)
        files_lines = "\n".join(f"- `{f}`" for f in sub.files_to_touch)
        sub_body = f"{sub.description}\n\n## Acceptance Criteria\n{ac_lines}"
        if files_lines:
            sub_body += f"\n\n## Files to Touch\n{files_lines}"
        if test_lines:
            sub_body += f"\n\n## Test Cases\n{test_lines}"
        sub_body += f"\n\n*Estimated time: ~{sub.estimated_minutes} min*"

        if ctx.create_plan_sub_issues:
            try:
                child = coder.create_sub_issue(repo, issue_num, title=sub.title, body=sub_body)
                created_links.append(f"- #{child['number']} — {sub.title}")
                continue
            except GitHubAPIError as exc:
                logger.warning(
                    "Sub-issue creation fell back to comment on %s#%d: %s", repo, issue_num, exc
                )
        created_links.append(f"- **{sub.title}** (~{sub.estimated_minutes} min): {sub.description}")

    comment_lines = [
        "### Implementation Plan",
        "",
        plan.summary,
        "",
        "#### Sub-Issues",
        *created_links,
    ]
    if plan.open_questions:
        comment_lines.extend(["", "#### Open Questions", *(f"- {q}" for q in plan.open_questions)])
    coder.post_comment(repo, issue_num, "\n".join(comment_lines))

    coder.swap_agent_labels(repo, issue_num, remove=[AgentLabel.IN_PROGRESS, AgentLabel.PLAN])
    _save_state(
        ctx.state_store,
        state,
        status=RunStatus.QUEUED,
        delivery_id=envelope.delivery_id,
        conversation_id=turn.conversation_id,
    )
    return TaskOutcome(
        status="completed",
        task_type=TaskType.PLAN,
        repo_full_name=repo,
        issue_or_pr_number=issue_num,
        summary=plan.summary,
        conversation_id=turn.conversation_id,
    )


def handle_task(envelope: TaskEnvelope, ctx: WorkerContext) -> TaskOutcome:
    """Shared task entrypoint executed by both Phase 1 local daemon and Phase 2 Lambda workers."""
    repo = envelope.repo_full_name
    item_number = envelope.issue_or_pr_number
    state = _load_or_init_state(ctx.state_store, envelope)

    try:
        if envelope.task_type is TaskType.REVIEW:
            review_outcome = run_review_task(
                envelope,
                github_clients=ctx.github_clients,
                agent_backend=ctx.agent_backend,
                prompt_assembler=ctx.prompt_assembler,
                workspace_manager=ctx.workspace_manager,
                state_store=ctx.state_store,
                clone_url=ctx.clone_url_override,
            )
            return TaskOutcome(
                status=review_outcome.status,
                task_type=TaskType.REVIEW,
                repo_full_name=repo,
                issue_or_pr_number=item_number,
                summary=review_outcome.summary,
                pr_number=item_number,
                branch=review_outcome.branch,
                conversation_id=review_outcome.conversation_id,
            )

        if _is_paused(ctx, repo, item_number):
            _save_state(
                ctx.state_store,
                state,
                status=RunStatus.PAUSED,
                delivery_id=envelope.delivery_id,
            )
            return TaskOutcome(
                status="skipped",
                task_type=envelope.task_type,
                repo_full_name=repo,
                issue_or_pr_number=item_number,
                summary="Item is paused via agent:paused",
                branch=state.branch,
                pr_number=state.pr_number,
            )

        if envelope.task_type is TaskType.CODE_AND_TEST:
            return _handle_code_and_test(envelope, ctx, state)
        if envelope.task_type is TaskType.ADDRESS_REVIEW:
            return _handle_address_review(envelope, ctx, state)
        if envelope.task_type is TaskType.FIX_CI:
            return _handle_fix_ci(envelope, ctx, state)
        if envelope.task_type is TaskType.PLAN:
            return _handle_plan(envelope, ctx, state)

        raise ValueError(f"Unsupported task_type: {envelope.task_type!r}")
    except Exception as exc:
        logger.exception(
            "Task %s failed on %s#%d; escalating to agent:needs-human",
            envelope.task_type.value,
            repo,
            item_number,
        )
        return _escalate_needs_human(
            ctx,
            envelope,
            state,
            reason=f"Execution failed with `{type(exc).__name__}`: {exc}",
        )
