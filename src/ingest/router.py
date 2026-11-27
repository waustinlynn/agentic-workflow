"""Event routing from verified GitHub webhooks to ``TaskEnvelope`` or label bootstrap."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Any, Literal

from ingest.registry import RepoRegistry, RepoRegistryEntry
from ingest.security import has_paused_label, is_actor_allowed, is_external_fork_pr
from shared.labels import AgentLabel
from shared.models import ExecutionTarget, RuntimeKind, TaskEnvelope, TaskType

_ISSUE_LABEL_TO_TASK: dict[str, TaskType] = {
    AgentLabel.TRIAGE.value: TaskType.TRIAGE,
    AgentLabel.PLAN.value: TaskType.PLAN,
    AgentLabel.READY.value: TaskType.CODE_AND_TEST,
}

_FAILURE_CHECK_CONCLUSIONS = frozenset({"failure", "timed_out"})
_BASE_ONLY_TASK_TYPES = frozenset({TaskType.REVIEW, TaskType.PLAN, TaskType.TRIAGE})


def resolve_runtime_queue_key(task: TaskEnvelope) -> str | None:
    """Resolve the Lambda runtime queue key for ``task``, or ``None`` for the Phase 1 local queue.

    Routing rules:
    - ``execution_target == "local"`` -> ``None`` (primary local FIFO queue)
    - ``execution_target == "lambda"``:
      - ``review``, ``plan``, and ``triage`` tasks -> ``"base"`` (lightweight ``worker-base`` image)
      - ``runtime`` in ``{"base", "ci-verified"}`` -> ``"base"``
      - Any stack runtime (``python``, ``node``, ``infra``, etc.) -> ``task.runtime.value``
    """
    if task.execution_target is ExecutionTarget.LOCAL:
        return None
    if task.task_type in _BASE_ONLY_TASK_TYPES:
        return RuntimeKind.BASE.value
    if task.runtime in {RuntimeKind.BASE, RuntimeKind.CI_VERIFIED}:
        return RuntimeKind.BASE.value
    return task.runtime.value


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Outcome of evaluating a verified GitHub webhook event."""

    action: Literal["ping", "enqueue", "bootstrap_labels", "ignore"]
    reason: str
    task: TaskEnvelope | None = None
    registry_entry: RepoRegistryEntry | None = None
    bootstrap_repos: tuple[str, ...] = ()


