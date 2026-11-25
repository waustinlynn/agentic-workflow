# Task: Issue Decomposition & Implementation Plan (`plan`)

Analyze the GitHub issue in `<github_issue>` and inspect the repository codebase (**read-only**—do not edit files or commit changes).

## Planning Goals

1. Break the issue into small, atomic, independently verifiable sub-issues designed to complete well inside a single AWS Lambda execution window (**target <= 5–10 minutes per sub-issue**).
2. For each sub-issue, specify:
   - Concrete **title** and **description**
   - Testable **acceptance criteria**
   - Expected **files / module boundaries** to create or modify (respecting `protected_paths`)
   - Specific **test cases** to write first or alongside implementation
   - Rough **estimated_minutes** (1–12)
3. Surface any ambiguities, architectural trade-offs, or protected-path requirements in `open_questions` so a human can resolve them before labeling `agent:ready`.

## Output Format

Return ONLY valid JSON matching the `PlanResult` schema.
