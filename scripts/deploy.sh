#!/usr/bin/env bash
#
# deploy.sh — Deployment orchestrator for the OpenClaw Telegram AgentCore stack
# (Sprout, the serverless gardening assistant).
#
# This script drives a full deployment end-to-end through eight stages:
#   1. Prerequisites check  — aws, docker (+ buildx), curl on PATH
#   2. Template validation  — aws cloudformation validate-template
#   3. ECR repository       — create-if-not-exists (<STACK_NAME>-agent)
#   4. Mirror base images   — copy the upstream base images into your ECR so the
#                             build sources them from ECR (BuilderHub requirement)
#   5. Community skills      — install into agent-container/skills/ (pre-build)
#   6. Docker build & push  — linux/arm64 via buildx, tagged :latest
#   7. Stack create/update  — idempotent CloudFormation deploy (+ wait)
#   8. Telegram webhook      — register WebhookUrl via setWebhook
#
# Human-readable progress is written to STDERR via log(). On any stage failure
# the failed command's error is shown, the failed stage name is printed, and the
# script exits non-zero.
#
# ------------------------------------------------------------------------------
# Configuration (all overridable via environment variables):
#   TELEGRAM_BOT_TOKEN     (required) Telegram Bot API token from BotFather.
#                          Used as a CloudFormation parameter and for setWebhook.
#   AWS_REGION             AWS region   (default: $AWS_REGION / $CDK_DEFAULT_REGION / us-east-1)
#   AWS_ACCOUNT_ID         Account id   (default: from `aws sts get-caller-identity`)
#   STACK_NAME             Stack name   (default: sprout)
#   AWS_PROFILE            AWS CLI profile (optional; honored by the AWS CLI if set)
#
# Stack parameter overrides (defaults mirror openclaw-telegram.yaml):
#   MODEL_ID                 (default: us.anthropic.claude-haiku-4-5-20251001-v1:0)
#   VISION_MODEL_ID          (default: us.anthropic.claude-sonnet-4-5-20250929-v1:0)
#   MONTHLY_BUDGET_LIMIT     (default: 25)
#   ALERT_EMAIL              (default: empty)
#   LOG_RETENTION_DAYS       (default: 30)
#
# Configuration is loaded from a .env file at the repo root when present (see
# .env.example for the full list of supported variables). Variables already set
# in the environment take precedence over values in .env, so you can still
# override any single value inline, e.g. `STACK_NAME=mygarden scripts/deploy.sh`.
#
# Usage:
#   # Copy the template, fill in your values, then just run the script:
#   cp .env.example .env && $EDITOR .env && scripts/deploy.sh
#
#   # Or provide values inline / via exported environment variables:
#   TELEGRAM_BOT_TOKEN=123456:ABC... scripts/deploy.sh
#   STACK_NAME=mygarden AWS_REGION=us-west-2 TELEGRAM_BOT_TOKEN=... scripts/deploy.sh
#
set -euo pipefail

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
log() { printf '%s\n' "$*" >&2; }

# load_dotenv <path>
# Loads KEY=VALUE pairs from a .env file into the environment WITHOUT clobbering
# variables that are already set (so inline/exported values win). Blank lines and
# lines starting with '#' are ignored, as is an optional leading `export `.
# Surrounding single or double quotes around the value are stripped.
load_dotenv() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 0
  log "Loading configuration from ${env_file}"

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    # Strip a trailing carriage return (in case the file has CRLF line endings).
    line="${line%$'\r'}"
    # Skip blank lines and comments.
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    # Drop an optional leading `export ` and surrounding whitespace.
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line#export }"
    # Require a KEY=VALUE shape; skip anything malformed.
    [[ "$line" == *"="* ]] || continue

    key="${line%%=*}"
    value="${line#*=}"
    # Trim whitespace around the key.
    key="${key//[[:space:]]/}"
    [[ -z "$key" ]] && continue
    # Strip matching surrounding quotes from the value.
    if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    # Do not override values already present in the environment.
    if [[ -z "${!key:-}" ]]; then
      export "$key=$value"
    fi
  done < "$env_file"
}