def _get_mapping(obj: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    val = obj.get(key)
    return val if isinstance(val, Mapping) else {}


def _get_actor(payload: Mapping[str, Any]) -> str:
    sender = _get_mapping(payload, "sender")
    login = sender.get("login")
    return str(login).strip() if isinstance(login, str) else ""


def _get_repo_full_name(payload: Mapping[str, Any]) -> str:
    repo = _get_mapping(payload, "repository")
    full_name = repo.get("full_name")
    return str(full_name).strip() if isinstance(full_name, str) else ""


def _route_installation_repositories(
    payload: Mapping[str, Any],
    *,
    allowed_actors: Set[str],
    registry: RepoRegistry,
) -> RouteDecision:
    if payload.get("action") != "added":
        return RouteDecision(
            action="ignore",
            reason=f"unsupported installation_repositories action: {payload.get('action')!r}",
        )

    actor = _get_actor(payload)
    if not is_actor_allowed(actor, allowed_actors):
        return RouteDecision(action="ignore", reason=f"unauthorized actor: {actor!r}")

    added = payload.get("repositories_added")
    if not isinstance(added, list):
        return RouteDecision(action="ignore", reason="no repositories_added in payload")

    registered: list[str] = []
    for item in added:
        if not isinstance(item, Mapping):
            continue
        full_name = str(item.get("full_name") or "").strip()
        if not full_name:
            continue
        entry = registry.get_repo(full_name)
        if entry is not None and entry.enabled:
            registered.append(entry.repo_full_name)

    if not registered:
        return RouteDecision(
            action="ignore",
            reason="no added repositories are registered and enabled",
        )

    return RouteDecision(
        action="bootstrap_labels",
        reason="registered repositories added to GitHub App installation",
        bootstrap_repos=tuple(registered),
    )


def _route_issues_event(
    delivery_id: str,
    actor: str,
    entry: RepoRegistryEntry,
    payload: Mapping[str, Any],
) -> RouteDecision:
    if payload.get("action") != "labeled":
        return RouteDecision(
            action="ignore",
            reason=f"unsupported issues action: {payload.get('action')!r}",
        )

    issue = _get_mapping(payload, "issue")
    if "pull_request" in issue:
        return RouteDecision(action="ignore", reason="issues.labeled event refers to a PR")

    labels = issue.get("labels")
    if isinstance(labels, list) and has_paused_label(labels):
        return RouteDecision(action="ignore", reason="issue is paused via agent:paused")

    label = _get_mapping(payload, "label")
    label_name = str(label.get("name") or "").strip().lower()
    task_type = _ISSUE_LABEL_TO_TASK.get(label_name)
    if task_type is None:
        return RouteDecision(action="ignore", reason=f"label {label_name!r} does not trigger work")

    issue_number = issue.get("number")
    if not isinstance(issue_number, int) or issue_number <= 0:
        return RouteDecision(action="ignore", reason="missing or invalid issue number")

    task = TaskEnvelope(
        task_type=task_type,
        repo_full_name=entry.repo_full_name,
        issue_or_pr_number=issue_number,
        branch=None,
        delivery_id=delivery_id,
        actor=actor,
        execution_target=entry.execution_target,
        runtime=entry.runtime,
        payload={
            "event": "issues.labeled",
            "trigger_label": label_name,
            "issue_title": str(issue.get("title") or ""),
        },
    )
    return RouteDecision(
        action="enqueue",
        reason=f"routed issues.labeled ({label_name}) -> {task_type.value}",
        task=task,
        registry_entry=entry,
    )


def _route_pull_request_review_event(
    delivery_id: str,
    actor: str,
    entry: RepoRegistryEntry,
    payload: Mapping[str, Any],
    *,
    allowed_actors: Set[str],
) -> RouteDecision:
    if payload.get("action") != "submitted":
        return RouteDecision(
            action="ignore",
            reason=f"unsupported pull_request_review action: {payload.get('action')!r}",
        )

    review = _get_mapping(payload, "review")
    review_state = str(review.get("state") or "").strip().lower()
    if review_state != "changes_requested":
        return RouteDecision(
            action="ignore",
            reason=f"review state {review_state!r} does not trigger address_review",
        )

    reviewer_user = _get_mapping(review, "user")
    reviewer_login = str(reviewer_user.get("login") or "").strip()
    if not is_actor_allowed(reviewer_login, allowed_actors):
        return RouteDecision(
            action="ignore",
            reason=f"unauthorized reviewer login: {reviewer_login!r}",
        )

    pr = _get_mapping(payload, "pull_request")
    if is_external_fork_pr(pr, entry.repo_full_name):
        return RouteDecision(action="ignore", reason="external fork pull request rejected")

    pr_labels = pr.get("labels")
    if isinstance(pr_labels, list) and has_paused_label(pr_labels):
        return RouteDecision(action="ignore", reason="pull request is paused via agent:paused")

    pr_number = pr.get("number")
    head = _get_mapping(pr, "head")
    branch = head.get("ref")
    if not isinstance(pr_number, int) or pr_number <= 0 or not isinstance(branch, str):
        return RouteDecision(action="ignore", reason="missing PR number or head branch")

    task = TaskEnvelope(
        task_type=TaskType.ADDRESS_REVIEW,
        repo_full_name=entry.repo_full_name,
        issue_or_pr_number=pr_number,
        branch=branch,
        delivery_id=delivery_id,
        actor=actor,
        execution_target=entry.execution_target,
        runtime=entry.runtime,
        payload={
            "event": "pull_request_review.submitted",
            "review_id": review.get("id"),
            "reviewer": reviewer_login,
            "head_sha": head.get("sha"),
        },
    )
    return RouteDecision(
        action="enqueue",
        reason="routed pull_request_review.submitted (changes_requested) -> address_review",
        task=task,
        registry_entry=entry,
    )


def _route_check_suite_event(
    delivery_id: str,
    actor: str,
    entry: RepoRegistryEntry,
    payload: Mapping[str, Any],
) -> RouteDecision:
    if payload.get("action") != "completed":
        return RouteDecision(
            action="ignore",
            reason=f"unsupported check_suite action: {payload.get('action')!r}",
        )

    check_suite = _get_mapping(payload, "check_suite")
    conclusion = str(check_suite.get("conclusion") or "").strip().lower()
    if conclusion == "success":
        task_type = TaskType.REVIEW
    elif conclusion in _FAILURE_CHECK_CONCLUSIONS:
        task_type = TaskType.FIX_CI
    else:
        return RouteDecision(
            action="ignore",
            reason=f"check_suite conclusion {conclusion!r} does not trigger work",
        )

    pull_requests = check_suite.get("pull_requests")
    if not isinstance(pull_requests, list) or not pull_requests:
        return RouteDecision(action="ignore", reason="check_suite has no associated pull requests")

    pr = pull_requests[0]
    if not isinstance(pr, Mapping):
        return RouteDecision(action="ignore", reason="invalid pull request entry in check_suite")

    # Reject external forks: check_suite PR head.repo.id vs base.repo.id (or full_name if present).
    head = _get_mapping(pr, "head")
    base = _get_mapping(pr, "base")
    head_repo = _get_mapping(head, "repo")
    base_repo = _get_mapping(base, "repo")
    head_full = head_repo.get("full_name")
    if isinstance(head_full, str) and head_full.strip():
        if head_full.strip().lower() != entry.repo_full_name.lower():
            return RouteDecision(action="ignore", reason="external fork PR in check_suite")
    elif head_repo.get("id") is None or head_repo.get("id") != base_repo.get("id"):
        return RouteDecision(action="ignore", reason="external or unknown fork PR in check_suite")

    pr_labels = pr.get("labels")
    if isinstance(pr_labels, list) and has_paused_label(pr_labels):
        return RouteDecision(action="ignore", reason="pull request is paused via agent:paused")

    branch = str(head.get("ref") or check_suite.get("head_branch") or "").strip()
    if not branch.startswith("agent/"):
        return RouteDecision(
            action="ignore",
            reason=f"check_suite PR branch {branch!r} is not an agent-managed branch",
        )

    pr_number = pr.get("number")
    if not isinstance(pr_number, int) or pr_number <= 0:
        return RouteDecision(action="ignore", reason="missing PR number in check_suite")

    task = TaskEnvelope(
        task_type=task_type,
        repo_full_name=entry.repo_full_name,
        issue_or_pr_number=pr_number,
        branch=branch,
        delivery_id=delivery_id,
        actor=actor,
        execution_target=entry.execution_target,
        runtime=entry.runtime,
        payload={
            "event": "check_suite.completed",
            "conclusion": conclusion,
            "head_sha": check_suite.get("head_sha"),
            "check_suite_id": check_suite.get("id"),
        },
    )
    return RouteDecision(
        action="enqueue",
        reason=f"routed check_suite.completed ({conclusion}) -> {task_type.value}",
        task=task,
        registry_entry=entry,
    )


def route_webhook_event(
    event_name: str,
    delivery_id: str,
    payload: Mapping[str, Any],
    *,
    allowed_actors: Set[str],
    registry: RepoRegistry,
) -> RouteDecision:
    """Evaluate a signature-verified GitHub webhook and decide what action to take."""
    normalized_event = event_name.strip().lower()
    if normalized_event == "ping":
        return RouteDecision(action="ping", reason="github webhook ping")

    if normalized_event == "installation_repositories":
        return _route_installation_repositories(
            payload,
            allowed_actors=allowed_actors,
            registry=registry,
        )

    repo_full_name = _get_repo_full_name(payload)
    if not repo_full_name:
        return RouteDecision(action="ignore", reason="missing repository.full_name")

    entry = registry.get_repo(repo_full_name)
    if entry is None or not entry.enabled:
        return RouteDecision(
            action="ignore",
            reason=f"repository {repo_full_name!r} is not registered or is disabled",
        )

    actor = _get_actor(payload)
    if not is_actor_allowed(actor, allowed_actors):
        return RouteDecision(action="ignore", reason=f"unauthorized actor: {actor!r}")

    if normalized_event == "issues":
        return _route_issues_event(delivery_id, actor, entry, payload)
    if normalized_event == "pull_request_review":
        return _route_pull_request_review_event(
            delivery_id,
            actor,
            entry,
            payload,
            allowed_actors=allowed_actors,
        )
    if normalized_event == "check_suite":
        return _route_check_suite_event(delivery_id, actor, entry, payload)

    return RouteDecision(action="ignore", reason=f"unhandled event type: {normalized_event!r}")
