"""Unit tests for WorkspaceManager git checkouts, verify commands, and protected-path guards."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from worker.workspace import (
    CommandExecutionError,
    ProtectedPathViolationError,
    WorkspaceManager,
    find_protected_path_violations,
)


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _create_bare_origin(tmp_path: Path, config_yaml: str = "") -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(["init", "--bare", "-b", "main"], cwd=bare)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["clone", str(bare), str(seed)], cwd=tmp_path)
    _git(["config", "user.name", "Test User"], cwd=seed)
    _git(["config", "user.email", "test@example.com"], cwd=seed)
    (seed / "README.md").write_text("# Demo\n", encoding="utf-8")
    if config_yaml:
        cfg_path = seed / ".agentic" / "config.yaml"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(config_yaml, encoding="utf-8")
    _git(["add", "-A"], cwd=seed)
    _git(["commit", "-m", "chore: initial commit"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return bare


class TestProtectedPathMatching:
    def test_matches_default_and_custom_globs(self) -> None:
        patterns = [".github/workflows/**", ".agentic/**", "CODEOWNERS", "infra/prod/**"]
        changed = [
            "src/app.py",
            ".github/workflows/ci.yml",
            ".github/workflows/nested/deploy.yml",
            ".agentic/config.yaml",
            "CODEOWNERS",
            "infra/prod/main.tf",
            "infra/dev/main.tf",
        ]
        violations = find_protected_path_violations(changed, patterns)
        assert violations == [
            ".agentic/config.yaml",
            ".github/workflows/ci.yml",
            ".github/workflows/nested/deploy.yml",
            "CODEOWNERS",
            "infra/prod/main.tf",
        ]


class TestWorkspaceManager:
    def test_checkout_branch_commit_push_and_cleanup(self, tmp_path: Path) -> None:
        bare = _create_bare_origin(
            tmp_path,
            config_yaml=(
                "git:\n"
                "  branch_prefix: agent/\n"
                "execution:\n"
                "  verify_commands:\n"
                "    - 'test -f feature.txt'\n"
            ),
        )
        manager = WorkspaceManager(base_dir=tmp_path / "workspaces")

        with manager.checkout(
            "waustinlynn/demo",
            issue_number=42,
            clone_url=str(bare),
        ) as ws:
            repo_dir = ws.repo_dir
            assert ws.branch == "agent/issue-42"
            assert repo_dir.is_dir()

            (ws.repo_dir / "feature.txt").write_text("hello\n", encoding="utf-8")
            results = ws.run_verify_commands()
            assert len(results) == 1 and results[0].passed

            sha = ws.commit_and_push("feat: add feature")
            assert sha is not None

        # Ephemeral workspace removed after context exit.
        assert not repo_dir.exists()
        # Branch exists on origin bare repository.
        branches = _git(["branch", "--list"], cwd=bare)
        assert "agent/issue-42" in branches

    def test_refuses_to_commit_or_push_protected_paths(self, tmp_path: Path) -> None:
        bare = _create_bare_origin(
            tmp_path,
            config_yaml="safety:\n  protected_paths:\n    - 'infra/prod/**'\n",
        )
        manager = WorkspaceManager(base_dir=tmp_path / "workspaces")

        with manager.checkout(
            "waustinlynn/demo",
            issue_number=7,
            clone_url=str(bare),
        ) as ws:
            wf = ws.repo_dir / ".github" / "workflows" / "evil.yml"
            wf.parent.mkdir(parents=True)
            wf.write_text("name: pwn\n", encoding="utf-8")

            prod = ws.repo_dir / "infra" / "prod" / "main.tf"
            prod.parent.mkdir(parents=True)
            prod.write_text("# changed\n", encoding="utf-8")

            with pytest.raises(ProtectedPathViolationError) as exc_info:
                ws.commit_and_push("chore: attempt protected edit")

            assert ".github/workflows/evil.yml" in exc_info.value.violations
            assert "infra/prod/main.tf" in exc_info.value.violations

        # Nothing pushed to origin.
        branches = _git(["branch", "--list"], cwd=bare)
        assert "agent/issue-7" not in branches

    def test_command_timeout_raises_command_execution_error(self, tmp_path: Path) -> None:
        bare = _create_bare_origin(
            tmp_path,
            config_yaml=(
                "execution:\n"
                "  verify_commands:\n"
                "    - 'python3 -c \"import time; time.sleep(5)\"'\n"
            ),
        )
        manager = WorkspaceManager(base_dir=tmp_path / "workspaces")

        with (
            manager.checkout("waustinlynn/demo", issue_number=9, clone_url=str(bare)) as ws,
            pytest.raises(CommandExecutionError) as exc_info,
        ):
            ws.run_verify_commands(timeout_per_command=0.1)

        assert exc_info.value.result.timed_out is True
