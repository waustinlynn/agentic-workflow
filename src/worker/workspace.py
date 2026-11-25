"""Isolated git workspace manager with verify-command runner and protected-path guard."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.config import load_repo_config_or_default
from shared.models import RepoConfig

BOT_AUTHOR_NAME = "agentic-coder[bot]"
BOT_AUTHOR_EMAIL = "agentic-coder[bot]@users.noreply.github.com"


class WorkspaceError(RuntimeError):
    """Raised when git checkout, commit, or push operations fail."""


class ProtectedPathViolationError(WorkspaceError):
    """Raised when modified files violate ``safety.protected_paths``."""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations = tuple(sorted(set(violations)))
        joined = ", ".join(self.violations)
        super().__init__(f"Refusing to commit/push changes touching protected path(s): {joined}")


class CommandExecutionError(WorkspaceError):
    """Raised when a repo setup or verification command fails or times out."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        reason = "timed out" if result.timed_out else f"exited with status {result.returncode}"
        super().__init__(f"Command {result.command!r} {reason}")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured result of running a setup or verification command."""

    command: str
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.timed_out and self.returncode == 0


def _normalize_rel_path(value: str) -> str:
    norm = value.strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def _compile_glob_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a git/glob pattern supporting ``*``, ``?``, and ``**`` across path segments."""
    norm = _normalize_rel_path(pattern)
    regex_parts: list[str] = ["^"]
    idx = 0
    length = len(norm)
    while idx < length:
        if norm[idx : idx + 3] == "**/":
            regex_parts.append("(?:.*/)?")
            idx += 3
        elif norm[idx : idx + 2] == "**":
            regex_parts.append(".*")
            idx += 2
        elif norm[idx] == "*":
            regex_parts.append("[^/]*")
            idx += 1
        elif norm[idx] == "?":
            regex_parts.append("[^/]")
            idx += 1
        else:
            regex_parts.append(re.escape(norm[idx]))
            idx += 1
    regex_parts.append("$")
    return re.compile("".join(regex_parts))


def find_protected_path_violations(
    changed_files: Iterable[str],
    protected_patterns: Iterable[str],
) -> list[str]:
    """Return all relative file paths matching any pattern in ``protected_patterns``."""
    compiled = [_compile_glob_pattern(p) for p in protected_patterns if p.strip()]
    violations: list[str] = []
    for raw_path in changed_files:
        norm_path = _normalize_rel_path(raw_path)
        if not norm_path:
            continue
        if any(regex.match(norm_path) for regex in compiled):
            violations.append(norm_path)
    return sorted(set(violations))


def _git_auth_args(token: str | None) -> list[str]:
    """Build transient git auth header args so tokens are never persisted in ``.git/config``."""
    if not token:
        return []
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {encoded}"]


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    token: str | None = None,
    timeout: float = 120.0,
) -> str:
    cmd = ["git", *_git_auth_args(token), *args]
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {' '.join(args[:2])} timed out after {timeout:.0f}s") from exc

    if proc.returncode != 0:
        # Redact any accidental token reflection in git stderr.
        stderr = proc.stderr.strip()
        if token:
            stderr = stderr.replace(token, "***")
        raise WorkspaceError(f"git {' '.join(args[:2])} failed (exit {proc.returncode}): {stderr}")
    return proc.stdout.strip()


