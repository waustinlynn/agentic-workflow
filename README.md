# agentic-workflow

A GitHub webhook-driven agentic software development framework powered by AWS (Lambda + SQS FIFO + DynamoDB) and Google Antigravity.

## Architecture Overview

1. **Trigger:** An `agent:*` label (such as `agent:plan` or `agent:ready`) or PR review/CI event fires a GitHub webhook.
2. **Ingest (AWS Lambda):** Verifies HMAC signatures, enforces public-repo allowlists and safety checks, maps `repository.full_name` to a deterministic SQS `MessageGroupId` (sequential per repo, parallel across repos), and enqueues a `TaskEnvelope` on an SQS FIFO queue.
3. **Execute (Phase 1 Local Daemon -> Phase 2 AWS Lambda):** Polls SQS, loads the target repo's `.agentic/config.yaml` and `AGENTS.md`, runs Google Antigravity with structured prompt templates, verifies locally or via CI, and pushes/opens a PR as `agentic-coder[bot]`.
4. **Review (Option A Review Agent):** Triggered through the same pipeline under a second GitHub App identity (`agentic-reviewer[bot]`) so GitHub accepts formal `APPROVE` and `REQUEST_CHANGES` verdicts.
5. **Merge & Deploy:** `REQUEST_CHANGES` loops back to the coding agent (capped by `max_review_rounds` -> `agent:needs-human`). `APPROVE` + passing CI triggers native GitHub auto-merge and the repo's deploy workflow.

## Repository Layout

```text
infra/          Terraform (S3 backend) for AWS SQS FIFO, DynamoDB, Lambda, Secrets Manager
src/
  shared/       Shared Pydantic v2 models, config loader, GitHub App auth & API client
  ingest/       Webhook Ingest AWS Lambda
  worker/       Shared worker core, Antigravity driver, Phase 1 local daemon & Phase 2 Lambda entrypoint
  reviewer/     Option A PR Review Agent
prompts/        Prompt templates (base_rules, plan, code_and_test, review, address_review, fix_ci)
templates/      Universal repo onboarding kit (.agentic/config.yaml, AGENTS.md, CI/deploy workflows)
tests/          Unit and contract test suite
```

## Local Development

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

## Roadmap

Tracked via GitHub Issues (Epics 1–7):
- **Epic 1 (#1):** Foundation, repo layout, shared Pydantic contracts, 2-App GitHub client
- **Epic 2 (#5):** AWS Terraform infrastructure with S3 remote state
- **Epic 3 (#9):** GitHub Apps & Webhook Ingest Lambda
- **Epic 4 (#14):** Prompt templates & Antigravity execution driver
- **Epic 5 (#18):** Phase 1 local worker daemon & Option A Review Agent
- **Epic 6 (#23):** Universal repo onboarding kit & E2E validation
- **Epic 7 (#27):** Phase 2 AWS Lambda container workers & polyglot runtimes
