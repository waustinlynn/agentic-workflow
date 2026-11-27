# Prompt Templates & Output Schemas

Versioned prompt templates and Pydantic-aligned JSON schemas for every stage of `agentic-workflow`:

- `base_rules.md` — Universal trust boundaries, protected-path rules, and engineering/testing standards.
- `triage.md` + `schemas/triage_result.schema.json` — Read-only issue validity, actionability, and complexity triage (`TriageResult`).
- `plan.md` + `schemas/plan_result.schema.json` — Read-only decomposition into atomic Lambda-sized sub-issues (`PlanResult`).
- `code_and_test.md` + `schemas/code_task_result.schema.json` — Implementation, test-first bug fixes, and verification (`CodeTaskResult`).
- `review.md` + `schemas/review_result.schema.json` — Independent Option A review under `agentic-reviewer[bot]` (`ReviewResult`).
- `address_review.md` + `schemas/address_review_result.schema.json` — Targeted fixes for `REQUEST_CHANGES` review feedback (`AddressReviewResult`).
- `fix_ci.md` + `schemas/fix_ci_result.schema.json` — Root-cause fixes for failing GitHub Actions CI runs (`FixCIResult`).

All schemas in `prompts/schemas/` are verified 1:1 against the Pydantic v2 models in `src/shared/models.py` by `tests/test_prompts.py`.
