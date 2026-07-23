# Security

This repository contains **educational sample code** published to demonstrate
building a memory-enabled agent on Amazon Bedrock AgentCore. It is not intended
for production use without additional hardening (see the residual-risk section
of the threat model).

## Reporting a vulnerability

If you discover a potential security issue in this project, we ask that you
**do not** open a public GitHub issue. Instead:

- Report it privately through the
  [AWS Vulnerability Reporting](https://aws.amazon.com/security/vulnerability-reporting/)
  page, or email **aws-security@amazon.com**.

Please do **not** create a public issue or pull request that describes the
vulnerability until it has been addressed.

## Security posture

This sample is designed to be secure-by-default. Key controls:

- **Secrets** — the Telegram bot token, the webhook secret-token, and the
  OpenClaw gateway auth token are stored in AWS Secrets Manager, encrypted with
  a customer-managed KMS key (rotation enabled). No secrets are committed to the
  repository; a local `.env` is git-ignored and used only for deployment input.
- **Transport** — the webhook is served over HTTPS by API Gateway; the S3
  workspace bucket denies non-TLS access.
- **Authentication** — inbound Telegram webhooks are validated in constant time
  against a dedicated 48-character secret-token before any processing.
- **Least privilege** — each component (runtime, memory, webhook, cron,
  scheduler) has its own IAM role scoped to account/region-qualified ARNs.
- **Data isolation** — per-user memory namespaces (`sprout/{chat_id}/...`) and
  per-user S3 prefixes (`workspace/{chat_id}/`) prevent cross-user access.
- **Encryption at rest** — S3 (SSE-KMS) and Secrets Manager use the same
  customer-managed KMS key.
- **Cost guardrail** — an AWS Budget with SNS alerting caps runaway spend.

## Threat model & scans

- [`docs/threat-model.md`](docs/threat-model.md) — STRIDE threat model, trust
  boundaries, data-flow diagram, generative-AI–specific threats, and residual
  risks.
- [`docs/security-scan-results.md`](docs/security-scan-results.md) — results and
  dispositions from local static analysis (bandit), IaC linting (cfn-lint),
  secret scanning (detect-secrets), and dependency auditing (pip-audit).

## Deploying securely

- Provide the Telegram bot token only via the deployment input (Secrets Manager
  parameter); never hard-code it.
- Review the IAM policies in `openclaw-telegram.yaml` against your account's
  requirements before deploying.
- The one-click Launch Stack uses a publicly hosted container image; verify the
  image source before deploying into a sensitive account, or build and push your
  own image with `scripts/deploy.sh`.
