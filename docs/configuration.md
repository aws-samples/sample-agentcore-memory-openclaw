# Configuration

This page documents every knob you can turn when deploying and running Sprout: the
CloudFormation parameters you supply at deploy time, the environment variables that
flow into the AgentCore Runtime container and the Lambda functions, and how to switch
the Bedrock model.

## CloudFormation parameters

These parameters are defined in `openclaw-telegram.yaml` and are prompted for in the
CloudFormation console (or supplied via the `deploy.sh` script). Parameter validation
runs before any resources are created, so an out-of-range value is rejected up front.

| Parameter | Type | Default | Constraints | Description |
| --- | --- | --- | --- | --- |
| `TelegramBotToken` | String (`NoEcho`) | _(none — required)_ | Minimum length 1 | Telegram Bot API token issued by BotFather. Marked `NoEcho`, so it is never displayed in the console, stack events, or CLI output. Stored in Secrets Manager encrypted with the stack's KMS key. See [telegram-setup.md](./telegram-setup.md). |
| `ModelId` | String | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | Bedrock model ID or cross-region inference profile | Bedrock model the agent (and the memory extraction strategies) invoke for text chat. Defaults to the Claude Haiku 4.5 cross-region inference profile. See [Model switching](#model-switching). |
| `VisionModelId` | String | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock model ID or cross-region inference profile | Bedrock model used for plant image identification (vision). Sonnet is more accurate for visual tasks; the text `ModelId` stays on the faster/cheaper model to control cost. |
| `MonthlyBudgetLimit` | Number | `25` | Between `1` and `10000` | Monthly budget ceiling in USD. Drives the AWS Budgets resource that raises alerts at 80% and 100% of this amount. |
| `AlertEmail` | String | `''` (empty) | Valid email address, or empty | Email address subscribed to the SNS alert topic for budget and operational alarms. Leave empty to create the topic and alarms without an email subscription. |
| `LogRetentionDays` | Number | `30` | One of `1, 3, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400` | Retention period, in days, for the CloudWatch log groups of the webhook Lambda, cron Lambda, and AgentCore Runtime. |
| `ContainerImageUri` | String | `public.ecr.aws/<ECR_PUBLIC_ALIAS>/sprout-agent:latest` | Any container image URI | Agent container image for the AgentCore Runtime. **The default is a placeholder, not a working image** — this sample publishes no prebuilt image, so each operator builds and hosts their own. `scripts/deploy.sh` overrides this with the image it builds and pushes to ECR in your account; supply your own URI when deploying the template directly. |

### How parameters are supplied

- **CloudFormation console / CLI** — each parameter appears as a field you fill in before
  creating the stack. This path also requires a working `ContainerImageUri` (the default is
  a placeholder; see the note in the table above).
- **`scripts/deploy.sh`** — the script reads most parameters from environment variables
  (`TELEGRAM_BOT_TOKEN`, `MODEL_ID`, `VISION_MODEL_ID`, `MONTHLY_BUDGET_LIMIT`,
  `ALERT_EMAIL`, `LOG_RETENTION_DAYS`) and passes them through as
  `--parameter-overrides`. Only `TELEGRAM_BOT_TOKEN` is required; the rest fall back to
  the same defaults as the template.

## Environment variables

Environment variables come in two groups: those injected into the AgentCore Runtime
container (set by the `AgentCoreRuntime` resource) and those injected into the Lambda
functions. There are also container-only runtime tuning variables read by `server.py`.

### AgentCore Runtime container

Set by the `EnvironmentVariables` block of the `AgentCoreRuntime` resource in
`openclaw-telegram.yaml`:

| Variable | Source | Description |
| --- | --- | --- |
| `MODEL_ID` | `ModelId` parameter | Bedrock model / cross-region inference profile the agent and prompt-cached Converse calls use for text. Read by `server.py` at invocation time. |
| `VISION_MODEL_ID` | `VisionModelId` parameter | Bedrock model used for plant image identification (vision). Read by `server.py` when an invocation includes images. |
| `MEMORY_ID` | `AgentCoreMemory.MemoryId` | AgentCore Memory resource ID targeted by all `RetrieveMemoryRecords` and `CreateEvent` data-plane calls. |
| `WORKSPACE_BUCKET` | `WorkspaceBucket` | S3 bucket used to persist the OpenClaw workspace between container freezes. When unset, workspace persistence becomes a no-op. |

Additional runtime tuning variables read by `server.py` (not set by the template today —
override them via a stack update to the runtime's `EnvironmentVariables` if you need to
change the defaults):

| Variable | Default | Description |
| --- | --- | --- |
| `ENABLE_PROMPT_CACHE` | `true` | Enables Bedrock prompt caching. When enabled, a `cachePoint` is placed after the system prompt (persona + memory context) so the static prefix is cached across invocations (5-minute TTL that refreshes on each call), cutting cost and latency on cached input tokens. Set to `false` to disable. |
| `LOG_LEVEL` | `INFO` | Python logging level for the container. |
| `PORT` | `8080` | Port the HTTP server listens on. AgentCore Runtime expects `8080`; change only for local testing. |

### Lambda functions (webhook and cron)

Set by the `Environment.Variables` block of the `WebhookLambda` and `CronLambda`
resources:

| Variable | Source | Description |
| --- | --- | --- |
| `AGENTCORE_RUNTIME_ARN` | `AgentCoreRuntime.AgentRuntimeArn` | ARN the Lambda invokes via `InvokeAgentRuntime` to run the agent. |
| `BOT_TOKEN_SECRET_ARN` | `BotTokenSecret` | Secrets Manager ARN the Lambda reads (uncached, always latest) to obtain the Telegram bot token. |
| `TELEGRAM_API_BASE` | Literal `https://api.telegram.org` | Base URL for Telegram Bot API calls (`sendMessage`, `getFile`, etc.). |

## Model switching

The model is fully parameterized, so you can switch it without rebuilding or re-pushing
the container image.

1. Choose a Bedrock model ID or cross-region inference profile. Cross-region inference
   profiles are prefixed with `us.` (for example `us.anthropic.claude-haiku-4-5-20251001-v1:0`)
   and auto-route requests across regions for higher throughput.
2. Update the stack with the new `ModelId`:
   - **Console** — update the stack and change the `ModelId` parameter value.
   - **Script** — re-run the deploy with the override, e.g.
     `MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0 TELEGRAM_BOT_TOKEN=... scripts/deploy.sh`.
3. On the next invocation the container reads the updated `MODEL_ID` environment variable
   and uses the new model. No container image rebuild or ECR push is required.

Notes:

- The `ModelId` you choose is scoped into both the `RuntimeExecutionRole` and the
  `MemoryExecutionRole` IAM policies (as `foundation-model/${ModelId}` and
  `inference-profile/${ModelId}` ARNs), so the same model is authorized for live agent
  calls and for the asynchronous memory-extraction strategies.
- If the value does not match a valid Bedrock model ARN or inference profile pattern,
  the deployment is rejected with a parameter validation error.
- Multimodal features (plant photo identification) require a vision-capable model such as
  Claude Haiku 4.5.
