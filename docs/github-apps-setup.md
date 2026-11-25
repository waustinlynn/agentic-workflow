# GitHub Apps Setup Guide (2-App Architecture)

`agentic-workflow` uses **two separate GitHub Apps** under your GitHub account (`waustinlynn`):

1. **`agentic-coder`** — Receives webhooks, reads issues/CI, pushes branches, opens PRs, swaps `agent:*` labels, bootstraps labels on new repos, and enables native PR auto-merge.
2. **`agentic-reviewer`** — Separate bot identity used exclusively by the Option A Review Agent so GitHub accepts formal `APPROVE` and `REQUEST_CHANGES` verdicts on PRs authored by `agentic-coder[bot]`.

---

## Step 1: Gather Your Terraform Webhook URL & Generate a Webhook Secret

Run from the repo root:

```bash
cd infra && terraform output -raw webhook_url && echo
```

Generate a strong random webhook secret (save it temporarily in your password manager or shell variable; never commit it):

```bash
WEBHOOK_SECRET="$(openssl rand -hex 32)"
```

---

## Step 2: Register App 1 — `agentic-coder`

Open **GitHub Settings → Developer settings → GitHub Apps → New GitHub App** (`https://github.com/settings/apps/new`).

### Basic Information
- **GitHub App name:** `agentic-coder-waustinlynn` (GitHub App names are globally unique)
- **Homepage URL:** `https://github.com/waustinlynn/agentic-workflow`

### Webhook
- **Active:** Checked
- **Webhook URL:** `<output of terraform output -raw webhook_url>` (ends with `/webhook/github`)
- **Webhook secret:** Paste `$WEBHOOK_SECRET` from Step 1

### Repository Permissions
| Permission | Access |
|---|---|
| **Actions** | Read-only |
| **Checks** | Read-only |
| **Contents** | Read and write |
| **Issues** | Read and write |
| **Metadata** | Read-only (mandatory) |
| **Pull requests** | Read and write |

### Subscribe to Events
Check **only** these events:
- [x] `Check suite` (`check_suite`)
- [x] `Issues` (`issues`)
- [x] `Pull request` (`pull_request`)
- [x] `Pull request review` (`pull_request_review`)
- [x] `Installation repositories` (`installation_repositories` — selected automatically when the App can be installed on repos)

### Where can this GitHub App be installed?
- Select **Only on this account** (`waustinlynn`).

### Post-Creation
1. Click **Create GitHub App**.
2. Note the **App ID** (numeric) at the top of the settings page.
3. Scroll to **Private keys** → click **Generate a private key** → save the downloaded `.pem` file outside this repo (e.g., `~/.config/agentic-workflow/agentic-coder.pem`, `chmod 600`).
4. Under **Install App**, install it on your selected repositories (starting with `waustinlynn/agentic-workflow` or a test sandbox repo).

---

## Step 3: Register App 2 — `agentic-reviewer`

Open **New GitHub App** (`https://github.com/settings/apps/new`) again.

### Basic Information
- **GitHub App name:** `agentic-reviewer-waustinlynn`
- **Homepage URL:** `https://github.com/waustinlynn/agentic-workflow`

### Webhook
- **Active:** **Unchecked** (only `agentic-coder` sends webhooks so the Ingest Lambda never receives duplicate deliveries).

### Repository Permissions
| Permission | Access |
|---|---|
| **Checks** | Read-only |
| **Contents** | Read-only |
| **Issues** | Read-only |
| **Metadata** | Read-only (mandatory) |
| **Pull requests** | Read and write |

### Where can this GitHub App be installed?
- Select **Only on this account** (`waustinlynn`).

### Post-Creation
1. Click **Create GitHub App**.
2. Note the **App ID** (numeric).
3. Generate and download a private key (e.g., `~/.config/agentic-workflow/agentic-reviewer.pem`, `chmod 600`).
4. Under **Install App**, install it on the **same repositories** as `agentic-coder`.

---

## Step 4: Upload Secrets to AWS Secrets Manager

Use the helper script (does not log secret values and validates PEM files before uploading):

```bash
./scripts/upload_github_secrets.sh \
  --region us-east-1 \
  --prefix agentic-workflow-prod \
  --webhook-secret "$WEBHOOK_SECRET" \
  --coder-app-id "<CODER_APP_ID>" \
  --coder-pem ~/.config/agentic-workflow/agentic-coder.pem \
  --reviewer-app-id "<REVIEWER_APP_ID>" \
  --reviewer-pem ~/.config/agentic-workflow/agentic-reviewer.pem
```

> Note: Remember to update `allowed_actors` in `infra/terraform.tfvars` if your GitHub App slug differs from `agentic-coder` / `agentic-reviewer` (for example, `agentic-coder-waustinlynn[bot]` and `agentic-reviewer-waustinlynn[bot]`), then re-run `terraform apply`.

---

## Step 5: Verify Webhook `ping` Returns `200 OK`

1. In GitHub → **Developer settings → GitHub Apps → `agentic-coder` → Advanced → Recent Deliveries**.
2. Click the initial `ping` delivery (or click **Redeliver**).
3. Confirm the response status is **`200 OK`** with body:
   ```json
   {"status": "ok", "event": "ping", "delivery_id": "..."}
   ```
4. Optionally test an invalid signature locally to verify `401 Unauthorized`:
   ```bash
   curl -i -X POST "$(cd infra && terraform output -raw webhook_url)" \
     -H "Content-Type: application/json" \
     -H "X-GitHub-Event: ping" \
     -H "X-GitHub-Delivery: bad-sig-test" \
     -H "X-Hub-Signature-256: sha256=0000" \
     -d '{}'
   ```
