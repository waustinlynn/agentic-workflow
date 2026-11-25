#!/usr/bin/env bash
set -euo pipefail

# Upload GitHub webhook secret and both GitHub App credentials to AWS Secrets Manager.
# Validates inputs locally without echoing secret material.

AWS_REGION="${AWS_REGION:-us-east-1}"
PREFIX="${SECRET_PREFIX:-agentic-workflow-prod}"
WEBHOOK_SECRET=""
CODER_APP_ID=""
CODER_PEM=""
REVIEWER_APP_ID=""
REVIEWER_PEM=""

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --region <region>            AWS region (default: us-east-1)
  --prefix <prefix>            Secrets Manager prefix (default: agentic-workflow-prod)
  --webhook-secret <secret>    GitHub webhook HMAC secret
  --coder-app-id <id>          Numeric App ID for agentic-coder
  --coder-pem <path>           Path to agentic-coder PEM private key file
  --reviewer-app-id <id>       Numeric App ID for agentic-reviewer
  --reviewer-pem <path>        Path to agentic-reviewer PEM private key file
  -h, --help                   Show this help text

You can pass any subset of the three secrets to update only those entries.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) AWS_REGION="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --webhook-secret) WEBHOOK_SECRET="$2"; shift 2 ;;
    --coder-app-id) CODER_APP_ID="$2"; shift 2 ;;
    --coder-pem) CODER_PEM="$2"; shift 2 ;;
    --reviewer-app-id) REVIEWER_APP_ID="$2"; shift 2 ;;
    --reviewer-pem) REVIEWER_PEM="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

upload_app_secret() {
  local secret_id="$1"
  local app_id="$2"
  local pem_path="$3"

  if [[ ! -f "${pem_path}" ]]; then
    echo "Error: PEM file not found: ${pem_path}" >&2
    exit 1
  fi

  local payload
  payload="$(python3 -c '
import json, pathlib, sys
app_id, pem_path = sys.argv[1], pathlib.Path(sys.argv[2])
pem = pem_path.read_text(encoding="utf-8").strip()
if "PRIVATE KEY" not in pem:
    raise SystemExit(f"File does not look like a PEM private key: {pem_path}")
print(json.dumps({"app_id": app_id, "private_key": pem}))
' "${app_id}" "${pem_path}")"

  aws secretsmanager put-secret-value \
    --region "${AWS_REGION}" \
    --secret-id "${secret_id}" \
    --secret-string "${payload}" >/dev/null
  echo "Updated secret: ${secret_id}"
}

UPDATED=0

if [[ -n "${WEBHOOK_SECRET}" ]]; then
  aws secretsmanager put-secret-value \
    --region "${AWS_REGION}" \
    --secret-id "${PREFIX}/github-webhook-secret" \
    --secret-string "${WEBHOOK_SECRET}" >/dev/null
  echo "Updated secret: ${PREFIX}/github-webhook-secret"
  UPDATED=1
fi

if [[ -n "${CODER_APP_ID}" || -n "${CODER_PEM}" ]]; then
  if [[ -z "${CODER_APP_ID}" || -z "${CODER_PEM}" ]]; then
    echo "Error: --coder-app-id and --coder-pem must be provided together." >&2
    exit 2
  fi
  upload_app_secret "${PREFIX}/github-coder-app" "${CODER_APP_ID}" "${CODER_PEM}"
  UPDATED=1
fi

if [[ -n "${REVIEWER_APP_ID}" || -n "${REVIEWER_PEM}" ]]; then
  if [[ -z "${REVIEWER_APP_ID}" || -z "${REVIEWER_PEM}" ]]; then
    echo "Error: --reviewer-app-id and --reviewer-pem must be provided together." >&2
    exit 2
  fi
  upload_app_secret "${PREFIX}/github-reviewer-app" "${REVIEWER_APP_ID}" "${REVIEWER_PEM}"
  UPDATED=1
fi

if [[ "${UPDATED}" -eq 0 ]]; then
  echo "Nothing to upload. Provide at least one secret option." >&2
  usage >&2
  exit 2
fi

echo "Done."
