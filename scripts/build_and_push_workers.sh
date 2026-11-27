#!/usr/bin/env bash
set -euo pipefail

# Build (and optionally push to AWS ECR) Phase 2 Lambda worker container images.
# Usage:
#   ./scripts/build_and_push_workers.sh [--push] [--region us-east-1] [--tag latest] \
#       [--platform linux/amd64] [--runtimes "base python node infra"] \
#       [--sdk-package google-antigravity] [--agy-binary-url https://...]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PUSH=false
AWS_REGION="${AWS_REGION:-us-east-1}"
PROJECT_PREFIX="${PROJECT_PREFIX:-agentic-workflow-prod}"
TAG="${IMAGE_TAG:-latest}"
PLATFORM=""
RUNTIMES="base python node infra"
SDK_PACKAGE=""
AGY_BINARY_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --push)
      PUSH=true
      shift
      ;;
    --region)
      AWS_REGION="$2"
      shift 2
      ;;
    --prefix)
      PROJECT_PREFIX="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    --platform)
      PLATFORM="$2"
      shift 2
      ;;
    --runtimes)
      RUNTIMES="$2"
      shift 2
      ;;
    --sdk-package)
      SDK_PACKAGE="$2"
      shift 2
      ;;
    --agy-binary-url)
      AGY_BINARY_URL="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '3,10p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

PLATFORM_ARGS=()
if [[ -n "${PLATFORM}" ]]; then
  PLATFORM_ARGS=(--platform "${PLATFORM}")
fi

BASE_LOCAL_TAG="agentic-worker-base:${TAG}"

echo "==> Building worker-base (${BASE_LOCAL_TAG})"
docker build \
  --provenance=false \
  "${PLATFORM_ARGS[@]}" \
  -f "${REPO_ROOT}/docker/Dockerfile.base" \
  --build-arg "ANTIGRAVITY_SDK_PACKAGE=${SDK_PACKAGE}" \
  --build-arg "AGY_BINARY_URL=${AGY_BINARY_URL}" \
  -t "${BASE_LOCAL_TAG}" \
  "${REPO_ROOT}"

for runtime in ${RUNTIMES}; do
  if [[ "${runtime}" == "base" ]]; then
    continue
  fi
  dockerfile="${REPO_ROOT}/docker/Dockerfile.${runtime}"
  if [[ ! -f "${dockerfile}" ]]; then
    echo "Missing Dockerfile for runtime '${runtime}': ${dockerfile}" >&2
    exit 1
  fi
  local_tag="agentic-worker-${runtime}:${TAG}"
  echo "==> Building worker-${runtime} (${local_tag})"
  docker build \
    --provenance=false \
    "${PLATFORM_ARGS[@]}" \
    -f "${dockerfile}" \
    --build-arg "BASE_IMAGE=${BASE_LOCAL_TAG}" \
    -t "${local_tag}" \
    "${REPO_ROOT}"
done

if [[ "${PUSH}" == "true" ]]; then
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  ECR_REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  echo "==> Logging in to ECR ${ECR_REGISTRY}"
  aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "${ECR_REGISTRY}"

  for runtime in ${RUNTIMES}; do
    local_tag="agentic-worker-${runtime}:${TAG}"
    remote_repo="${ECR_REGISTRY}/${PROJECT_PREFIX}-worker-${runtime}"
    remote_tag="${remote_repo}:${TAG}"
    echo "==> Pushing ${remote_tag}"
    docker tag "${local_tag}" "${remote_tag}"
    docker push "${remote_tag}"
  done
fi

echo "==> Done."
