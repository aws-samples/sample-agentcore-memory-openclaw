# Telegram bot setup

Sprout talks to you through a Telegram bot. Before you deploy, you need to create that bot
with BotFather and grab its API token. This guide walks through it end to end.

## 1. Create a bot with BotFather

BotFather is Telegram's official bot for creating and managing bots.

1. Open Telegram and search for **@BotFather** (look for the blue verified checkmark), then
   open the chat.
2. Send `/start` to see the list of commands.
3. Send `/newbot` to begin creating a bot.
4. When prompted, enter a **display name** for your bot (for example `Sprout Garden
   Assistant`). This is what users see at the top of the chat.
5. When prompted, enter a **username**. It must be unique and end in `bot` (for example
   `sprout_garden_bot`). If the name is taken, BotFather asks you to try another.

Once the username is accepted, BotFather creates the bot and replies with a confirmation
message that includes your bot's token.

## 2. Retrieve the bot token

The token appears in BotFather's success message, on a line similar to:

```
Use this token to access the HTTP API:
123456789:AAExampleToken...NOT-A-REAL-KEY
```

That full string (the numeric ID, a colon, then the secret) is your **`TelegramBotToken`**.

Tips:

- Treat the token like a password. Anyone with it can control your bot. Do not commit it to
  source control or paste it into logs.
- To view it again later, message BotFather, send `/mybots`, choose your bot, then select
  **API Token**.
- To rotate it, use BotFather's **Revoke current token** option under the same menu. If you
  rotate the token, update the stack (see below) so Sprout uses the new value.

## 3. Supply the token during deployment

The token is passed to the stack as the `TelegramBotToken` CloudFormation parameter. It is
marked `NoEcho`, so it never shows up in the console, stack events, or CLI output, and it is
stored encrypted in AWS Secrets Manager (using the stack's KMS key). There are two ways to
provide it.

### Option A — Deploy script (recommended)

Export the token as an environment variable and run the deploy script. The script passes it
through as the `TelegramBotToken` parameter and also uses it to register the webhook with
Telegram at the end:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAExampleToken...NOT-A-REAL-KEY"
scripts/deploy.sh
```

You can set other parameters via their environment variables at the same time, for example:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AAExample..."
export ALERT_EMAIL="you@example.com"
export STACK_NAME="mygarden"
scripts/deploy.sh
```

### Option B — CloudFormation console / CLI

If you deploy the template directly through the AWS console or CLI, paste the token into the
**TelegramBotToken** parameter field on the stack parameters page. Because the field is
`NoEcho`, it is masked as you type and is not displayed afterward.

Note that this path also requires you to supply a working **ContainerImageUri** — the default
is a placeholder, since this sample does not publish a prebuilt image. Build and push the
agent image to your own ECR first (the deploy script does this for you).

When deploying through the console, the webhook is **not** registered automatically. After
the stack reaches `CREATE_COMPLETE`, copy the `WebhookUrl` from the stack **Outputs** and
register it with Telegram:

```bash
curl "https://api.telegram.org/bot<TelegramBotToken>/setWebhook?url=<WebhookUrl>"
```

A successful response looks like `{"ok":true,"result":true,"description":"Webhook was set"}`.

## 4. Verify

- Confirm the webhook is registered:
  `curl "https://api.telegram.org/bot<TelegramBotToken>/getWebhookInfo"` — the `url` field
  should match your stack's `WebhookUrl`.
- Open your bot in Telegram (search for the username you chose) and send a message. Sprout
  should reply.

If it does not respond, see [troubleshooting.md](./troubleshooting.md#webhook-issues).
