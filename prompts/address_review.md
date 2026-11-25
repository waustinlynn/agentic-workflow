# Task: Address PR Review Feedback (`address_review`)

A reviewer requested changes on the active pull request. Review the feedback in `<review_feedback>` and `<github_comments>`, along with the current diff in `<pr_diff>`.

## Execution Steps

1. Read every `blocker` and `warning` review item carefully.
2. Address each requested change cleanly and minimally—do **not** introduce unrelated refactoring or scope creep.
3. Add or strengthen tests covering every fixed issue so regressions cannot recur.
4. Run all configured `verify_commands` and confirm they pass cleanly.
5. Verify `git diff` touches no `protected_paths`.
6. Summarize how each review item was addressed so replies can be posted to the PR.

## Output Format

Return ONLY valid JSON matching the `AddressReviewResult` schema. Set `blocked_reason` to `null` on success, or explain clearly if a reviewer request conflicts with protected-path rules or needs human clarification.
