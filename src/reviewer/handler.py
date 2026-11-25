"""Option A Review Agent submitting formal PR reviews under ``agentic-reviewer[bot]``."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from shared.github_client import GitHubClientPair
from shared.labels import AgentLabel
from shared.models import (
    ReviewResult,
    ReviewVerdict,
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

_LINKED_ISSUE_RE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)
_BRANCH_ISSUE_RE = re.compile(r"issue-(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ReviewTaskOutcome:
    """Outcome of executing a single ``review`` task."""

    status: Literal["completed", "skipped"]
    summary: str
    verdict: ReviewVerdict | None = None
    conversation_id: str | None = None
    branch: str | None = None


def _extract_linked_issue_number(pr: dict[str, Any], branch: str) -> int | None:
    body = str(pr.get("body") or "")
    match = _LINKED_ISSUE_RE.search(body)
    if match:
        return int(match.group(1))
    branch_match = _BRANCH_ISSUE_RE.search(branch)
    if branch_match:
        return int(branch_match.group(1))
    return None


def _has_paused_label(labels: list[str]) -> bool:
    return any(lbl.lower() == AgentLabel.PAUSED.value for lbl in labels)


def run_review_task(
    envelope: TaskEnvelope,
    *,
    github_clients: GitHubClientPair,
    agent_backend: AgentBackend,
    prompt_assembler: PromptAssembler,
    workspace_manager: WorkspaceManager,
    state_store: RunStateStore,
    clone_url: str | None = None,
) -> ReviewTaskOutcome:
    """Execute an Option A automated code review as ``agentic-reviewer[bot]``.

    - Reads PR metadata, diff, linked issue context, and CI status via ``reviewer``.
    - Skips cleanly if ``agent:paused`` is set or if ``wait_for_ci`` is true and CI is not green.
    - Submits a formal GitHub PR Review (``APPROVE`` / ``REQUEST_CHANGES``) as reviewer bot.
    - Uses ``agentic-coder[bot]`` to swap ``agent:*`` labels and enable native auto-merge.
    """
    repo = envelope.repo_full_name
    pr_number = envelope.issue_or_pr_number
    reviewer_gh = github_clients.reviewer
    coder_gh = github_clients.coder

    pr = reviewer_gh.get_pull_request(repo, pr_number)
    pr_labels = [str(item["name"]) for item in pr.get("labels", []) if isinstance(item, dict)]
    if _has_paused_label(pr_labels):
        state = state_store.get(repo, pr_number) or RunState(
            repo_full_name=repo,
            issue_or_pr_number=pr_number,
        )
        state.status = RunStatus.PAUSED
        state.last_delivery_id = envelope.delivery_id
        state.updated_at = datetime.now(UTC)
        state_store.put(state)
        return ReviewTaskOutcome(status="skipped", summary="PR is paused via agent:paused")

    head = pr.get("head") or {}
    branch = str(head.get("ref") or envelope.branch or "")
    head_sha = str(head.get("sha") or "")
    if not branch:
        raise ValueError(f"Pull request {repo}#{pr_number} is missing head.ref")

    reviewer_token = reviewer_gh.auth.get_installation_token(repo) if clone_url is None else None

    with workspace_manager.checkout(
        repo,
        branch=branch,
        token=reviewer_token,
        clone_url=clone_url,
    ) as ws:
        repo_config = ws.repo_config

        ci_summary_text = ""
        if head_sha:
            ci = reviewer_gh.get_ci_status(repo, head_sha)
            ci_summary_text = (
                f"CI state: {ci.state} ({ci.total_checks} checks; "
                f"failed={ci.failed_checks}, pending={ci.pending_checks})"
            )
            if repo_config.review.wait_for_ci and ci.state != "success":
                logger.info(
                    "Skipping review for %s#%d because CI is %s",
                    repo,
                    pr_number,
                    ci.state,
                )
                return ReviewTaskOutcome(
                    status="skipped",
                    summary=f"Waiting for CI before review (current state: {ci.state})",
                    branch=branch,
                )

        pr_diff = reviewer_gh.get_pull_request_diff(repo, pr_number)
        linked_issue_title = str(pr.get("title") or "")
        linked_issue_body = str(pr.get("body") or "")
        linked_issue_num = _extract_linked_issue_number(pr, branch)
        if linked_issue_num is not None:
            issue = reviewer_gh.get_issue(repo, linked_issue_num)
            linked_issue_title = str(issue.get("title") or linked_issue_title)
            linked_issue_body = str(issue.get("body") or linked_issue_body)

        prompt = prompt_assembler.render(
            PromptContext(
                task_type=TaskType.REVIEW,
                repo_full_name=repo,
                issue_or_pr_number=pr_number,
                repo_config=repo_config,
                repo_root=ws.repo_dir,
                issue_title=linked_issue_title,
                issue_body=linked_issue_body,
                pr_diff=pr_diff,
                ci_status_summary=ci_summary_text,
            )
        )
        schema_path = prompt_assembler.get_schema_path(TaskType.REVIEW)
        turn = agent_backend.run_turn(
            prompt,
            ws.repo_dir,
            schema=schema_path,
            timeout=float(repo_config.execution.timeout_minutes * 60),
            read_only=True,
        )
        if turn.structured_output is None:
            raise ValueError("Review agent did not return structured ReviewResult JSON")

        review_result = ReviewResult.model_validate(turn.structured_output)

        # Submit formal review under the dedicated agentic-reviewer[bot] identity.
        reviewer_gh.submit_pr_review(
            repo,
            pr_number,
            review_result,
            commit_id=head_sha or None,
        )

        # Persist state & sync labels using agentic-coder[bot] (has Issues: R/W).
        state = state_store.get(repo, pr_number) or RunState(
            repo_full_name=repo,
            issue_or_pr_number=pr_number,
            branch=branch,
            pr_number=pr_number,
        )
        state.branch = branch
        state.pr_number = pr_number
        state.last_delivery_id = envelope.delivery_id
        state.conversation_id = turn.conversation_id or state.conversation_id
        state.last_error = None
        state.updated_at = datetime.now(UTC)

        if review_result.verdict is ReviewVerdict.REQUEST_CHANGES:
            state.review_round += 1
            state.status = RunStatus.CHANGES_REQUESTED
            coder_gh.swap_agent_labels(
                repo,
                pr_number,
                add=[AgentLabel.CHANGES_REQUESTED],
                clear_other_agent_labels=True,
            )
        else:
            state.status = RunStatus.APPROVED
            coder_gh.swap_agent_labels(
                repo,
                pr_number,
                remove=[AgentLabel.IN_REVIEW, AgentLabel.CHANGES_REQUESTED],
            )
            if repo_config.review.auto_merge:
                coder_gh.enable_auto_merge(
                    repo,
                    pr_number,
                    merge_method=repo_config.review.merge_method,
                )

        state_store.put(state)
        return ReviewTaskOutcome(
            status="completed",
            summary=review_result.summary,
            verdict=review_result.verdict,
            conversation_id=turn.conversation_id,
            branch=branch,
        )
