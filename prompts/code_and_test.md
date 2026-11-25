# Task: Code and Test (`code_and_test`)

Implement the GitHub issue described in `<github_issue>` (and any clarifying `<github_comments>`) in the current repository workspace.

## Execution Steps

1. Inspect the relevant files and existing tests to understand repository patterns and conventions.
2. Check whether the issue is a bug fix or a feature/change:
   - **Bug fix:** Write a failing test reproducing the bug first, verify it fails for the expected reason, then implement the minimal fix.
   - **Feature / refactor:** Implement clean, focused changes adhering to the repo's `AGENTS.md` and add/update tests covering acceptance criteria and key edge cases.
3. Run all configured `setup_commands` (if dependencies are not already installed) and all `verify_commands`.
4. Review `git status` and `git diff` to verify:
   - Zero protected paths were touched.
   - No debug leftovers, untracked scratch files, or unrelated edits remain.
   - All verification commands pass cleanly.
5. Produce a concise commit message following the repository's `commit_style` and a clear PR title/body referencing the issue number.

## Output Format

Return ONLY valid JSON matching the `CodeTaskResult` schema. Set `blocked_reason` to `null` on success, or explain clearly why human intervention is required if blocked.