# fail <stage-name> [extra message...]
# Reports the failed stage and exits non-zero. Intended to be called from a
# stage's error path; the failing command's own error output has already been
# surfaced to STDERR (commands are not silenced).
fail() {
  local stage="$1"; shift || true
  if [[ "$#" -gt 0 ]]; then
    log "ERROR: $*"
  fi
  log "DEPLOY FAILED at stage: ${stage}"
  exit 1
}

# ------------------------------------------------------------------------------
# Resolve configuration
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env from the repo root (if present). Values already set in the
# environment take precedence, so inline overrides still work.
load_dotenv "${REPO_ROOT}/.env"

AWS_REGION="${AWS_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}"
STACK_NAME="${STACK_NAME:-sprout}"

TEMPLATE_FILE="${REPO_ROOT}/openclaw-telegram.yaml"
DOCKERFILE="${REPO_ROOT}/agent-container/Dockerfile"

# Bumped every run (unless overridden). Used both as a runtime env var AND as
# the image tag so each deploy produces a UNIQUE ContainerImageUri. AgentCore
# Runtime only rolls a fresh container when the ContainerUri string changes;
# a fixed ":latest" tag would leave the old warm container serving stale code
# and env. A unique per-deploy tag guarantees the new image + env take effect.
DEPLOYMENT_VERSION="${DEPLOYMENT_VERSION:-$(date +%s)}"

# Image wiring: the CloudFormation ContainerImageUri parameter is set to this
# exact URI, so the tag must be unique per deploy (see DEPLOYMENT_VERSION above).
ECR_REPO_NAME="${STACK_NAME}-agent"
IMAGE_TAG="${DEPLOYMENT_VERSION}"
PLATFORM="linux/arm64"

# Base images are pulled from ECR (BuilderHub requires internal-ECR base images).
# deploy.sh mirrors these upstream images into dedicated repos in your account's
# ECR, and the Dockerfile references them via the ECR_REGISTRY build arg. The
# upstream sources are overridable so a mirror/proxy can be substituted; the
# OpenClaw pin keeps its digest (imagetools create preserves the manifest).
OPENCLAW_UPSTREAM_IMAGE="${OPENCLAW_UPSTREAM_IMAGE:-ghcr.io/openclaw/openclaw:2026.2.26@sha256:ce9347548afa0b6bdd1d262060535ba04baf0b19cde0fc211c8039492647d1b1}"
OPENCLAW_ECR_REPO="openclaw/openclaw"
OPENCLAW_ECR_TAG="2026.2.26"
PYTHON_UPSTREAM_IMAGE="${PYTHON_UPSTREAM_IMAGE:-python:3.12-slim}"
PYTHON_ECR_REPO="python"
PYTHON_ECR_TAG="3.12-slim"

INSTALL_SKILLS_SCRIPT="${SCRIPT_DIR}/install-skills.sh"
MIRROR_BASE_IMAGES_SCRIPT="${SCRIPT_DIR}/mirror-base-images.sh"
LAMBDA_SRC_DIR="${REPO_ROOT}/telegram-webhook"

# Stack parameter overrides (defaults mirror openclaw-telegram.yaml).
MODEL_ID="${MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
VISION_MODEL_ID="${VISION_MODEL_ID:-us.anthropic.claude-sonnet-4-5-20250929-v1:0}"
MONTHLY_BUDGET_LIMIT="${MONTHLY_BUDGET_LIMIT:-25}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-30}"

