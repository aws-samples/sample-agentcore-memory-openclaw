#!/usr/bin/env bash
#
# mirror-base-images.sh — Mirror the agent container's upstream base images into
# your account's Amazon ECR (BuilderHub internal-ECR requirement).
#
# The agent container (agent-container/Dockerfile) builds on two upstream base
# images — the official OpenClaw release and python:3.12-slim — but BuilderHub
# security guidelines require base images to be pulled from an internal Amazon
# ECR repository rather than an external registry (Docker Hub / GHCR). The
# Dockerfile therefore references both base images through the ECR_REGISTRY
# build arg, and this script copies the upstream images into your account's ECR
# so that arg resolves.
#
# `docker buildx imagetools create` copies the source manifest verbatim, so the
# OpenClaw tag+digest pin is preserved through the mirror and still resolves from
# ECR. Re-running is safe: each destination tag is simply re-pointed at the same
# (content-addressed) manifest.
#
# This script is invoked automatically by scripts/deploy.sh (Stage 4), and can
# also be run on its own for the one-time manual mirror described in the README
# section "Mirroring base images to internal ECR".
#
# ------------------------------------------------------------------------------
# Configuration (all overridable via environment variables):
#   AWS_REGION               AWS region (default: $AWS_REGION / $CDK_DEFAULT_REGION / us-east-1)
#   AWS_ACCOUNT_ID           Account id (default: from `aws sts get-caller-identity`)
#   REGISTRY                 ECR registry host (default: <account>.dkr.ecr.<region>.amazonaws.com)
#   OPENCLAW_UPSTREAM_IMAGE  Source OpenClaw image (default: pinned ghcr.io tag+digest)
#   OPENCLAW_ECR_REPO        Destination repo    (default: openclaw/openclaw)
#   OPENCLAW_ECR_TAG         Destination tag     (default: 2026.2.26)
#   PYTHON_UPSTREAM_IMAGE    Source Python image (default: python:3.12-slim)
#   PYTHON_ECR_REPO          Destination repo    (default: python)
#   PYTHON_ECR_TAG           Destination tag     (default: 3.12-slim)
#
# On success the resolved REGISTRY is printed to STDOUT (so callers can capture
# it), while all human-readable progress goes to STDERR.
#
# Usage:
#   scripts/mirror-base-images.sh
#   AWS_REGION=us-west-2 scripts/mirror-base-images.sh
#
set -euo pipefail

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
log() { printf '%s\n' "$*" >&2; }

fail() {
  log "ERROR: $*"
  log "MIRROR FAILED"
  exit 1
}

# ------------------------------------------------------------------------------
# Resolve configuration
# ------------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}"

# Upstream sources are overridable so a mirror/proxy can be substituted; the
# OpenClaw pin keeps its digest (imagetools create preserves the manifest).
OPENCLAW_UPSTREAM_IMAGE="${OPENCLAW_UPSTREAM_IMAGE:-ghcr.io/openclaw/openclaw:2026.2.26@sha256:ce9347548afa0b6bdd1d262060535ba04baf0b19cde0fc211c8039492647d1b1}"
OPENCLAW_ECR_REPO="${OPENCLAW_ECR_REPO:-openclaw/openclaw}"
OPENCLAW_ECR_TAG="${OPENCLAW_ECR_TAG:-2026.2.26}"
PYTHON_UPSTREAM_IMAGE="${PYTHON_UPSTREAM_IMAGE:-python:3.12-slim}"
PYTHON_ECR_REPO="${PYTHON_ECR_REPO:-python}"
PYTHON_ECR_TAG="${PYTHON_ECR_TAG:-3.12-slim}"

# ------------------------------------------------------------------------------
# Prerequisites
# ------------------------------------------------------------------------------
command -v aws >/dev/null 2>&1 || fail "required tool 'aws' (AWS CLI) not found on PATH."
command -v docker >/dev/null 2>&1 || fail "required tool 'docker' not found on PATH."
docker buildx version >/dev/null 2>&1 || fail "required tool 'docker buildx' (Docker Buildx) is not available."

# Resolve the account id / registry when not supplied by the caller (deploy.sh
# passes these through; standalone runs resolve them here).
if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
  log "Resolving AWS account id via sts get-caller-identity..."
  if ! AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
    fail "unable to resolve AWS account id (is the AWS CLI authenticated?): ${AWS_ACCOUNT_ID}"
  fi
fi
REGISTRY="${REGISTRY:-${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com}"

# ------------------------------------------------------------------------------
# Ensure destination repositories exist (idempotent)
# ------------------------------------------------------------------------------
ensure_repo() {
  local repo="$1"
  if aws ecr describe-repositories \
        --repository-names "$repo" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    log "ECR repository '${repo}' already exists."
  else
    log "Creating ECR repository '${repo}'..."
    if ! aws ecr create-repository \
          --repository-name "$repo" \
          --image-scanning-configuration scanOnPush=true \
          --image-tag-mutability MUTABLE \
          --region "$AWS_REGION" >/dev/null; then
      fail "failed to create ECR repository '${repo}'."
    fi
  fi
}

# ------------------------------------------------------------------------------
# Mirror a single upstream image into ECR (imagetools preserves the manifest)
# ------------------------------------------------------------------------------
mirror_image() {
  local upstream="$1" repo="$2" tag="$3"
  local dest="${REGISTRY}/${repo}:${tag}"
  log "Mirroring ${upstream}"
  log "       -> ${dest}"
  if ! docker buildx imagetools create --tag "$dest" "$upstream" >&2; then
    fail "failed to mirror '${upstream}' -> '${dest}'."
  fi
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
main() {
  log "==> Mirroring base images into ECR (${REGISTRY})"

  ensure_repo "$OPENCLAW_ECR_REPO"
  ensure_repo "$PYTHON_ECR_REPO"

  log "Logging in to ECR registry ${REGISTRY}..."
  if ! aws ecr get-login-password --region "$AWS_REGION" \
        | docker login --username AWS --password-stdin "$REGISTRY" >&2; then
    fail "ECR docker login failed."
  fi

  mirror_image "$OPENCLAW_UPSTREAM_IMAGE" "$OPENCLAW_ECR_REPO" "$OPENCLAW_ECR_TAG"
  mirror_image "$PYTHON_UPSTREAM_IMAGE" "$PYTHON_ECR_REPO" "$PYTHON_ECR_TAG"

  log "Base images mirrored to ECR."
  # Emit the resolved registry on STDOUT so callers can capture it.
  printf '%s\n' "$REGISTRY"
}

main "$@"
