# Onboarding Any Repository to `agentic-workflow`

Any repository—frontend, backend, mobile, or infrastructure—can onboard in ~5 minutes without modifying any framework code.

---

## Step 1: Copy a Starter Kit into Your Target Repository

Pick the closest stack under `templates/`:
- `templates/frontend/.agentic/config.yaml`
- `templates/backend/.agentic/config.yaml`
- `templates/mobile/.agentic/config.yaml`
- `templates/infra/.agentic/config.yaml`

Copy into the target repository:

```bash
mkdir -p <target-repo>/.agentic <target-repo>/.github/workflows
cp templates/<stack>/.agentic/config.yaml <target-repo>/.agentic/config.yaml
cp templates/AGENTS.md <target-repo>/AGENTS.md
cp templates/workflows/ci.yml <target-repo>/.github/workflows/ci.yml
cp templates/workflows/deploy.yml <target-repo>/.github/workflows/deploy.yml
```

Customize:
1. `.agentic/config.yaml`: set `setup_commands`, `verify_commands`, `review.auto_merge`, and any repo-specific `review.extra_checklist` or `safety.protected_paths`.
2. `AGENTS.md`: describe your repo's directory layout, architecture conventions, and test standards.
3. `.github/workflows/ci.yml` and `deploy.yml`: wire your stack's CI checks and deployment target.

Commit and push these files to `main` (agents cannot edit `.github/workflows/**` or `.agentic/**` themselves).

---

## Step 2: Configure GitHub Repository Settings

In your target repository on GitHub:
1. **Enable Native Auto-Merge:** *Settings → General → Pull Requests → Allow auto-merge* (checked).
2. **Configure Branch Protection on `main`:**
   - Require a pull request before merging
   - Require **1 approving review** (satisfied by `agentic-reviewer[bot]`)
   - Require status checks to pass before merging → select `verify` (from `ci.yml`)
   - *(Recommended)* Dismiss stale pull request approvals when new commits are pushed
3. **Install Both GitHub Apps:**
   - Install **`agentic-coder`** on the repository
   - Install **`agentic-reviewer`** on the repository

---

## Step 3: Run `agentic onboard`

From this repository (with AWS credentials active and either `GITHUB_CODER_SECRET_ARN` + `GITHUB_REVIEWER_SECRET_ARN` or local `GITHUB_*` env vars set):

```bash
uv run agentic onboard waustinlynn/<target-repo> \
  --target local \
  --runtime python \
  --registry-table agentic-workflow-prod-repo-registry \
  --region us-east-1
```

What this does idempotently:
1. Verifies both `agentic-coder` and `agentic-reviewer` GitHub Apps are installed.
2. Registers `waustinlynn/<target-repo>` in DynamoDB `agentic-workflow-prod-repo-registry` with a deterministic SQS FIFO `MessageGroupId` (`repo-waustinlynn-<target-repo>`).
3. Bootstraps all 7 standard `agent:*` labels (`agent:plan`, `agent:ready`, `agent:in-progress`, `agent:in-review`, `agent:changes-requested`, `agent:needs-human`, `agent:paused`).
4. Fetches and validates `.agentic/config.yaml` against `RepoConfig`.
5. Checks that native auto-merge and branch protection on `main` are enabled.

When every check prints `[PASS]` and `Result: READY`, your repository is live!

---

## Step 4: Trigger Your First Autonomous Task

1. Create a scoped GitHub issue describing the task and acceptance criteria.
2. Add the **`agent:ready`** label (or **`agent:plan`** first to decompose a larger issue).
3. Start the Phase 1 local runner if not already running:
   ```bash
   SQS_QUEUE_URL="$(cd infra && terraform output -raw sqs_tasks_queue_url)" \
   RUN_STATE_TABLE="$(cd infra && terraform output -raw dynamodb_run_state_table)" \
   GITHUB_CODER_SECRET_ARN="$(cd infra && terraform output -json secret_arns | jq -r .github_coder_app)" \
   GITHUB_REVIEWER_SECRET_ARN="$(cd infra && terraform output -json secret_arns | jq -r .github_reviewer_app)" \
   uv run agentic-local-runner
   ```
4. Watch the closed loop:
   `agent:ready` → `agent:in-progress` → PR opened + `agent:in-review` → CI passes → `agentic-reviewer[bot]` reviews → (`REQUEST_CHANGES` → fix loop →) `APPROVE` → auto-merge to `main` → `deploy.yml`.
