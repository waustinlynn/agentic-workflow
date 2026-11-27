# Task: Issue Triage & Actionability Verification (`triage`)

Inspect the GitHub issue in `<github_issue>` (plus any `<github_comments>`) against the actual repository codebase (**read-only**—do not edit files, commit, or push changes).

## Triage Goals

1. **Verify validity against the codebase:**
   - Confirm whether the described bug, gap, or feature request actually applies to the current code on the default branch.
   - Check whether the referenced files, symbols, or behaviors exist and whether the issue is already implemented or resolved.
2. **Verify actionability:**
   - Check that the goal, expected behavior, and acceptance criteria are clear enough for an autonomous coding or planning agent to implement without guessing product intent.
   - Check whether completing the issue requires modifying any **Protected Paths** (`protected_paths`), secrets, credentials, or external manual steps that an autonomous worker cannot perform.
3. **Choose exactly one verdict:**
   - **`ready`**: The issue is **valid, clearly specified, and actionable**, and small enough to complete cleanly in a single coding turn / Lambda execution window (typically <= 10 minutes, focused on a well-defined set of files/tests).
   - **`plan`**: The issue is **valid and actionable**, but large, multi-component, or complex enough that it should be decomposed into atomic sub-issues via `agent:plan` before coding.
   - **`needs_human`**: The issue is **not valid or not currently actionable**—for example, it is already fixed/implemented, cannot be verified from the repo, lacks clear requirements or reproduction details, has unresolved architectural/product ambiguity, or requires touching `protected_paths` or manual external setup.
     - Clearly state **why** it is not actionable in `rationale`, and list concrete questions or information needed in `missing_details`.

## Output Format

Return ONLY valid JSON matching the `TriageResult` schema.
