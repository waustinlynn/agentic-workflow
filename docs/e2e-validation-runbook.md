# End-to-End Live Validation Runbook (`#26`)

Use this checklist to validate the complete closed-loop pipeline on a small public sandbox repository under `waustinlynn` (for example, `waustinlynn/agentic-sandbox`).

---

## 1. Create & Onboard the Sandbox Repo

1. Create a small public Python or Node repo (e.g., `waustinlynn/agentic-sandbox`) with a trivial function + unit test, `.agentic/config.yaml`, `AGENTS.md`, `.github/workflows/ci.yml`, and `.github/workflows/deploy.yml`.
2. Enable **Allow auto-merge** and protect `main` (require 1 approval + `verify` CI check).
3. Install both `agentic-coder` and `agentic-reviewer` GitHub Apps on the sandbox repo.
4. Run readiness onboarding:
   ```bash
   uv run agentic onboard waustinlynn/agentic-sandbox \
     --target local \
     --runtime python
   ```
   Confirm `Result: READY` (all 7 checks `[PASS]`).

## 2. Start the Phase 1 Local Runner

```bash
export AWS_REGION=us-east-1
export SQS_QUEUE_URL="$(cd infra && terraform output -raw sqs_tasks_queue_url)"
export RUN_STATE_TABLE="$(cd infra && terraform output -raw dynamodb_run_state_table)"
export GITHUB_CODER_SECRET_ARN="$(cd infra && terraform output -json secret_arns | jq -r .github_coder_app)"
export GITHUB_REVIEWER_SECRET_ARN="$(cd infra && terraform output -json secret_arns | jq -r .github_reviewer_app)"
export AGY_AUTO_APPROVE=true

uv run agentic-local-runner
```

## 3. Closed-Loop Happy Path + Review Iteration

1. Open issue `#1` in `waustinlynn/agentic-sandbox` (e.g., *"Add a `subtract(a, b)` helper and unit tests"*) and apply **`agent:ready`**.
2. Verify:
   - Webhook Ingest Lambda logs `Enqueued task=code_and_test` with `MessageGroupId=repo-waustinlynn-agentic-sandbox`.
   - Local runner transitions label `agent:ready` → `agent:in-progress` → pushes `agent/issue-1` → opens PR → transitions to `agent:in-review`.
   - GitHub Actions `CI` runs and completes green → `check_suite.completed` enqueues `review`.
   - `agentic-reviewer[bot]` submits a formal PR review.
3. Verify **`REQUEST_CHANGES` → fix loop** (either from reviewer feedback or by submitting a human `Request changes` review as `waustinlynn` asking for one small edge-case test):
   - Webhook enqueues `address_review`.
   - Coding agent pushes a follow-up commit and comments summarizing the fix.
   - CI turns green → `agentic-reviewer[bot]` submits **`APPROVE`**.
   - GitHub native auto-merge merges the PR into `main` → `deploy.yml` executes automatically.

## 4. Security Guardrails & Circuit Breaker Check

1. **`agent:paused` kill switch:** Create issue `#2`, add both `agent:paused` and `agent:ready` → verify Ingest Lambda logs `ignored (issue is paused via agent:paused)` and zero work is enqueued.
2. **Max-rounds circuit breaker:** Set `execution.max_review_rounds: 1` (or seed `attempt_count = 3` in DynamoDB), trigger a review change request → verify worker halts immediately, applies `agent:needs-human`, and posts an escalation summary.
3. **External fork / untrusted actor:** Confirm unauthorized actors and fork PRs are ignored at Ingest Lambda (also covered automatically by `tests/test_ingest.py` and `tests/test_e2e_closed_loop.py`).