@dataclass(slots=True)
class WorkspaceHandle:
    """Active ephemeral git checkout for a single task execution."""

    repo_full_name: str
    repo_dir: Path
    branch: str
    base_branch: str
    repo_config: RepoConfig
    _token: str | None = None
    _cleaned_up: bool = False

    def __enter__(self) -> WorkspaceHandle:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    @property
    def working_dir(self) -> Path:
        return (self.repo_dir / self.repo_config.execution.working_directory).resolve()

    def run_command(
        self,
        command: str,
        *,
        timeout_seconds: float = 300.0,
        extra_env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Execute a single shell command inside the repo's configured working directory."""
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)

        started = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.working_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            return CommandResult(
                command=command,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                timed_out=False,
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            return CommandResult(
                command=command,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                duration_seconds=time.monotonic() - started,
            )

    def run_setup_commands(self, *, timeout_per_command: float = 300.0) -> list[CommandResult]:
        """Run all ``execution.setup_commands`` in order, failing fast on error."""
        results: list[CommandResult] = []
        for cmd in self.repo_config.execution.setup_commands:
            res = self.run_command(cmd, timeout_seconds=timeout_per_command)
            results.append(res)
            if not res.passed:
                raise CommandExecutionError(res)
        return results

    def run_verify_commands(self, *, timeout_per_command: float = 300.0) -> list[CommandResult]:
        """Run all ``execution.verify_commands`` in order, failing fast on error."""
        results: list[CommandResult] = []
        for cmd in self.repo_config.execution.verify_commands:
            res = self.run_command(cmd, timeout_seconds=timeout_per_command)
            results.append(res)
            if not res.passed:
                raise CommandExecutionError(res)
        return results

    def changed_files(self) -> list[str]:
        """Return all files changed relative to base branch plus any unstaged/untracked files."""
        files: set[str] = set()

        # 1. Working tree & index changes (including untracked files).
        status_out = _run_git(["status", "--porcelain", "-uall"], cwd=self.repo_dir)
        for line in status_out.splitlines():
            if len(line) < 4:
                continue
            path_part = line[3:].strip()
            if " -> " in path_part:
                old_p, new_p = path_part.split(" -> ", 1)
                files.add(old_p.strip().strip('"'))
                files.add(new_p.strip().strip('"'))
            else:
                files.add(path_part.strip('"'))

        # 2. Committed changes on this branch relative to origin/<base_branch>.
        base_ref = f"origin/{self.base_branch}"
        try:
            diff_out = _run_git(["diff", "--name-only", f"{base_ref}...HEAD"], cwd=self.repo_dir)
            for line in diff_out.splitlines():
                if line.strip():
                    files.add(line.strip())
        except WorkspaceError:
            pass

        return sorted(files)

    def assert_no_protected_paths(self) -> None:
        """Hard-verify no changed, untracked, or committed file matches ``protected_paths``."""
        violations = find_protected_path_violations(
            self.changed_files(),
            self.repo_config.safety.effective_protected_paths,
        )
        if violations:
            raise ProtectedPathViolationError(violations)

    def commit_and_push(
        self,
        commit_message: str,
        *,
        author_name: str = BOT_AUTHOR_NAME,
        author_email: str = BOT_AUTHOR_EMAIL,
    ) -> str | None:
        """Enforce protected paths, commit working tree changes, and push to ``origin``."""
        self.assert_no_protected_paths()

        status_out = _run_git(["status", "--porcelain"], cwd=self.repo_dir)
        if status_out:
            _run_git(["add", "-A"], cwd=self.repo_dir)
            self.assert_no_protected_paths()
            _run_git(
                [
                    "-c",
                    f"user.name={author_name}",
                    "-c",
                    f"user.email={author_email}",
                    "commit",
                    "-m",
                    commit_message,
                ],
                cwd=self.repo_dir,
            )

        # Check whether branch has commits ahead of origin/<base_branch> or origin/<branch>.
        head_sha = _run_git(["rev-parse", "HEAD"], cwd=self.repo_dir)
        base_sha = _run_git(["rev-parse", f"origin/{self.base_branch}"], cwd=self.repo_dir)
        if not status_out and head_sha == base_sha:
            return None

        _run_git(
            ["push", "origin", f"HEAD:refs/heads/{self.branch}"],
            cwd=self.repo_dir,
            token=self._token,
        )
        return head_sha

    def cleanup(self) -> None:
        """Remove the ephemeral checkout directory."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        if self.repo_dir.exists():
            shutil.rmtree(self.repo_dir, ignore_errors=True)


class WorkspaceManager:
    """Creates isolated, ephemeral git checkouts compatible with local dev and Lambda ``/tmp``."""

    def __init__(self, base_dir: Path | None = None) -> None:
        default_root = Path(
            os.environ.get(
                "AGENTIC_WORKSPACE_DIR", str(Path(tempfile.gettempdir()) / "agentic-workspaces")
            )
        )
        self.base_dir = (base_dir or default_root).resolve()

    def checkout(
        self,
        repo_full_name: str,
        *,
        branch: str | None = None,
        issue_number: int | None = None,
        token: str | None = None,
        clone_url: str | None = None,
        depth: int = 50,
    ) -> WorkspaceHandle:
        """Clone an isolated checkout, load ``.agentic/config.yaml``, and check out the branch."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        safe_slug = repo_full_name.replace("/", "-")
        checkout_dir = self.base_dir / f"{safe_slug}-{uuid.uuid4().hex[:10]}"
        url = clone_url or f"https://github.com/{repo_full_name}.git"

        try:
            clone_args = ["clone", "--no-single-branch", f"--depth={depth}", url, str(checkout_dir)]
            _run_git(clone_args, token=token)

            repo_config = load_repo_config_or_default(checkout_dir)
            base_branch = repo_config.git.default_branch
            target_branch = branch
            if target_branch is None:
                if issue_number is None:
                    raise WorkspaceError("Either 'branch' or 'issue_number' must be provided")
                target_branch = f"{repo_config.git.branch_prefix}issue-{issue_number}"

            # Check if target_branch already exists on origin; otherwise branch from origin/default.
            remote_branches = _run_git(
                ["ls-remote", "--heads", "origin", target_branch],
                cwd=checkout_dir,
                token=token,
            )
            if remote_branches:
                _run_git(
                    ["fetch", "--depth", str(depth), "origin", target_branch],
                    cwd=checkout_dir,
                    token=token,
                )
                _run_git(
                    ["checkout", "-B", target_branch, f"origin/{target_branch}"],
                    cwd=checkout_dir,
                )
            else:
                _run_git(
                    ["checkout", "-B", target_branch, f"origin/{base_branch}"],
                    cwd=checkout_dir,
                )

            return WorkspaceHandle(
                repo_full_name=repo_full_name,
                repo_dir=checkout_dir,
                branch=target_branch,
                base_branch=base_branch,
                repo_config=repo_config,
                _token=token,
            )
        except Exception:
            if checkout_dir.exists():
                shutil.rmtree(checkout_dir, ignore_errors=True)
            raise
