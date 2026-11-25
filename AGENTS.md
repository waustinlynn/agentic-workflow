# AGENTS.md

Instructions and engineering rules for coding and review agents working on `waustinlynn/agentic-workflow`.

## Repository Layout

- `infra/` — Terraform infrastructure (S3-backed remote state, SQS FIFO + DLQ, DynamoDB, Lambda, Secrets Manager).
- `src/shared/` — Transport-agnostic Pydantic v2 models (`models.py`), `.agentic/config.yaml` loader (`config.py`), `agent:*` label definitions (`labels.py`), and 2-App GitHub auth/API client (`github_auth.py`, `github_client.py`).
- `src/ingest/` — AWS Lambda webhook ingestion, HMAC verification, allowlist enforcement, and repo-scoped SQS FIFO routing.
- `src/worker/` — Shared task execution core, Antigravity runner, Phase 1 local daemon, and Phase 2 Lambda handler.
- `src/reviewer/` — Option A Review Agent producing structured `ReviewResult` verdicts under `agentic-reviewer[bot]`.
- `prompts/` — Versioned prompt templates and output JSON schemas.
- `templates/` — Onboarding templates for external repositories (`.agentic/config.yaml`, `AGENTS.md`, workflows).
- `tests/` — Pytest unit and contract tests mirroring `src/`.

## Verification Commands

Run all checks before committing or opening a PR:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

## Coding & Architecture Rules

1. **Local-to-Lambda portability:** Code in `src/shared/`, `src/worker/`, and `src/reviewer/` must run identically on a local machine (Phase 1) and inside AWS Lambda (Phase 2). Never assume local-only persistence (use DynamoDB abstractions) or interactive OAuth.
2. **Dependency injection for I/O:** Inject HTTP clients, AWS SDK clients, clocks, and credential providers so core logic is 100% unit-testable without live network or AWS calls.
3. **Avoid over-abstraction:** Keep modules and functions focused, but do not introduce base classes, protocols, or factories unless there are at least two concrete implementations or an explicit test seam.
4. **Strict typing & validation:** All public functions and models must pass `mypy --strict` and validate external inputs with Pydantic v2 (`extra="forbid"` on user config schemas).
5. **Security on public GitHub:** Never commit secrets, PEM keys, or `.env` files. Never bypass HMAC verification, actor/repo allowlists, fork PR rejection, `agent:paused`, or `safety.protected_paths`.

## Testing Rules

1. **Bug fixes are test-first:** Reproduce bugs with a failing unit test first, verify it fails for the expected reason, then apply the minimal fix.
2. **Test observable behavior:** Assert public contracts, edge cases, and error paths rather than private implementation details.
3. **Never weaken tests:** Do not delete, skip, or relax existing tests just to make CI pass.

## Protected Paths

Agents must not modify these paths without explicit human approval:
- `.github/workflows/**`
- `.agentic/**`
- `CODEOWNERS`
