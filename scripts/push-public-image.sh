#!/usr/bin/env bash
#
# push-public-image.sh — Build and push the Sprout container to public ECR.
#
# Intended to run as a pre-push hook (see .githooks/pre-push) or manually when
# agent-container/ changes need to be reflected in the public image used by the
# one-click Launch Stack deploy.
#
# Requires: aws CLI, docker with buildx, authenticated to the personal profile.
#
set -euo pipefail

log() { printf '%s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Set to the published AWS-owned ECR Public alias for this project.
PUBLIC_IMAGE="${PUBLIC_IMAGE:-public.ecr.aws/<ECR_PUBLIC_ALIAS>/sprout-agent:latest}"
PLATFORM="linux/arm64"

# The Dockerfile pulls its base images from ECR (BuilderHub requirement) via the
# ECR_REGISTRY build arg, so point it at an ECR registry that already holds the
# mirrored base images (scripts/deploy.sh mirrors them; see the README section
# "Mirroring base images to internal ECR"). The base layers are baked into the
# image that gets pushed here, so consumers of the public image need no ECR
# access — only this maintainer build does.
if [[ -z "${ECR_REGISTRY:-}" ]]; then
  log "ERROR: ECR_REGISTRY is not set."
  log "       Set it to an ECR registry that holds the mirrored base images, e.g."
  log "       ECR_REGISTRY=<account>.dkr.ecr.us-east-1.amazonaws.com $0"
  exit 1
fi

# Authenticate to public ECR (destination) and the base-image ECR (source).
log "Authenticating to public ECR..."
aws ecr-public get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin public.ecr.aws >&2

log "Authenticating to base-image registry ${ECR_REGISTRY}..."
aws ecr get-login-password --region "${AWS_REGION:-us-east-1}" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY" >&2

# Build and push
log "Building and pushing ${PUBLIC_IMAGE} (${PLATFORM})..."
docker buildx build \
  --platform "$PLATFORM" \
  -f "${REPO_ROOT}/agent-container/Dockerfile" \
  --build-arg "ECR_REGISTRY=${ECR_REGISTRY}" \
  -t "$PUBLIC_IMAGE" \
  --push \
  "$REPO_ROOT" >&2

log "Public image pushed: ${PUBLIC_IMAGE}"
