#!/usr/bin/env bash
set -euo pipefail

# Print a JSON map {runtime: ImageUri} of the worker Lambda images currently deployed.
# Used so `terraform plan` on PRs does not show spurious image changes.
# Usage: PROJECT_PREFIX=agentic-workflow-prod ./scripts/deployed_image_uris.sh [runtime ...]

PROJECT_PREFIX="${PROJECT_PREFIX:-agentic-workflow-prod}"
RUNTIMES=("$@")
if [[ ${#RUNTIMES[@]} -eq 0 ]]; then
  RUNTIMES=(base python node infra)
fi

uris='{}'
for runtime in "${RUNTIMES[@]}"; do
  if uri="$(aws lambda get-function --function-name "${PROJECT_PREFIX}-worker-${runtime}" \
    --query 'Code.ImageUri' --output text 2>/dev/null)" && [[ -n "${uri}" && "${uri}" != "None" ]]; then
    uris="$(jq -c --arg k "${runtime}" --arg v "${uri}" '. + {($k): $v}' <<<"${uris}")"
  fi
done
echo "${uris}"
