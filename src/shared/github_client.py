"""Reusable GitHub API client supporting 2-App operations and label bootstrapping."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from shared.github_auth import (
    GITHUB_API_BASE_URL,
    GITHUB_API_VERSION,
    AppIdentity,
    CredentialProvider,
    GitHubAppAuth,
)
from shared.labels import AGENT_LABEL_SPECS, AgentLabel, is_agent_label
from shared.models import CIStatusSummary, MergeMethod, ReviewResult

_ENABLE_AUTO_MERGE_MUTATION = """
mutation EnableAutoMerge($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(
    input: {pullRequestId: $pullRequestId, mergeMethod: $mergeMethod}
  ) {
    pullRequest {
      id
      number
      autoMergeRequest {
        enabledAt
        mergeMethod
      }
    }
  }
}
"""

_GRAPHQL_MERGE_METHOD: dict[MergeMethod, str] = {
    MergeMethod.SQUASH: "SQUASH",
    MergeMethod.MERGE: "MERGE",
    MergeMethod.REBASE: "REBASE",
}

_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "stale", "startup_failure"}
)


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub REST or GraphQL API call returns an error."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(slots=True)
class BootstrapLabelsResult:
    """Summary of an idempotent ``bootstrap_labels`` execution."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


class GitHubClient:
    """Typed GitHub REST/GraphQL client backed by a single GitHub App identity."""

    def __init__(
        self,
        auth: GitHubAppAuth,
        *,
        http_client: httpx.Client | None = None,
        api_base_url: str = GITHUB_API_BASE_URL,
    ) -> None:
        self.auth = auth
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(timeout=30.0)
        self._api_base_url = api_base_url.rstrip("/")

    @property
    def identity(self) -> AppIdentity:
        return self.auth.identity

    def close(self) -> None:
        self.auth.close()
        if self._owns_http_client:
            self._http.close()

    def _request(
        self,
        method: str,
        repo_full_name: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = self.auth.get_installation_token(repo_full_name)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": accept,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        url = f"{self._api_base_url}{path}"
        response = self._http.request(
            method,
            url,
            headers=headers,
            json=json_body,
            params=params,
        )
        if response.status_code >= 400:
            raise GitHubAPIError(
                f"GitHub API {method} {path} failed for {repo_full_name} "
                f"as {self.identity.value} (HTTP {response.status_code})",
                status_code=response.status_code,
                body=response.text,
            )
        return response

    def get_issue(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        resp = self._request(
            "GET", repo_full_name, f"/repos/{repo_full_name}/issues/{issue_number}"
        )
        data: dict[str, Any] = resp.json()
        return data

    def list_issue_comments(
        self, repo_full_name: str, issue_or_pr_number: int
    ) -> list[dict[str, Any]]:
        resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/comments",
            params={"per_page": 100},
        )
        data: list[dict[str, Any]] = resp.json()
        return data

    def post_comment(
        self,
        repo_full_name: str,
        issue_or_pr_number: int,
        body: str,
    ) -> dict[str, Any]:
        resp = self._request(
            "POST",
            repo_full_name,
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/comments",
            json_body={"body": body},
        )
        data: dict[str, Any] = resp.json()
        return data

    def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        resp = self._request("GET", repo_full_name, f"/repos/{repo_full_name}/pulls/{pr_number}")
        data: dict[str, Any] = resp.json()
        return data

    def get_pull_request_diff(self, repo_full_name: str, pr_number: int) -> str:
        resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/pulls/{pr_number}",
            accept="application/vnd.github.v3.diff",
        )
        return resp.text

    def list_pull_request_files(self, repo_full_name: str, pr_number: int) -> list[dict[str, Any]]:
        resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )
        data: list[dict[str, Any]] = resp.json()
        return data

    def get_ci_status(self, repo_full_name: str, ref: str) -> CIStatusSummary:
        """Aggregate GitHub Check Runs and Commit Statuses for a commit SHA or branch."""
        encoded_ref = quote(ref, safe="")
        checks_resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/commits/{encoded_ref}/check-runs",
            params={"per_page": 100},
        ).json()
        status_resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/commits/{encoded_ref}/status",
        ).json()

        failed: list[str] = []
        pending: list[str] = []
        total = 0

        for run in checks_resp.get("check_runs", []):
            total += 1
            name = str(run.get("name", "unnamed-check"))
            status = str(run.get("status", ""))
            conclusion = str(run.get("conclusion") or "")
            if status != "completed":
                pending.append(name)
            elif conclusion in _FAILURE_CONCLUSIONS or conclusion not in _SUCCESS_CONCLUSIONS:
                failed.append(name)

        for commit_status in status_resp.get("statuses", []):
            total += 1
            context = str(commit_status.get("context", "unnamed-status"))
            st = str(commit_status.get("state", ""))
            if st == "pending":
                pending.append(context)
            elif st != "success":
                failed.append(context)

        if failed:
            overall: str = "failure"
        elif pending:
            overall = "pending"
        else:
            overall = "success"

        return CIStatusSummary(
            state=overall,  # type: ignore[arg-type]
            total_checks=total,
            failed_checks=failed,
            pending_checks=pending,
        )

    def list_labels(self, repo_full_name: str, issue_or_pr_number: int) -> list[str]:
        resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/labels",
            params={"per_page": 100},
        )
        return [str(item["name"]) for item in resp.json()]

    def swap_agent_labels(
        self,
        repo_full_name: str,
        issue_or_pr_number: int,
        *,
        add: Iterable[AgentLabel | str] = (),
        remove: Iterable[AgentLabel | str] = (),
        clear_other_agent_labels: bool = False,
    ) -> list[str]:
        """Atomically update labels on an issue or PR while preserving non-agent labels."""
        current = self.list_labels(repo_full_name, issue_or_pr_number)
        to_remove = {str(label) for label in remove}
        to_add = [str(label) for label in add]

        updated: list[str] = []
        for existing in current:
            if existing in to_remove:
                continue
            if (
                clear_other_agent_labels
                and is_agent_label(existing)
                and existing != AgentLabel.PAUSED.value
                and existing not in to_add
            ):
                continue
            updated.append(existing)

        for label in to_add:
            if label not in updated:
                updated.append(label)

        resp = self._request(
            "PUT",
            repo_full_name,
            f"/repos/{repo_full_name}/issues/{issue_or_pr_number}/labels",
            json_body={"labels": updated},
        )
        return [str(item["name"]) for item in resp.json()]

    def submit_pr_review(
        self,
        repo_full_name: str,
        pr_number: int,
        review: ReviewResult,
        *,
        commit_id: str | None = None,
        allow_non_reviewer: bool = False,
    ) -> dict[str, Any]:
        """Submit a formal PR review (``APPROVE`` or ``REQUEST_CHANGES``) with inline comments."""
        if self.identity is not AppIdentity.REVIEWER and not allow_non_reviewer:
            raise GitHubAPIError(
                f"submit_pr_review must be called with {AppIdentity.REVIEWER.value!r}, "
                f"not {self.identity.value!r}"
            )

        payload: dict[str, Any] = {
            "event": review.verdict.value,
            "body": review.summary,
        }
        if commit_id is not None:
            payload["commit_id"] = commit_id
        if review.comments:
            payload["comments"] = [
                {
                    "path": comment.path,
                    "line": comment.line,
                    "side": comment.side,
                    "body": f"[{comment.severity.value}] {comment.body}",
                }
                for comment in review.comments
            ]

        resp = self._request(
            "POST",
            repo_full_name,
            f"/repos/{repo_full_name}/pulls/{pr_number}/reviews",
            json_body=payload,
        )
        data: dict[str, Any] = resp.json()
        return data

    def enable_auto_merge(
        self,
        repo_full_name: str,
        pr_number: int,
        *,
        merge_method: MergeMethod = MergeMethod.SQUASH,
    ) -> dict[str, Any]:
        """Enable native GitHub auto-merge on a pull request via GraphQL."""
        pr = self.get_pull_request(repo_full_name, pr_number)
        node_id = pr.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise GitHubAPIError(
                f"Pull request #{pr_number} in {repo_full_name} is missing node_id"
            )

        resp = self._request(
            "POST",
            repo_full_name,
            "/graphql",
            json_body={
                "query": _ENABLE_AUTO_MERGE_MUTATION,
                "variables": {
                    "pullRequestId": node_id,
                    "mergeMethod": _GRAPHQL_MERGE_METHOD[merge_method],
                },
            },
        )
        body: dict[str, Any] = resp.json()
        errors = body.get("errors")
        if errors:
            raise GitHubAPIError(
                f"GraphQL enablePullRequestAutoMerge failed for "
                f"{repo_full_name}#{pr_number}: {errors}",
                status_code=resp.status_code,
                body=resp.text,
            )
        result: dict[str, Any] = body["data"]["enablePullRequestAutoMerge"]["pullRequest"]
        return result

    def bootstrap_labels(self, repo_full_name: str) -> BootstrapLabelsResult:
        """Idempotently ensure all standard ``agent:*`` labels exist with expected metadata."""
        existing_resp = self._request(
            "GET",
            repo_full_name,
            f"/repos/{repo_full_name}/labels",
            params={"per_page": 100},
        )
        existing_by_name: dict[str, dict[str, Any]] = {
            str(label["name"]).lower(): label for label in existing_resp.json()
        }

        result = BootstrapLabelsResult()
        for spec in AGENT_LABEL_SPECS:
            name = spec.name.value
            current = existing_by_name.get(name.lower())
            if current is None:
                self._request(
                    "POST",
                    repo_full_name,
                    f"/repos/{repo_full_name}/labels",
                    json_body={
                        "name": name,
                        "color": spec.color,
                        "description": spec.description,
                    },
                )
                result.created.append(name)
                continue

            current_color = str(current.get("color", "")).lower()
            current_desc = str(current.get("description") or "")
            if current_color != spec.color.lower() or current_desc != spec.description:
                encoded_name = quote(name, safe="")
                self._request(
                    "PATCH",
                    repo_full_name,
                    f"/repos/{repo_full_name}/labels/{encoded_name}",
                    json_body={
                        "new_name": name,
                        "color": spec.color,
                        "description": spec.description,
                    },
                )
                result.updated.append(name)
            else:
                result.unchanged.append(name)

        return result


@dataclass(frozen=True, slots=True)
class GitHubClientPair:
    """Paired GitHub clients enforcing 2-App separation (coder vs reviewer)."""

    coder: GitHubClient
    reviewer: GitHubClient

    def close(self) -> None:
        self.coder.close()
        self.reviewer.close()


def create_github_clients(
    credential_provider: CredentialProvider,
    *,
    http_client: httpx.Client | None = None,
    api_base_url: str = GITHUB_API_BASE_URL,
) -> GitHubClientPair:
    """Create paired ``agentic-coder`` and ``agentic-reviewer`` GitHub clients."""
    coder_auth = GitHubAppAuth(
        AppIdentity.CODER,
        credential_provider,
        http_client=http_client,
        api_base_url=api_base_url,
    )
    reviewer_auth = GitHubAppAuth(
        AppIdentity.REVIEWER,
        credential_provider,
        http_client=http_client,
        api_base_url=api_base_url,
    )
    return GitHubClientPair(
        coder=GitHubClient(coder_auth, http_client=http_client, api_base_url=api_base_url),
        reviewer=GitHubClient(reviewer_auth, http_client=http_client, api_base_url=api_base_url),
    )
