# Troubleshooting

Common problems you may hit deploying and running Sprout, grouped by category. Each entry
lists the symptom you would observe, the likely cause, and how to resolve it.

## Deployment failures

### 1. Prerequisite tool missing

- **Symptom:** `scripts/deploy.sh` exits immediately with a message like
  `required tool 'docker buildx' (Docker Buildx) is not available` and `DEPLOY FAILED at
  stage: Prerequisites check`.
- **Cause:** One of the required tools (`aws` CLI, `docker` with the `buildx` plugin, or
  `curl`) is not on your `PATH`, or `TELEGRAM_BOT_TOKEN` was not exported.
- **Resolution:** Install the missing tool (`docker buildx` ships with recent Docker
  Desktop / Docker Engine; verify with `docker buildx version`). Ensure the AWS CLI is
  authenticated (`aws sts get-caller-identity`). Export the token before running:
  `export TELEGRAM_BOT_TOKEN=123456:ABC...`.

### 2. Template validation error / parameter constraint violation

- **Symptom:** Deploy stops at `DEPLOY FAILED at stage: Template validation`, or the
  CloudFormation console rejects a parameter (for example "MonthlyBudgetLimit must be a
  number between 1 and 10000" or an `AlertEmail` pattern error) before any resources are
  created.
- **Cause:** A parameter value falls outside its constraints — budget outside 1–10000,
  an invalid email format, or a `LogRetentionDays` value not in the allowed list.
- **Resolution:** Re-supply a valid value. See [configuration.md](./configuration.md) for
  the exact type, default, and constraint of every parameter. For the deploy script, set
  the corresponding environment variable (e.g. `MONTHLY_BUDGET_LIMIT=25`).

### 3. Stack rolls back during resource creation (image not found)

- **Symptom:** The stack reaches `ROLLBACK_COMPLETE`; the `AgentCoreRuntime` resource
  fails because the container image cannot be pulled, or the ECR login step failed during
  the build.
- **Cause:** The container image was not built/pushed before the stack tried to create the
  runtime, the ECR repository name does not match `${StackName}-agent`, or the image was
  built for the wrong architecture (AgentCore Runtime requires `linux/arm64`).
- **Resolution:** Run the full `scripts/deploy.sh`, which builds for `linux/arm64` via
  `docker buildx` and pushes to `${STACK_NAME}-agent:latest` before deploying the stack.
  Confirm the image exists with
  `aws ecr describe-images --repository-name ${STACK_NAME}-agent`. Delete the failed stack
  (or let it roll back fully) and redeploy — CloudFormation rolls back automatically to
  leave no orphaned resources.

### 4. Bucket name already exists

- **Symptom:** Stack creation fails on the `WorkspaceBucket` resource with a bucket name
  conflict.
- **Cause:** S3 bucket names are globally unique. The bucket is named
  `${StackName}-workspace-${AccountId}`; a leftover bucket from a previous stack with the
  same name still exists.
- **Resolution:** Empty and delete the stale bucket, or deploy under a different
  `STACK_NAME`.

## Webhook issues

### 1. Messages sent to the bot get no reply

- **Symptom:** You message the bot in Telegram and nothing comes back.
- **Cause:** The webhook was never registered with Telegram, or it points at the wrong
  URL. The deploy script registers it in Stage 7 via `setWebhook`; if that stage was
  skipped or failed, Telegram has no endpoint to deliver updates to.
- **Resolution:** Check the current webhook with
  `curl "https://api.telegram.org/bot<token>/getWebhookInfo"`. It should show the API
  Gateway `WebhookUrl` from the stack outputs. Re-register with
  `curl "https://api.telegram.org/bot<token>/setWebhook?url=<WebhookUrl>"`, or re-run
  `scripts/deploy.sh`.

### 2. Requests rejected with HTTP 401

- **Symptom:** `getWebhookInfo` shows a `last_error_message`, and the webhook Lambda logs
  show requests returning 401.
- **Cause:** The incoming request lacks a valid Telegram bot token in the verification
  header, or the token stored in Secrets Manager no longer matches the token registered
  with Telegram (for example after a BotFather token regeneration).
- **Resolution:** Regenerate/confirm the token in BotFather, update the stack's
  `TelegramBotToken` parameter so the new value lands in Secrets Manager, and re-run
  `setWebhook`. The webhook Lambda always reads the latest secret value (no caching), so
  no redeploy of code is needed.

### 3. Long replies arrive truncated or the assistant reports being unavailable

- **Symptom:** Very long answers seem cut off, or the user sees "the assistant is
  temporarily unavailable."
- **Cause:** Telegram enforces a 4096-character message limit (the webhook Lambda splits
  longer responses at paragraph/sentence boundaries), and the "unavailable" message is
  returned (with HTTP 200 to Telegram) when the AgentCore Runtime invocation errors or the
  55-second timeout is exceeded.
- **Resolution:** For truncation, confirm the response is being split — check the webhook
  Lambda logs. For the unavailable message, inspect the runtime log group
  (`/aws/bedrock-agentcore/${StackName}-runtime`) for the underlying error or a slow model
  call, and consider a faster model via `ModelId`.

## Memory and container errors

### 1. Sprout does not remember earlier conversations

- **Symptom:** Facts stated in a previous session (plants, climate zone, preferences) are
  not recalled later.
- **Cause:** Either the session transcript was never persisted (a `CreateEvent` failure is
  logged but never blocks the reply, per graceful degradation), or the memory retrieval at
  session start timed out (3-second budget) and the agent proceeded with no context.
- **Resolution:** Check the runtime logs for `CreateEvent failed` or
  `Memory retrieval ... proceeding without context` warnings. Verify `MEMORY_ID` is set on
  the runtime and that the `RuntimeExecutionRole` grants the AgentCore Memory data-plane
  actions. Remember that long-term records are extracted asynchronously server-side, so
  they may take a short while to appear after a session ends.

### 2. Container health check failing / runtime not ready

- **Symptom:** Invocations fail, or the runtime never becomes healthy; `GET /ping` does
  not return 200.
- **Cause:** The container failed to start — most often a missing dependency, a community
  skill that could not be installed at build time, or the image built for the wrong
  architecture.
- **Resolution:** Rebuild locally and watch the logs
  (`docker buildx build --platform linux/arm64 ...`). Confirm every entry in
  `community-skills.json` is resolvable — an unfound skill fails the Docker build by
  design. Verify the image is `linux/arm64`. Check the runtime log group for startup
  stack traces.

### 3. Access-denied errors in the runtime logs

- **Symptom:** Runtime logs show `AccessDenied` on `bedrock:InvokeModel`,
  AgentCore Memory calls, S3, or KMS.
- **Cause:** The `ModelId` was changed to a model not covered by the role's scoped ARNs,
  or a resource ARN drifted from what the `RuntimeExecutionRole` policies allow (model
  ARNs, `memory/*`, the workspace bucket, or the KMS key).
- **Resolution:** Ensure `ModelId` matches a valid model/inference profile — the role
  scopes permissions to `foundation-model/${ModelId}` and `inference-profile/${ModelId}`,
  so changing the model updates the grants on the next stack update. For S3/KMS errors,
  confirm the workspace bucket policy and KMS key policy still reference the runtime role.

### 4. Workspace changes are lost between invocations

- **Symptom:** Files written to the OpenClaw workspace do not persist across container
  freezes.
- **Cause:** `WORKSPACE_BUCKET` is unset (persistence becomes a no-op), or an S3
  download/upload failed. Upload failures are logged and swallowed so the user still gets a
  reply, which means the update is silently lost.
- **Resolution:** Confirm `WORKSPACE_BUCKET` is present on the runtime and that the bucket
  policy grants the runtime role `s3:GetObject`/`s3:PutObject`/`s3:ListBucket`. Look for
  `Workspace upload failed; update lost` warnings in the runtime logs.