# ------------------------------------------------------------------------------
# Stage 1 — Prerequisites check (Req 14.7)
# ------------------------------------------------------------------------------
stage_prerequisites() {
  log "==> [Stage 1/8] Prerequisites check"

  if ! command -v aws >/dev/null 2>&1; then
    fail "Prerequisites check" "required tool 'aws' (AWS CLI) not found on PATH."
  fi
  if ! command -v docker >/dev/null 2>&1; then
    fail "Prerequisites check" "required tool 'docker' not found on PATH."
  fi
  if ! docker buildx version >/dev/null 2>&1; then
    fail "Prerequisites check" "required tool 'docker buildx' (Docker Buildx) is not available."
  fi
  if ! command -v curl >/dev/null 2>&1; then
    fail "Prerequisites check" "required tool 'curl' not found on PATH."
  fi

  # The bot token is required both as a stack parameter and for setWebhook.
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
    fail "Prerequisites check" "TELEGRAM_BOT_TOKEN environment variable is required but not set."
  fi

  if [[ ! -f "$TEMPLATE_FILE" ]]; then
    fail "Prerequisites check" "CloudFormation template not found at ${TEMPLATE_FILE}."
  fi
  if [[ ! -f "$DOCKERFILE" ]]; then
    fail "Prerequisites check" "Dockerfile not found at ${DOCKERFILE}."
  fi

  # Resolve the account id now that the AWS CLI is confirmed available.
  if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
    log "Resolving AWS account id via sts get-caller-identity..."
    if ! AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>&1)"; then
      fail "Prerequisites check" "unable to resolve AWS account id (is the AWS CLI authenticated?): ${AWS_ACCOUNT_ID}"
    fi
  fi

  REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
  IMAGE_URI="${REGISTRY}/${ECR_REPO_NAME}:${IMAGE_TAG}"

  log "Region:      ${AWS_REGION}"
  log "Account:     ${AWS_ACCOUNT_ID}"
  log "Stack:       ${STACK_NAME}"
  log "ECR repo:    ${ECR_REPO_NAME}"
  log "Image URI:   ${IMAGE_URI}"
  log "All prerequisites satisfied."
}

# ------------------------------------------------------------------------------
# Stage 2 — Template validation (Req 14.1)
# ------------------------------------------------------------------------------
stage_validate_template() {
  log "==> [Stage 2/8] Validating CloudFormation template"
  if ! aws cloudformation validate-template \
        --template-body "file://${TEMPLATE_FILE}" \
        --region "$AWS_REGION" >/dev/null; then
    fail "Template validation"
  fi
  log "Template is valid."
}

# ------------------------------------------------------------------------------
# Stage 3 — ECR repository create-if-not-exists (Req 14.2)
# ------------------------------------------------------------------------------
stage_ecr_repository() {
  log "==> [Stage 3/8] Ensuring ECR repository '${ECR_REPO_NAME}' exists"
  if aws ecr describe-repositories \
        --repository-names "$ECR_REPO_NAME" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    log "ECR repository '${ECR_REPO_NAME}' already exists."
  else
    log "Creating ECR repository '${ECR_REPO_NAME}'..."
    if ! aws ecr create-repository \
          --repository-name "$ECR_REPO_NAME" \
          --image-scanning-configuration scanOnPush=true \
          --image-tag-mutability MUTABLE \
          --region "$AWS_REGION" >/dev/null; then
      fail "ECR repository create-if-not-exists"
    fi
    log "Created ECR repository '${ECR_REPO_NAME}'."
  fi
}

