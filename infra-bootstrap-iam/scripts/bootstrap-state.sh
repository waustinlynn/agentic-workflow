#!/usr/bin/env bash
set -euo pipefail

# Idempotently create the S3 remote state bucket for Terraform (native S3 locking via use_lockfile)
# and generate a local untracked infra-bootstrap-iam/backend.hcl file.

AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET_NAME="${TF_STATE_BUCKET:-agentic-iam-deploy-tfstate-${ACCOUNT_ID}-${AWS_REGION}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_HCL="${INFRA_DIR}/backend.hcl"

echo "Using AWS Account: ${ACCOUNT_ID}"
echo "Using Region:      ${AWS_REGION}"
echo "State Bucket:      ${BUCKET_NAME}"

if aws s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
  echo "Bucket ${BUCKET_NAME} already exists; verifying security settings..."
else
  echo "Creating S3 bucket ${BUCKET_NAME}..."
  if [ "${AWS_REGION}" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "${BUCKET_NAME}" --region "${AWS_REGION}"
  else
    aws s3api create-bucket \
      --bucket "${BUCKET_NAME}" \
      --region "${AWS_REGION}" \
      --create-bucket-configuration "LocationConstraint=${AWS_REGION}"
  fi
fi

echo "Blocking all public access..."
aws s3api put-public-access-block \
  --bucket "${BUCKET_NAME}" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "Enabling versioning..."
aws s3api put-bucket-versioning \
  --bucket "${BUCKET_NAME}" \
  --versioning-configuration Status=Enabled

echo "Enabling default AES256 server-side encryption..."
aws s3api put-bucket-encryption \
  --bucket "${BUCKET_NAME}" \
  --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

echo "Enforcing TLS-only bucket policy..."
BUCKET_POLICY="$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws:s3:::${BUCKET_NAME}",
        "arn:aws:s3:::${BUCKET_NAME}/*"
      ],
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
EOF
)"
aws s3api put-bucket-policy --bucket "${BUCKET_NAME}" --policy "${BUCKET_POLICY}"

cat > "${BACKEND_HCL}" <<EOF
bucket = "${BUCKET_NAME}"
region = "${AWS_REGION}"
EOF

echo "Wrote untracked backend config to ${BACKEND_HCL}"
echo "Next step: cd infra-bootstrap-iam && terraform init -backend-config=backend.hcl"
