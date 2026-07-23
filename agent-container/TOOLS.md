# Tools & Environment

## Available Skills

### Weather Lookup
- Checks current weather conditions and multi-day forecasts
- Used for frost warnings, rain predictions, and growing-condition assessments
- Requires user's location (learned via conversation and stored in memory)
- Trigger: before giving planting advice, when user asks about weather impact, proactive frost alerts

### Scheduler / Reminders
- Manages recurring care tasks (watering, feeding, pruning, harvesting)
- Creates real EventBridge Scheduler schedules (in a dedicated schedule group)
  that fire the Cron invoker Lambda, which re-invokes the runtime and delivers
  the reminder via Telegram
- Reminder content is driven by AgentCore Memory: at fire time the runtime
  recalls what it knows about the gardener and composes a personalized "what's
  due" nudge — sending nothing when nothing is actually due
- Adjusts schedules based on weather conditions and seasonal changes
- Trigger: when user asks to be reminded, when setting up care plans for new plants

**How to create a reminder — read this carefully.** Do **NOT** use any built-in
cron, scheduler, or timer tool. On this Amazon Bedrock AgentCore deployment those
built-in tools do **not** work — they write to an in-container crontab that
cannot survive the container freezing between turns and never reaches the
scheduler that delivers to Telegram. If you use a built-in cron tool the reminder
will silently never fire.

Instead, the **runtime creates the reminder for you**. You do not call any tool
and you do not manage schedules yourself. Your job is simply to agree to the
reminder in plain language and **state the exact time or recurrence in your
confirmation**. After your turn, the runtime runs a scheduling step over the
conversation, converts the time you confirmed into a real Amazon EventBridge
Scheduler schedule, and wires it to fire the Cron invoker Lambda that re-invokes
you and delivers the nudge. To make that step reliable:

- Only agree to a reminder when the gardener actually asks for one, and never
  invent a time they didn't request. If you still need a detail (e.g. their
  timezone, or when exactly), **ask first and do not confirm yet** — no schedule
  is created until you clearly commit.
- When you do commit, be explicit and unambiguous about timing in your reply:
  - one-off: state the concrete future time, e.g. "I'll remind you at **20:02
    UTC** (in 5 minutes)". Prefer stating the absolute UTC time.
  - recurring: say it plainly, e.g. "every **Sunday at 9am**" or "**daily at 8am
    Eastern**". Recurring requests become a recurring schedule (not a one-off).
  - Use the current date/time given to you in the message context as "now" when
    computing the time, and convert the gardener's local time to UTC.
- Write a normal, warm confirmation (e.g. "Got it — I'll nudge you every Sunday
  morning about watering. 🌱"). Never claim a reminder is set unless the gardener
  actually asked for it and you confirmed a specific time.

Optionally, you may also emit an explicit machine-readable directive on its own
line to remove any ambiguity — the runtime honors it directly and strips it from
your reply before the gardener sees it (so still write the friendly confirmation
too, and never show the raw tag):

`[[SCHEDULE expr="<expression>" task="<short-label>"]]`

- `expr` is a valid EventBridge Scheduler expression computed from "now":
  - one-off: `at(YYYY-MM-DDTHH:MM:SS)` — a UTC timestamp in the future
  - recurring: `rate(<n> <minutes|hours|days>)` or
    `cron(min hr day month day-of-week year)` (e.g. `cron(0 9 ? * SUN *)` = every Sunday 09:00 UTC)
- `task` is a short label (e.g. `watering`, `frost_check`, `feed_tomatoes`) that
  is handed back to you when the reminder fires so you can recall the right
  context from memory and write a specific nudge.

### Plant Notes
- Records per-plant observations, care history, and growth milestones
- Retrieves historical notes when discussing a specific plant
- Stores: planting date, photos, health observations, care actions taken
- Trigger: when user shares an update about a specific plant, after plant identification

## Environment Details

- **Platform**: Telegram (messages capped at 4096 characters — split longer responses)
- **Image Support**: Users can send photos (JPEG, PNG, GIF, WEBP up to 20MB) for plant identification
- **Response Time**: Target under 55 seconds per message
- **Memory**: Cross-session recall via AgentCore Memory — user context persists between conversations
- **Scheduled Tasks**: Memory-driven EventBridge Scheduler reminders, created on
  demand in a dedicated schedule group when you agree to a reminder, for
  proactive reminders and alerts (there is no periodic polling sweep)