# ------------------------------------------------------------------------------
# Stage 4 — Mirror base images into ECR (BuilderHub internal-ECR requirement)
# ------------------------------------------------------------------------------
# The Dockerfile pulls its two base images (OpenClaw + python:3.12-slim) from ECR
# via the ECR_REGISTRY build arg. This stage delegates to the standalone
# scripts/mirror-base-images.sh helper, which ensures the destination repos
# exist and copies the upstream images into them with `docker buildx imagetools
# create` (the source manifest is copied verbatim, so the OpenClaw tag+digest
# pin is preserved and still resolves from ECR). Re-running is safe: each tag is
# simply re-pointed at the same (content-addressed) manifest. The already
# resolved account/region/registry and image coordinates are passed through the
# environment so both entry points mirror identical images.
stage_mirror_base_images() {
  log "==> [Stage 4/8] Mirroring base images into ECR"

  if [[ ! -f "$MIRROR_BASE_IMAGES_SCRIPT" ]]; then
    fail "Mirror base images" "mirror script not found at ${MIRROR_BASE_IMAGES_SCRIPT}."
  fi

  if ! AWS_REGION="$AWS_REGION" \
       AWS_ACCOUNT_ID="$AWS_ACCOUNT_ID" \
       REGISTRY="$REGISTRY" \
       OPENCLAW_UPSTREAM_IMAGE="$OPENCLAW_UPSTREAM_IMAGE" \
       OPENCLAW_ECR_REPO="$OPENCLAW_ECR_REPO" \
       OPENCLAW_ECR_TAG="$OPENCLAW_ECR_TAG" \
       PYTHON_UPSTREAM_IMAGE="$PYTHON_UPSTREAM_IMAGE" \
       PYTHON_ECR_REPO="$PYTHON_ECR_REPO" \
       PYTHON_ECR_TAG="$PYTHON_ECR_TAG" \
       bash "$MIRROR_BASE_IMAGES_SCRIPT" >/dev/null; then
    fail "Mirror base images"
  fi

  log "Base images mirrored to ECR."
}

# ------------------------------------------------------------------------------
# Stage 5 — Community skills install (pre-build)
# ------------------------------------------------------------------------------
# Community skills are resolved from ClawHub and materialized into
# agent-container/skills/ (and registered in agent-container/openclaw.json)
# BEFORE the image is built, so the Dockerfile only COPYs already-installed
# skills — it never runs `openclaw skill install` itself. The installer warns
# and falls back to local stubs when a skill cannot be resolved.
stage_install_skills() {
  log "==> [Stage 5/8] Installing community skills (pre-build)"
  if [[ ! -f "$INSTALL_SKILLS_SCRIPT" ]]; then
    fail "Community skills install" "installer not found at ${INSTALL_SKILLS_SCRIPT}."
  fi
  if ! bash "$INSTALL_SKILLS_SCRIPT" >&2; then
    fail "Community skills install"
  fi
  log "Community skills ready."
}

# ------------------------------------------------------------------------------
# Stage 6 — Docker build (linux/arm64) and push to ECR (Req 14.2)
# ------------------------------------------------------------------------------
stage_build_and_push() {
  log "==> [Stage 6/8] Building (${PLATFORM}) and pushing image"

  log "Logging in to ECR registry ${REGISTRY}..."
  if ! aws ecr get-login-password --region "$AWS_REGION" \
        | docker login --username AWS --password-stdin "$REGISTRY" >&2; then
    fail "Docker build and push" "ECR docker login failed."
  fi

  # Build context is the repo root; the Dockerfile lives under agent-container/.
  log "Building and pushing ${IMAGE_URI}..."
  if ! docker buildx build \
        --platform "$PLATFORM" \
        --file "$DOCKERFILE" \
        --build-arg "ECR_REGISTRY=${REGISTRY}" \
        --tag "$IMAGE_URI" \
        --push \
        "$REPO_ROOT" >&2; then
    fail "Docker build and push"
  fi
  log "Pushed ${IMAGE_URI}."
}

