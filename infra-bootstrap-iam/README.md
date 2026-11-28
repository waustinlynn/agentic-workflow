# infra-bootstrap-iam

Minimal Terraform root that bootstraps **GitHub OIDC → AWS** deploys. It is deliberately separate from [`../infra`](../infra) and holds only:

- the GitHub OIDC identity provider (`token.actions.githubusercontent.com`)
- one shared deploy role (`github-actions-deploy`) and its permission policy

The role is shared by `waustinlynn/agentic-workflow` and `waustinlynn/monitor-grow` (account `367553824651`, `us-east-1`). No AWS access keys are stored in GitHub.

State lives in its own bucket: `agentic-iam-deploy-tfstate-367553824651-us-east-1`.

## Setup (one time, run locally with admin credentials)

1. **Prerequisites:** Terraform >= 1.10, AWS CLI, `gh` CLI. Confirm you are in the right account:
   ```bash
   aws sts get-caller-identity   # Account must be 367553824651
   ```
2. **Create the state bucket** (idempotent; versioned, encrypted, TLS-only, private) and generate `backend.hcl`:
   ```bash
   ./infra-bootstrap-iam/scripts/bootstrap-state.sh
   ```
3. **Configure trusted repos.** Copy and edit the tfvars (gitignored):
   ```bash
   cd infra-bootstrap-iam
   cp terraform.tfvars.example terraform.tfvars
   ```
   `github_subjects` lists OIDC `sub` patterns allowed to assume the role. Prefer narrow subjects:
   - `repo:OWNER/REPO:ref:refs/heads/main` — workflows on `main`
   - `repo:OWNER/REPO:environment:production` — jobs using the `production` GitHub Environment (use for `terraform apply`)
   - `repo:OWNER/REPO:pull_request` — PR runs (only add if PRs need AWS for `terraform plan`; consider a separate read-only role first, since this grants the deploy role to PR code)

   If your account already has a GitHub OIDC provider, set `create_oidc_provider = false` (only one per account is allowed).
4. **Apply:**
   ```bash
   terraform init -backend-config=backend.hcl
   terraform plan
   terraform apply
   terraform output deploy_role_arn
   ```
5. **Configure GitHub** in each repo (`agentic-workflow`, `monitor-grow`):
   ```bash
   gh variable set AWS_DEPLOY_ROLE_ARN --repo waustinlynn/agentic-workflow --body "$(terraform output -raw deploy_role_arn)"
   gh variable set AWS_REGION          --repo waustinlynn/agentic-workflow --body us-east-1
   ```
   Create a `production` Environment (Settings → Environments) with required reviewers, and restrict it to the `main` branch. Put any extra pipeline config in repo/environment variables or secrets (never commit it).
6. **Use in a workflow:**
   ```yaml
   permissions:
     id-token: write
     contents: read
   steps:
     - uses: aws-actions/configure-aws-credentials@v4
       with:
         role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
         aws-region: ${{ vars.AWS_REGION }}
   ```
7. **Verify** with a throwaway workflow that runs `aws sts get-caller-identity`; the ARN should be `assumed-role/github-actions-deploy/...`.

## Adding another repo

Append its subjects to `github_subjects`, add its name prefix to `resource_name_prefixes` (and its state bucket to `state_bucket_arns`), then `terraform apply`. Attach repo-specific permissions via `additional_policy_arns`.

## Security notes

- IAM role/policy management is limited to resources whose names start with `resource_name_prefixes`, and all service permissions are scoped to those prefixes. Review before widening: a role that can create IAM roles can escalate privileges.
- The trust policy requires `aud = sts.amazonaws.com` and a matching `sub`. Never use `repo:OWNER/*`.
- This root is applied by a human with admin credentials; the deploy role cannot modify itself (not matched by the prefixes, as long as `role_name` doesn't start with one).

## Teardown

```bash
terraform destroy
```
The state bucket is not managed by Terraform; empty and delete it manually if desired.
