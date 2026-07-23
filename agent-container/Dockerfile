# Sprout Agent_Runtime container image for Amazon Bedrock AgentCore Runtime.
#
# AgentCore Runtime hosts a container that MUST:
#   * be built for linux/arm64 (AgentCore requires ARM64),
#   * listen on port 8080, and
#   * expose `POST /invocations` and `GET /ping`.
# (See agent-container/server.py for the contract implementation.)
#
# This runs the REAL OpenClaw runtime: the official OpenClaw Node app is copied
# into a python:3.12-slim image with Node.js 22.x installed. Both base images are
# sourced from internal ECR (see the ECR_REGISTRY build arg below). server.py
# starts `openclaw gateway run` as a subprocess (OpenAI-compatible
# /v1/chat/completions on loopback) and forwards each AgentCore invocation to it.
# Memory stays on AWS AgentCore Memory.
#
# Community skills are installed at DEPLOY time (scripts/install-skills.sh) into
# agent-container/skills/ BEFORE this build runs — never via the Dockerfile.
#
# IMPORTANT: the Docker build context is the REPOSITORY ROOT, with this
# Dockerfile referenced via `-f agent-container/Dockerfile`, e.g.:
#   docker buildx build --platform linux/arm64 \
#     --build-arg ECR_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com \
#     -f agent-container/Dockerfile -t sprout-agent:test .

# ---------------------------------------------------------------------------
# Base images (sourced from internal ECR)
# ---------------------------------------------------------------------------
# BuilderHub security guidelines require container base images to be pulled from
# an internal Amazon ECR repository (owned by a Conduit/Isengard account) rather
# than an external registry such as Docker Hub or GHCR. Both base images are
# therefore referenced through the ECR_REGISTRY build arg. Mirror the two
# upstream images into your account's ECR ONCE (see the README section
# "Mirroring base images to internal ECR" — scripts/deploy.sh does this
# automatically), then build with:
#   docker buildx build --platform linux/arm64 \
#     --build-arg ECR_REGISTRY=<account>.dkr.ecr.<region>.amazonaws.com \
#     -f agent-container/Dockerfile -t sprout-agent:test .
#
# OpenClaw is pinned to the release this integration is built and tested against
# (v2026.2.26) by tag + digest. `docker buildx imagetools create` copies the
# source manifest verbatim, so the digest is preserved through the mirror and the
# pin below still resolves from ECR — builds stay reproducible and scannable.
# NOTE: do NOT bump OpenClaw without re-testing — newer builds changed the
# gateway chat-completions contract and dropped compatibility with Bedrock
# *inference profile* model ids (the `us.`/`ap.`/`jp.` prefixes this project
# requires). See OpenClaw issue #55642. To update: verify the gateway still
# accepts the Bedrock model id and inference profiles, mirror the new tag+digest
# into ECR, replace the values below, and re-run the container image scan.
ARG ECR_REGISTRY
ARG OPENCLAW_BASE_IMAGE=${ECR_REGISTRY}/openclaw/openclaw:2026.2.26@sha256:ce9347548afa0b6bdd1d262060535ba04baf0b19cde0fc211c8039492647d1b1
ARG PYTHON_BASE_IMAGE=${ECR_REGISTRY}/python:3.12-slim

# ---------------------------------------------------------------------------
# Stage 1: extract the OpenClaw application from the official image
# ---------------------------------------------------------------------------
FROM --platform=linux/arm64 ${OPENCLAW_BASE_IMAGE} AS openclaw-base

# ---------------------------------------------------------------------------
# Stage 2: python 3.12 slim runtime with Node.js 22.x
# ---------------------------------------------------------------------------
FROM --platform=linux/arm64 ${PYTHON_BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OPENCLAW_SKIP_ONBOARDING=1 \
    OPENCLAW_CONFIG=/app/openclaw.json \
    PORT=8080

# Install curl/ca-certificates (health check + node key) and Node.js 22.x.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
       curl \
       ca-certificates \
       gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy the OpenClaw application from the official base image and create the
# `openclaw` wrapper that execs the Node entrypoint.
COPY --from=openclaw-base /app /openclaw-app
RUN printf '#!/bin/sh\nexec node /openclaw-app/openclaw.mjs "$@"\n' > /usr/local/bin/openclaw \
    && chmod +x /usr/local/bin/openclaw

WORKDIR /app

# Install the Python runtime dependencies (boto3 + requests).
COPY agent-container/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Application code: the AgentCore Runtime HTTP server, the OpenClaw gateway
# config, and the persona/startup markdown files the server loads.
COPY agent-container/server.py /app/server.py
COPY agent-container/openclaw.json /app/openclaw.json
COPY agent-container/AGENTS.md /app/AGENTS.md
COPY agent-container/SOUL.md /app/SOUL.md
COPY agent-container/USER.md /app/USER.md
COPY agent-container/TOOLS.md /app/TOOLS.md
COPY agent-container/IDENTITY.md /app/IDENTITY.md

# Community skills, installed at deploy time into agent-container/skills/, are
# placed in OpenClaw's bundled skills directory so the gateway loads them.
COPY agent-container/skills/ /openclaw-app/skills/

# AgentCore Runtime contract: container listens on 8080.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/ping || exit 1

# server.py starts the OpenClaw gateway subprocess then serves GET /ping and
# POST /invocations on :8080.
ENTRYPOINT ["python", "-u", "/app/server.py"]