# ------------------------------------------------------------------------------
# Stage 7 — CloudFormation create-or-update and wait for completion (Req 14.3)
# ------------------------------------------------------------------------------
# `aws cloudformation deploy` is idempotent: it creates the stack on first run
# and updates it on subsequent runs, waiting for completion in both cases. It
# also treats the "no updates to perform" case as success, so the script does
# not fail when nothing changed. The deploy command performs both the change
# submission and the wait.
stage_deploy_stack() {
  log "==> [Stage 7/8] Creating/updating CloudFormation stack '${STACK_NAME}' (waits for completion)"

  local deploy_output
  # Capture combined output so we can detect the benign "no changes" message
  # that `aws cloudformation deploy` reports with a non-zero exit code.
  if deploy_output="$(aws cloudformation deploy \
        --stack-name "$STACK_NAME" \
        --template-file "$TEMPLATE_FILE" \
        --region "$AWS_REGION" \
        --capabilities CAPABILITY_NAMED_IAM \
        --no-fail-on-empty-changeset \
        --parameter-overrides \
          "TelegramBotToken=${TELEGRAM_BOT_TOKEN}" \
          "ModelId=${MODEL_ID}" \
          "VisionModelId=${VISION_MODEL_ID}" \
          "MonthlyBudgetLimit=${MONTHLY_BUDGET_LIMIT}" \
          "AlertEmail=${ALERT_EMAIL}" \
          "LogRetentionDays=${LOG_RETENTION_DAYS}" \
          "DeploymentVersion=${DEPLOYMENT_VERSION}" \
          "ContainerImageUri=${IMAGE_URI}" 2>&1)"; then
    [[ -n "$deploy_output" ]] && log "$deploy_output"
    log "Stack '${STACK_NAME}' reached a completed state."
  else
    # Older AWS CLI versions exit non-zero with this message even when the
    # changeset is empty; treat that as success for idempotency.
    if printf '%s' "$deploy_output" | grep -qiE "No changes to deploy|didn't contain changes|No updates are to be performed"; then
      log "No changes to deploy; stack '${STACK_NAME}' is already up to date."
    else
      [[ -n "$deploy_output" ]] && log "$deploy_output"
      fail "CloudFormation create-or-update"
    fi
  fi
}

