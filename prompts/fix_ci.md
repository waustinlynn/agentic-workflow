# Task: Fix Failing CI (`fix_ci`)

CI checks failed on the pull request branch. Inspect the failing logs in `<ci_logs>` and the current PR diff in `<pr_diff>`.

## Execution Steps

1. Identify the exact root cause of every CI failure (lint, formatting, type errors, failing unit/integration tests, build errors).
2. Fix the underlying bug or formatting/type error directly.
   - **Never** disable linters, add `# type: ignore` blindly, or delete/skip/weaken failing tests just to make CI green.
3. Run all configured `verify_commands` locally (if available in the current runtime) to confirm the fix before finishing.
4. Verify no `protected_paths` were modified.

## Output Format

Return ONLY valid JSON matching the `FixCIResult` schema. Set `blocked_reason` to `null` on success, or explain clearly if the failure stems from external infrastructure or a protected workflow file that requires human intervention.
