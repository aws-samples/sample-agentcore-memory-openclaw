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
| `MEMORY_ID` | `AgentCoreMemory.MemoryId` | AgentCore Memory resource ID targeted by all data-plane calls — `RetrieveMemoryRecords`, `CreateEvent`, and `BatchCreateMemoryRecords`. |
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

## Memory configuration

The `AgentCoreMemory` resource in the template is configured in three parts —
namespaces, indexed keys, and the per-strategy metadata schema — all of which affect
what the agent can recall and how precisely.

### Namespaces

Both extraction strategies write to the same per-user namespace:

| Strategy | Namespace |
| --- | --- |
| `UserPreferenceMemoryStrategy` | `sprout/{actorId}/long_term` |
| `SemanticMemoryStrategy` | `sprout/{actorId}/long_term` |

The runtime retrieves with the **`namespacePath`** parameter rather than
`namespace`. Today the two are equivalent, since both strategies write directly to
that namespace — but `namespace` matches only the *exact* value given, so anything
stored deeper in the subtree would be silently omitted. Using the path form keeps
retrieval correct if a nested strategy is added later.

> There is deliberately **no** `SummaryMemoryStrategy`. Session summaries restated,
> in looser prose, facts the semantic and user-preference strategies already
> extract, while costing extraction on every session and accumulating one record per
> session that competes for the capped retrieval budget. If you add one back, its
> namespace **must** end in `{sessionId}` — the service rejects a summarization
> namespace without it — which is exactly the nested case `namespacePath` handles.

### Indexed keys

`IndexedKeys` declares which metadata keys may be used in a server-side
`metadataFilters` expression. Sprout indexes:

| Key | Type | Used for |
| --- | --- | --- |
| `type` | `STRING` | Separating sections, plants, conditions, and care |
| `section` | `STRING` | Scoping retrieval to one backyard area |
| `plants` | `STRINGLIST` | `CONTAINS` lookups such as "which beds have basil?" |

Filters on indexed keys are applied **before** the vector search, so they narrow the
candidate set rather than trimming whatever similarity returned. Filtering on a
non-indexed key raises `ValidationException`; such metadata (for example
`photo_count`) is still stored on the record and returned by
`Get`/`ListMemoryRecords`, and `server.py` filters it in-process instead.

> Indexed keys are **additive-only and cannot be removed** once added, with a limit
> of 10 per memory resource. Add them deliberately, and reserve them for dimensions
> you actually filter on.

### Metadata schema (extraction instructions)

Each strategy declares a `MemoryRecordSchema.MetadataSchema`, which is how the
extraction model is instructed:

- `Definition` — what the field means (this is the primary instruction).
- `LlmExtractionInstruction` — extra guidance and conflict resolution; the built-in
  `LATEST_VALUE` keeps the most recent value when events disagree.
- `Validation.AllowedValues` / `MaxItems` — constrains the output so filter values
  stay consistent (without it the model may emit `Herbs`, `herbs`, and `HERB` for
  one concept and break downstream matching).

Keys whose values the application already knows are also attached to the event via
`CreateEvent` metadata (see `build_event_metadata` in `server.py`), so extraction
propagates them onto derived records instead of re-inferring them from prose. Note
that `CreateEvent` metadata is `stringValue`-only, so list-valued dimensions such as
`plants` are supplied on records written directly with `BatchCreateMemoryRecords`.

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