# ------------------------------------------------------------------------------
# Stage 7b — Package and upload the real Lambda handler code
# ------------------------------------------------------------------------------
# The CloudFormation template creates the Webhook/Cron Lambdas with a tiny inline
# placeholder (so the stack can stand up before an image/code exists). The real
# handlers live in telegram-webhook/ and depend on ``requests`` (not present in
# the Lambda Python runtime), so we build a deployment package that bundles the
# third-party deps alongside the handler modules and push it to both functions
# with ``update-function-code``. This runs AFTER the stack exists so the
# functions are present to update.
stage_package_lambdas() {
  log "==> [Stage 7b/8] Packaging and uploading Lambda handler code"

  if [[ ! -d "$LAMBDA_SRC_DIR" ]]; then
    fail "Lambda packaging" "handler source directory not found at ${LAMBDA_SRC_DIR}."
  fi

  local build_dir zip_file
  build_dir="$(mktemp -d)"
  zip_file="$(mktemp -u).zip"

  # Bundle third-party dependencies (requests + transitive) into the package
  # root. boto3/botocore are provided by the Lambda runtime and are intentionally
  # not bundled to keep the package small.
  if [[ -f "${LAMBDA_SRC_DIR}/requirements.txt" ]]; then
    log "Installing Lambda dependencies into the package..."
    if ! python3 -m pip install \
          --quiet \
          --target "$build_dir" \
          --only-binary=:all: \
          requests >&2; then
      rm -rf "$build_dir"
      fail "Lambda packaging" "pip install of Lambda dependencies failed."
    fi
  fi

  # Copy the handler modules (handler.py, cron_handler.py, telegram_api.py).
  cp "${LAMBDA_SRC_DIR}"/*.py "$build_dir/" 2>/dev/null || true

  # Zip the package contents at the archive root (Lambda expects handlers at top).
  log "Creating deployment package ${zip_file}..."
  ( cd "$build_dir" && zip -qr "$zip_file" . -x '*.pyc' -x '*/__pycache__/*' ) || {
    rm -rf "$build_dir"
    fail "Lambda packaging" "failed to create the deployment zip."
  }

  # Update both functions with the same package (each has its own Handler).
  local fn
  for fn in "${STACK_NAME}-webhook" "${STACK_NAME}-cron"; do
    log "Updating function code: ${fn}"
    if ! aws lambda update-function-code \
          --function-name "$fn" \
          --zip-file "fileb://${zip_file}" \
          --region "$AWS_REGION" >/dev/null; then
      rm -rf "$build_dir" "$zip_file"
      fail "Lambda packaging" "update-function-code failed for ${fn}."
    fi
  done

  rm -rf "$build_dir" "$zip_file"
  log "Lambda handler code uploaded to ${STACK_NAME}-webhook and ${STACK_NAME}-cron."
}

# ------------------------------------------------------------------------------
# Stage 8 — Register the Telegram webhook (Req 14.4)
# ------------------------------------------------------------------------------
stage_register_webhook() {
  log "==> [Stage 8/8] Registering Telegram webhook"

  local webhook_url
  if ! webhook_url="$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue | [0]" \
        --output text 2>&1)"; then
    fail "Telegram webhook registration" "unable to read WebhookUrl stack output: ${webhook_url}"
  fi

  if [[ -z "$webhook_url" || "$webhook_url" == "None" ]]; then
    fail "Telegram webhook registration" "WebhookUrl output is empty; stack may not have deployed correctly."
  fi

  log "Webhook URL: ${webhook_url}"

  # Fetch the dedicated webhook secret so we can register it as setWebhook's
  # secret_token. Telegram then echoes it in the X-Telegram-Bot-Api-Secret-Token
  # header on every delivery, and the webhook Lambda validates it (Req 2.4).
  local webhook_secret_arn webhook_secret
  if ! webhook_secret_arn="$(aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Outputs[?OutputKey=='WebhookSecretArn'].OutputValue | [0]" \
        --output text 2>&1)"; then
    fail "Telegram webhook registration" "unable to read WebhookSecretArn stack output: ${webhook_secret_arn}"
  fi
  if [[ -z "$webhook_secret_arn" || "$webhook_secret_arn" == "None" ]]; then
    fail "Telegram webhook registration" "WebhookSecretArn output is empty; stack may not have deployed correctly."
  fi
  if ! webhook_secret="$(aws secretsmanager get-secret-value \
        --secret-id "$webhook_secret_arn" \
        --region "$AWS_REGION" \
        --query 'SecretString' \
        --output text 2>&1)"; then
    fail "Telegram webhook registration" "unable to read webhook secret value: ${webhook_secret}"
  fi

  # Call the Telegram Bot API setWebhook method. --data-urlencode ensures the
  # url/secret are safely encoded; -sS keeps curl quiet but still reports errors;
  # --fail makes HTTP >=400 responses return a non-zero exit code.
  local response
  if ! response="$(curl -sS --fail \
        --get \
        --data-urlencode "url=${webhook_url}" \
        --data-urlencode "secret_token=${webhook_secret}" \
        "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" 2>&1)"; then
    fail "Telegram webhook registration" "setWebhook request failed: ${response}"
  fi

  log "Telegram setWebhook response: ${response}"

  # Telegram returns {"ok":true,...} on success; anything else is a failure.
  if ! printf '%s' "$response" | grep -q '"ok":true'; then
    fail "Telegram webhook registration" "Telegram API did not confirm webhook registration."
  fi

  log "Webhook registered successfully."
}

# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
main() {
  log "Starting deployment of '${STACK_NAME}' to region '${AWS_REGION}'."
  stage_prerequisites
  stage_validate_template
  stage_ecr_repository
  stage_mirror_base_images
  stage_install_skills
  stage_build_and_push
  stage_deploy_stack
  stage_package_lambdas
  stage_register_webhook
  log "Deployment complete. Sprout is live."
}

main "$@"
