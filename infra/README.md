# Infrastructure (Terraform + S3 Backend)

Provisions the AWS control plane for `agentic-workflow` with near-$0 idle cost:
- **S3 Remote State** with native S3 locking (`use_lockfile = true`, Terraform >= 1.10)
- **SQS FIFO + DLQ** (`agentic-workflow-prod-tasks.fifo`, `maxReceiveCount = 2`, long polling)
- **DynamoDB (PAY_PER_REQUEST)**: `run-state` and `repo-registry` (optional `initial_repos` seeding)
- **Secrets Manager** placeholders (values populated out-of-band; never committed to git or Terraform state)
- **Webhook Ingest Lambda** (`python3.12`, `arm64`) behind an **API Gateway HTTP API** (`POST /webhook/github` with edge throttling)
- **Least-privilege IAM** roles/policies for the Ingest Lambda and Phase 1 local runner

## 1. One-Time S3 Remote State Bootstrap

```bash
AWS_REGION=us-east-1 ./infra/scripts/bootstrap-state.sh
```

This creates an encrypted, versioned, public-access-blocked, TLS-only S3 bucket (`agentic-workflow-tfstate-<ACCOUNT_ID>-us-east-1`) and generates a gitignored `infra/backend.hcl`.

## 2. Build Lambda Package, Initialize, Plan, and Apply

```bash
./infra/scripts/build-ingest-lambda.sh
cd infra
terraform init -backend-config=backend.hcl
terraform fmt -check -recursive
terraform validate
terraform plan
terraform apply
```

To seed initial repositories into `repo-registry`, copy `terraform.tfvars.example` to `terraform.tfvars` (gitignored) and customize `initial_repos`.

## 3. Populate Secrets Out-of-Band

Secret containers are created empty by Terraform. Populate them directly via AWS CLI:

```bash
# 1. Webhook HMAC secret (generate a random 32-byte hex secret and paste the same value in GitHub App settings)
aws secretsmanager put-secret-value \
  --secret-id agentic-workflow-prod/github-webhook-secret \
  --secret-string "<WEBHOOK_SECRET>"

# 2. agentic-coder GitHub App credentials
aws secretsmanager put-secret-value \
  --secret-id agentic-workflow-prod/github-coder-app \
  --secret-string "$(jq -n --arg id "<CODER_APP_ID>" --rawfile key /path/to/coder.pem '{app_id:$id, private_key:$key}')"

# 3. agentic-reviewer GitHub App credentials
aws secretsmanager put-secret-value \
  --secret-id agentic-workflow-prod/github-reviewer-app \
  --secret-string "$(jq -n --arg id "<REVIEWER_APP_ID>" --rawfile key /path/to/reviewer.pem '{app_id:$id, private_key:$key}')"

# 4. Headless Antigravity/Gemini API key (for Phase 2 Lambda workers)
aws secretsmanager put-secret-value \
  --secret-id agentic-workflow-prod/antigravity-api-key \
  --secret-string "<API_KEY>"
```
