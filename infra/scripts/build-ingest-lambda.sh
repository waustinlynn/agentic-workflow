#!/usr/bin/env bash
set -euo pipefail

# Build an AWS Lambda arm64 (Python 3.12) deployment zip for src/ingest + src/shared.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"
BUILD_DIR="${INFRA_DIR}/.build"
STAGE_DIR="${BUILD_DIR}/ingest_stage"
DEPS_DIR="${BUILD_DIR}/ingest_deps_arm64"
ZIP_PATH="${BUILD_DIR}/ingest_lambda.zip"
STAMP_FILE="${DEPS_DIR}/.deps.sha256"

DEPS=(
  "pydantic>=2.7,<3"
  "httpx>=0.27,<1"
  "pyjwt[crypto]>=2.8,<3"
  "pyyaml>=6.0,<7"
)

mkdir -p "${BUILD_DIR}"

DEPS_HASH="$(printf '%s\n' "${DEPS[@]}" | sha256sum | awk '{print $1}')"
if [[ ! -f "${STAMP_FILE}" ]] || [[ "$(cat "${STAMP_FILE}")" != "${DEPS_HASH}" ]]; then
  echo "Installing arm64 Python 3.12 Lambda dependencies via uv..."
  rm -rf "${DEPS_DIR}"
  mkdir -p "${DEPS_DIR}"
  uv pip install \
    --quiet \
    --target "${DEPS_DIR}" \
    --python-platform aarch64-unknown-linux-gnu \
    --python-version 3.12 \
    --only-binary=:all: \
    "${DEPS[@]}"
  printf '%s' "${DEPS_HASH}" > "${STAMP_FILE}"
else
  echo "Reusing cached arm64 Lambda dependencies in ${DEPS_DIR}"
fi

rm -rf "${STAGE_DIR}"
mkdir -p "${STAGE_DIR}"
cp -a "${DEPS_DIR}/." "${STAGE_DIR}/"
rm -f "${STAGE_DIR}/.deps.sha256"
cp -a "${REPO_ROOT}/src/shared" "${STAGE_DIR}/shared"
cp -a "${REPO_ROOT}/src/ingest" "${STAGE_DIR}/ingest"
find "${STAGE_DIR}" -type d -name "__pycache__" -exec rm -rf {} +
find "${STAGE_DIR}" -type f -name "*.pyc" -delete

python3 - "${STAGE_DIR}" "${ZIP_PATH}" <<'PY'
import os
import pathlib
import sys
import zipfile

stage_dir = pathlib.Path(sys.argv[1])
zip_path = pathlib.Path(sys.argv[2])
fixed_time = (2025, 1, 1, 0, 0, 0)

files = sorted(p for p in stage_dir.rglob("*") if p.is_file())
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for path in files:
        rel = path.relative_to(stage_dir).as_posix()
        info = zipfile.ZipInfo(rel, date_time=fixed_time)
        info.external_attr = (os.stat(path).st_mode & 0xFFFF) << 16
        zf.writestr(info, path.read_bytes())

print(f"Built {zip_path} ({zip_path.stat().st_size // 1024} KiB, {len(files)} files)")
PY
