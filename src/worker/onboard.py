"""CLI helper (`agentic onboard <owner/repo>`) for idempotently onboarding any repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import boto3

from shared.config import ConfigError, parse_repo_config
from shared.github_auth import (
    AppIdentity,
    CredentialProvider,
    EnvCredentialProvider,
    GitHubAuthError,
    SecretsManagerCredentialProvider,
)
from shared.github_client import GitHubAPIError, GitHubClientPair, create_github_clients
from shared.models import ExecutionTarget, RepoConfig, RuntimeKind

_GROUP_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class DynamoDBPutProtocol(Protocol):
    """Minimal subset of ``boto3.client('dynamodb')`` required for repo registration."""

    def put_item(self, *, TableName: str, Item: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """Single pass/fail readiness verification check emitted by ``agentic onboard``."""

    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class OnboardingReport:
    """Aggregated result of onboarding a repository."""

    repo_full_name: str
    message_group_id: str
    execution_target: str
    runtime: str
    checks: list[ReadinessCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_full_name": self.repo_full_name,
            "message_group_id": self.message_group_id,
            "execution_target": self.execution_target,
            "runtime": self.runtime,
            "all_passed": self.all_passed,
            "checks": [asdict(c) for c in self.checks],
        }


def default_message_group_id(repo_full_name: str) -> str:
    """Compute a deterministic SQS FIFO ``MessageGroupId`` from ``owner/repo``."""
    slug = _GROUP_ID_SAFE_RE.sub("-", repo_full_name.strip().lower()).strip("-")
    return f"repo-{slug}"[:128]


def register_repository(
    dynamodb_client: DynamoDBPutProtocol,
    *,
    table_name: str,
    repo_full_name: str,
    message_group_id: str,
    execution_target: ExecutionTarget,
    runtime: RuntimeKind,
    enabled: bool = True,
) -> None:
    """Idempotently upsert a repository entry into ``agentic_repo_registry``."""
    dynamodb_client.put_item(
        TableName=table_name,
        Item={
            "repo_full_name": {"S": repo_full_name},
            "message_group_id": {"S": message_group_id},
            "execution_target": {"S": execution_target.value},
            "runtime": {"S": runtime.value},
            "enabled": {"BOOL": enabled},
        },
    )


def onboard_repository(
    repo_full_name: str,
    *,
    github_clients: GitHubClientPair,
    dynamodb_client: DynamoDBPutProtocol | None = None,
    registry_table: str | None = None,
    message_group_id: str | None = None,
    execution_target: ExecutionTarget = ExecutionTarget.LOCAL,
    runtime: RuntimeKind = RuntimeKind.BASE,
    enabled: bool = True,
) -> OnboardingReport:
    """Register a repo, bootstrap ``agent:*`` labels, and run readiness checks."""
    group_id = message_group_id or default_message_group_id(repo_full_name)
    report = OnboardingReport(
        repo_full_name=repo_full_name,
        message_group_id=group_id,
        execution_target=execution_target.value,
        runtime=runtime.value,
    )

    # 1. Verify agentic-coder GitHub App installation
    coder_installed = False
    try:
        github_clients.coder.auth.get_installation_token(repo_full_name)
        coder_installed = True
        report.checks.append(
            ReadinessCheck("coder_app_installed", True, "agentic-coder App installed")
        )
    except GitHubAuthError as exc:
        report.checks.append(
            ReadinessCheck("coder_app_installed", False, f"Install agentic-coder App: {exc}")
        )

    # 2. Verify agentic-reviewer GitHub App installation
    try:
        github_clients.reviewer.auth.get_installation_token(repo_full_name)
        report.checks.append(
            ReadinessCheck("reviewer_app_installed", True, "agentic-reviewer App installed")
        )
    except GitHubAuthError as exc:
        report.checks.append(
            ReadinessCheck("reviewer_app_installed", False, f"Install agentic-reviewer App: {exc}")
        )

    # 3. Register repo in DynamoDB Repo Registry
    if dynamodb_client is not None and registry_table:
        try:
            register_repository(
                dynamodb_client,
                table_name=registry_table,
                repo_full_name=repo_full_name,
                message_group_id=group_id,
                execution_target=execution_target,
                runtime=runtime,
                enabled=enabled,
            )
            report.checks.append(
                ReadinessCheck(
                    "repo_registry_registered",
                    True,
                    f"Registered in {registry_table} "
                    f"(group={group_id}, target={execution_target.value}, runtime={runtime.value})",
                )
            )
        except Exception as exc:
            report.checks.append(
                ReadinessCheck("repo_registry_registered", False, f"DynamoDB error: {exc}")
            )
    else:
        report.checks.append(
            ReadinessCheck(
                "repo_registry_registered",
                False,
                "Skipped: provide --registry-table / REPO_REGISTRY_TABLE to register in DynamoDB",
            )
        )

    if not coder_installed:
        return report

    # 4. Bootstrap canonical agent:* labels
    try:
        lbl_res = github_clients.coder.bootstrap_labels(repo_full_name)
        report.checks.append(
            ReadinessCheck(
                "agent_labels_bootstrapped",
                True,
                f"created={len(lbl_res.created)}, updated={len(lbl_res.updated)}, "
                f"unchanged={len(lbl_res.unchanged)}",
            )
        )
    except GitHubAPIError as exc:
        report.checks.append(
            ReadinessCheck("agent_labels_bootstrapped", False, f"Label bootstrap failed: {exc}")
        )

    # 5. Fetch and validate .agentic/config.yaml
    parsed_cfg: RepoConfig | None = None
    try:
        raw_cfg = github_clients.coder.get_file_content(repo_full_name, ".agentic/config.yaml")
        if raw_cfg is None:
            report.checks.append(
                ReadinessCheck(
                    "config_yaml_valid",
                    False,
                    "Missing .agentic/config.yaml (copy a starter from templates/<stack>/)",
                )
            )
        else:
            parsed_cfg = parse_repo_config(raw_cfg)
            report.checks.append(
                ReadinessCheck(
                    "config_yaml_valid",
                    True,
                    f"Valid v{parsed_cfg.version} config (stack={parsed_cfg.stack.value}, "
                    f"verify_commands={len(parsed_cfg.execution.verify_commands)})",
                )
            )
    except (ConfigError, GitHubAPIError) as exc:
        report.checks.append(ReadinessCheck("config_yaml_valid", False, str(exc)))

    # 6. Check native auto-merge & branch protection on default branch
    default_branch = parsed_cfg.git.default_branch if parsed_cfg else "main"
    try:
        repo_meta = github_clients.coder.get_repository(repo_full_name)
        default_branch = str(repo_meta.get("default_branch") or default_branch)
        allow_auto_merge = bool(repo_meta.get("allow_auto_merge", False))
        needs_auto_merge = parsed_cfg.review.auto_merge if parsed_cfg else True
        if allow_auto_merge or not needs_auto_merge:
            detail = (
                "Native auto-merge enabled"
                if allow_auto_merge
                else "auto_merge disabled in .agentic/config.yaml"
            )
            report.checks.append(ReadinessCheck("auto_merge_enabled", True, detail))
        else:
            report.checks.append(
                ReadinessCheck(
                    "auto_merge_enabled",
                    False,
                    "Enable 'Allow auto-merge' in GitHub Settings -> General -> Pull Requests",
                )
            )
    except GitHubAPIError as exc:
        report.checks.append(ReadinessCheck("auto_merge_enabled", False, str(exc)))

    # 7. Check branch protection rules on default branch
    try:
        protection = github_clients.coder.get_branch_protection(repo_full_name, default_branch)
        if protection is None:
            report.checks.append(
                ReadinessCheck(
                    "branch_protection_configured",
                    False,
                    f"No branch protection readable on '{default_branch}'; "
                    "require 1 PR approval + CI status checks",
                )
            )
        else:
            has_pr_reviews = protection.get("required_pull_request_reviews") is not None
            has_checks = protection.get("required_status_checks") is not None
            if has_pr_reviews and has_checks:
                report.checks.append(
                    ReadinessCheck(
                        "branch_protection_configured",
                        True,
                        f"Branch '{default_branch}' requires PR approval and status checks",
                    )
                )
            else:
                missing = []
                if not has_pr_reviews:
                    missing.append("PR review approval")
                if not has_checks:
                    missing.append("required status checks")
                report.checks.append(
                    ReadinessCheck(
                        "branch_protection_configured",
                        False,
                        f"Branch '{default_branch}' protection missing: {', '.join(missing)}",
                    )
                )
    except GitHubAPIError as exc:
        report.checks.append(ReadinessCheck("branch_protection_configured", False, str(exc)))

    return report


def _format_human_report(report: OnboardingReport) -> str:
    lines = [
        f"Onboarding readiness for {report.repo_full_name}",
        f"  MessageGroupId:   {report.message_group_id}",
        f"  Execution Target: {report.execution_target}",
        f"  Runtime:          {report.runtime}",
        "",
    ]
    for check in report.checks:
        badge = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{badge}] {check.name}: {check.detail}")
    lines.append("")
    lines.append("Result: READY" if report.all_passed else "Result: ACTION REQUIRED")
    return "\n".join(lines)


def _build_credential_provider(environ: Mapping[str, str], region: str) -> CredentialProvider:
    coder_arn = environ.get("GITHUB_CODER_SECRET_ARN", "").strip()
    reviewer_arn = environ.get("GITHUB_REVIEWER_SECRET_ARN", "").strip()
    if coder_arn and reviewer_arn:
        return SecretsManagerCredentialProvider(
            {
                AppIdentity.CODER: coder_arn,
                AppIdentity.REVIEWER: reviewer_arn,
            },
            region_name=region,
        )
    return EnvCredentialProvider(environ)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic",
        description="CLI utilities for managing agentic-workflow repositories.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    onboard_cmd = sub.add_parser(
        "onboard",
        help="Register a repository, bootstrap agent:* labels, and run readiness checks.",
    )
    onboard_cmd.add_argument("repo", help="GitHub repository in 'owner/repo' format")
    onboard_cmd.add_argument(
        "--target",
        choices=[t.value for t in ExecutionTarget],
        default=ExecutionTarget.LOCAL.value,
        help="Execution target (default: local)",
    )
    onboard_cmd.add_argument(
        "--runtime",
        choices=[r.value for r in RuntimeKind],
        default=RuntimeKind.BASE.value,
        help="Runtime profile (default: base)",
    )
    onboard_cmd.add_argument(
        "--group-id",
        default=None,
        help="Override deterministic SQS FIFO MessageGroupId (default: repo-<owner>-<repo>)",
    )
    onboard_cmd.add_argument(
        "--registry-table",
        default=os.environ.get("REPO_REGISTRY_TABLE", "agentic-workflow-prod-repo-registry"),
        help="DynamoDB Repo Registry table name",
    )
    onboard_cmd.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: us-east-1)",
    )
    onboard_cmd.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON readiness report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``agentic`` CLI."""
    args = _build_parser().parse_args(argv)
    if args.command != "onboard":
        return 2

    env = os.environ
    cred_provider = _build_credential_provider(env, args.region)
    clients = create_github_clients(cred_provider)
    ddb_client = boto3.client("dynamodb", region_name=args.region)

    try:
        report = onboard_repository(
            args.repo,
            github_clients=clients,
            dynamodb_client=ddb_client,
            registry_table=args.registry_table,
            message_group_id=args.group_id,
            execution_target=ExecutionTarget(args.target),
            runtime=RuntimeKind(args.runtime),
        )
    finally:
        clients.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(_format_human_report(report))

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
