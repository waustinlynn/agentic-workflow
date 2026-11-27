# Phase 2 AWS Lambda Worker Container Images

Layered AWS Lambda container images built from `public.ecr.aws/lambda/python:3.12`. Every image executes the same `worker.lambda_handler.lambda_handler` entrypoint, which invokes the shared `handle_task()` core.

## Image Hierarchy

| Image | Dockerfile | Purpose | Writable `/tmp` Caches |
|---|---|---|---|
| `worker-base` | `docker/Dockerfile.base` | `review`, `plan`, and `ci-verified` coding for any language | `HOME`, `XDG_*`, `AGENTIC_WORKSPACE_DIR=/tmp/agentic-workspaces` |
| `worker-python` | `docker/Dockerfile.python` | Python repos (`uv`, `ruff`, `mypy`, `pytest`) | `UV_CACHE_DIR`, `RUFF_CACHE_DIR`, `MYPY_CACHE_DIR`, `PYTEST_CACHE_DIR` |
| `worker-node` | `docker/Dockerfile.node` | TypeScript/JS repos (Node 22 LTS, `npm`, `pnpm`) | `NPM_CONFIG_CACHE`, `YARN_CACHE_FOLDER`, `PNPM_STORE_DIR` |
| `worker-infra` | `docker/Dockerfile.infra` | Terraform / IaC repos (`terraform`, `jq`) | `TF_DATA_DIR`, `TF_PLUGIN_CACHE_DIR` |
| `worker-dotnet` | `docker/Dockerfile.dotnet` | .NET repos (`dotnet` SDK 8.0) | `DOTNET_CLI_HOME`, `NUGET_PACKAGES` |
| `worker-rust` | `docker/Dockerfile.rust` | Rust repos (`cargo`, `clippy`, `rustfmt`) | `CARGO_HOME`, `CARGO_TARGET_DIR` |

## Adding a New Runtime Stack

1. Create `docker/Dockerfile.<stack>` starting with:
   ```dockerfile
   ARG BASE_IMAGE=agentic-worker-base:latest
   FROM ${BASE_IMAGE}
   ```
2. Install only the stack's CLI toolchain and point all runtime cache directories to `/tmp`.
3. Add `<stack>` to `lambda_worker_runtimes` in `infra/terraform.tfvars` (and `RuntimeKind` in `src/shared/models.py` if new).
4. Build and push with `./scripts/build_and_push_workers.sh --push --runtimes "base <stack>"`.

## Building & Pushing to ECR

```bash
# Local build only (defaults to host arch, builds base/python/node/infra):
./scripts/build_and_push_workers.sh

# Build for Lambda architecture and push to ECR:
AWS_REGION=us-east-1 ./scripts/build_and_push_workers.sh \
  --push \
  --platform linux/arm64 \
  --tag v0.1.0 \
  --runtimes "base python node infra"
```

### Packaging Headless Antigravity Auth / Runtime

- **Python SDK (`AGENT_BACKEND=sdk`):** pass `--sdk-package google-antigravity` to bake the headless Python SDK into `worker-base`.
- **`agy` CLI (`AGENT_BACKEND=agy_cli`):** pass `--agy-binary-url <url>` (or copy an architecture-matched `agy` binary into `/usr/local/bin/agy`).
- **API Key:** stored out-of-band in AWS Secrets Manager (`agentic-workflow-prod/antigravity-api-key`) and loaded automatically by `worker.lambda_handler` via `ANTIGRAVITY_API_KEY_SECRET_ARN`.
