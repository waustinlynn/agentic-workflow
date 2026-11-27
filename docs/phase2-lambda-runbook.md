# Phase 2 AWS Lambda Container Workers — Rollout & Cloud Validation Runbook

This runbook walks through deploying the Phase 2 serverless Lambda container workers (`worker-base`, `worker-python`, `worker-node`, `worker-infra`) and verifying an end-to-end cloud execution loop with your local machine turned off.

## Architecture Overview

- **Webhook Ingest Lambda** reads `execution_target` and `runtime` from DynamoDB `repo-registry`:
  - `execution_target = "local"` -> routes to `agentic-workflow-prod-tasks.fifo` (Phase 1 local runner).
  - `execution_target = "lambda"`:
    - `review` and `plan` tasks -> always route to `worker-base.fifo` (lightweight image, read-only).
    - `runtime = "base"` or `runtime = "ci-verified"` -> routes to `worker-base.fifo`.
    - `runtime = "python" | "node" | "infra" | ...` -> routes to `worker-<runtime>.fifo` (falling back to `worker-base.fifo` if a stack queue is not provisioned).
- **Soft-Timeout Guard:** `src/worker/lambda_handler.py` reads `context.get_remaining_time_in_millis()` and subtracts `LAMBDA_SOFT_TIMEOUT_BUFFER_SECONDS` (default `120s` before the `900s` Lambda hard limit). If a task runs out of budget, `handle_task()` catches `TaskDeadlineExceededError` / `AgentTimeoutError`, labels the issue/PR `agent:needs-human`, posts a diagnostic comment, and persists `RunStatus.NEEDS_HUMAN` in DynamoDB cleanly instead of letting Lambda hard-kill the container.

## Step 1 — Provision ECR Repositories & Populate Headless API Key

```bash
# 1. Ensure ECR repositories exist ($0 idle cost when empty):
./infra/scripts/build-ingest-lambda.sh
cd infra
terraform apply

# 2. Populate the headless Antigravity/Gemini API key out-of-band:
aws secretsmanager put-secret-value \
  --secret-id agentic-workflow-prod/antigravity-api-key \
  --secret-string "<YOUR_API_KEY>"
```

## Step 2 — Build & Push Container Images to ECR

Match `--platform` to `lambda_worker_architecture` (`linux/arm64` by default, or `linux/amd64` if `lambda_worker_architecture = "x86_64"`):

```bash
AWS_REGION=us-east-1 ./scripts/build_and_push_workers.sh \
  --push \
  --platform linux/arm64 \
  --tag v0.1.0 \
  --sdk-package google-antigravity \
  --runtimes "base python node infra"
```

## Step 3 — Enable Phase 2 Lambda Workers in Terraform

Add to your gitignored `infra/terraform.tfvars`:

```hcl
enable_lambda_workers      = true
lambda_worker_image_tag    = "v0.1.0"
lambda_worker_architecture = "arm64"
lambda_worker_agent_backend = "sdk"
```

Apply Terraform:

```bash
cd infra
terraform plan
terraform apply
```

This provisions:
- `agentic-workflow-prod-worker-{base,python,node,infra}.fifo` SQS FIFO queues (`visibility_timeout = 960s`, redriving to the shared DLQ after 2 attempts)
- `agentic-workflow-prod-worker-{base,python,node,infra}` Lambda container functions (3008 MB RAM, 2048 MB `/tmp`, 900s hard timeout, 120s soft-timeout buffer)
- SQS Event Source Mappings (`batch_size = 1`, `ReportBatchItemFailures`, `maximum_concurrency = 5`)
- Updated `SQS_RUNTIME_QUEUE_URLS` on the Webhook Ingest Lambda

## Step 4 — Switch a Repository from Local to Lambda

No code changes are required — update the repository's DynamoDB Registry item using the onboarding CLI:

```bash
# Switch to Python in-Lambda verification:
uv run agentic onboard waustinlynn/<repo> --target lambda --runtime python

# Or switch to CI-verified mode on worker-base (works for any language):
uv run agentic onboard waustinlynn/<repo> --target lambda --runtime ci-verified
```

## Step 5 — End-to-End Cloud Validation Checklist (Issue #30)

Ensure the Phase 1 `agentic-local-runner` daemon is **stopped**, then verify:

1. **Plan in Lambda (`worker-base`):** Apply `agent:plan` to a feature issue -> verify `worker-base` posts structured sub-issues and removes `agent:plan`.
2. **Code & Verify in Lambda (`worker-python` / `worker-node` / `worker-infra`):** Apply `agent:ready` to a small sub-issue -> verify Lambda runs `verify_commands` cleanly using `/tmp` caches, pushes `agent/issue-<N>`, opens a PR, and enables auto-merge.
3. **CI-Verified Mode (`worker-base`):** On a repo with `runtime: ci-verified`, apply `agent:ready` -> verify `worker-base` opens a PR, GitHub Actions runs CI, and a simulated CI failure triggers `fix_ci` on `worker-base`.
4. **Review & Merge (`worker-base`):** When CI turns green -> verify `worker-base` runs `agentic-reviewer[bot]`, submits `REQUEST_CHANGES` or `APPROVE`, and `APPROVE` + green CI triggers native auto-merge.
5. **Latency & Soft-Timeout Check:** Inspect CloudWatch Logs (`/aws/lambda/agentic-workflow-prod-worker-*`) to confirm typical invocations finish in 2–6 minutes (well below the 15-minute ceiling) and that an intentionally low `LAMBDA_SOFT_TIMEOUT_BUFFER_SECONDS` / short deadline cleanly escalates to `agent:needs-human`.
