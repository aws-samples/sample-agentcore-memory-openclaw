"""Unit tests for proactive scheduling (``agent-container/server.py``).

Covers the memory-driven reminder scheduling helpers ported from the reference
"agent self-schedules via EventBridge Scheduler" design:

* :func:`server.derive_schedule_name` — deterministic, valid, unique per
  (chat_id, task) and length-capped.
* :func:`server.build_create_schedule_kwargs` — the pure boto3
  ``create_schedule`` request builder (GroupName, Target Arn/RoleArn, Input JSON
  carrying ``chat_ids``/``task_type``, unique Name, ScheduleExpression
  passthrough, FlexibleTimeWindow OFF).
* :class:`server.SproutScheduler` — delegates request building and calls
  boto3 ``create_schedule`` with graceful degradation, plus ``from_env``
  configuration.

Everything is exercised with injected fakes — no real AWS.
"""

from __future__ import annotations

import json
import os
import sys

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402  (path setup must precede the import)
from server import (  # noqa: E402
    SproutScheduler,
    build_create_schedule_kwargs,
    derive_schedule_name,
)

GROUP = "sprout-telegram-cron"
INVOKER_ARN = "arn:aws:lambda:us-east-1:111122223333:function:sprout-telegram-cron"
ROLE_ARN = "arn:aws:iam::111122223333:role/sprout-telegram-scheduler-role"

_SCHEDULE_NAME_RE = __import__("re").compile(r"^[0-9a-zA-Z._-]{1,64}$")


# =============================================================================
# derive_schedule_name
# =============================================================================
def test_schedule_name_is_deterministic_and_valid():
    name1 = derive_schedule_name("12345", "watering")
    name2 = derive_schedule_name("12345", "watering")
    assert name1 == name2  # deterministic -> update, not duplicate
    assert _SCHEDULE_NAME_RE.match(name1)
    assert "12345" in name1 and "watering" in name1


def test_schedule_name_unique_per_chat_and_task():
    base = derive_schedule_name("12345", "watering")
    assert base != derive_schedule_name("99999", "watering")  # differs by chat
    assert base != derive_schedule_name("12345", "feeding")  # differs by task


def test_schedule_name_sanitizes_invalid_characters():
    name = derive_schedule_name("chat 42", "water the roses!")
    assert _SCHEDULE_NAME_RE.match(name)
    assert " " not in name and "!" not in name


def test_schedule_name_is_capped_at_64_chars():
    name = derive_schedule_name("1" * 80, "x" * 80)
    assert len(name) <= server.MAX_SCHEDULE_NAME_LENGTH
    assert _SCHEDULE_NAME_RE.match(name)


def test_schedule_name_falls_back_when_tokens_empty():
    name = derive_schedule_name("", "")
    assert _SCHEDULE_NAME_RE.match(name)


# =============================================================================
# build_create_schedule_kwargs (pure request builder)
# =============================================================================
def test_build_kwargs_shape_and_target():
    kwargs = build_create_schedule_kwargs(
        chat_id="12345",
        schedule_expression="at(2025-06-01T09:00:00)",
        task="watering",
        group_name=GROUP,
        invoker_arn=INVOKER_ARN,
        role_arn=ROLE_ARN,
    )

    assert kwargs["GroupName"] == GROUP
    assert kwargs["Name"] == derive_schedule_name("12345", "watering")
    assert kwargs["ScheduleExpression"] == "at(2025-06-01T09:00:00)"
    assert kwargs["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert kwargs["ActionAfterCompletion"] == "DELETE"  # fired reminders self-clean
    assert kwargs["Target"]["Arn"] == INVOKER_ARN
    assert kwargs["Target"]["RoleArn"] == ROLE_ARN


def test_build_kwargs_input_payload_carries_chat_id_and_task():
    kwargs = build_create_schedule_kwargs(
        chat_id="777",
        schedule_expression="cron(0 9 * * ? *)",
        task="frost_check",
        group_name=GROUP,
        invoker_arn=INVOKER_ARN,
        role_arn=ROLE_ARN,
    )

    payload = json.loads(kwargs["Target"]["Input"])
    assert payload == {"chat_ids": ["777"], "task_type": "frost_check"}


def test_build_kwargs_passes_through_schedule_expression_variants():
    for expr in ("at(2025-01-01T00:00:00)", "rate(1 days)", "cron(0 12 * * ? *)"):
        kwargs = build_create_schedule_kwargs(
            chat_id="1",
            schedule_expression=expr,
            task="t",
            group_name=GROUP,
            invoker_arn=INVOKER_ARN,
            role_arn=ROLE_ARN,
        )
        assert kwargs["ScheduleExpression"] == expr


def test_build_kwargs_description_optional():
    without = build_create_schedule_kwargs(
        chat_id="1", schedule_expression="rate(1 days)", task="t",
        group_name=GROUP, invoker_arn=INVOKER_ARN, role_arn=ROLE_ARN,
    )
    assert "Description" not in without

    with_desc = build_create_schedule_kwargs(
        chat_id="1", schedule_expression="rate(1 days)", task="t",
        group_name=GROUP, invoker_arn=INVOKER_ARN, role_arn=ROLE_ARN,
        description="Water the basil",
    )
    assert with_desc["Description"] == "Water the basil"


# =============================================================================
# SproutScheduler
# =============================================================================
class _FakeSchedulerClient:
    """Records create_schedule kwargs; optionally raises to simulate failure."""

    def __init__(self, *, exc=None):
        self._exc = exc
        self.create_kwargs = None

    def create_schedule(self, **kwargs):
        self.create_kwargs = kwargs
        if self._exc is not None:
            raise self._exc
        return {"ScheduleArn": "arn:aws:scheduler:us-east-1:111122223333:schedule/g/n"}


def _scheduler(client):
    return SproutScheduler(
        group_name=GROUP, invoker_arn=INVOKER_ARN, role_arn=ROLE_ARN, client=client
    )


def test_scheduler_build_kwargs_uses_configured_target_and_group():
    sched = _scheduler(_FakeSchedulerClient())
    kwargs = sched.build_kwargs(
        chat_id="55", schedule_expression="rate(7 days)", task="pruning"
    )
    assert kwargs["GroupName"] == GROUP
    assert kwargs["Target"]["Arn"] == INVOKER_ARN
    assert kwargs["Target"]["RoleArn"] == ROLE_ARN
    assert json.loads(kwargs["Target"]["Input"]) == {
        "chat_ids": ["55"],
        "task_type": "pruning",
    }


def test_scheduler_create_schedule_calls_client_and_returns_true():
    client = _FakeSchedulerClient()
    sched = _scheduler(client)

    ok = sched.create_schedule(
        chat_id="55", schedule_expression="rate(7 days)", task="pruning"
    )

    assert ok is True
    assert client.create_kwargs["Name"] == derive_schedule_name("55", "pruning")
    assert client.create_kwargs["GroupName"] == GROUP


def test_scheduler_create_schedule_degrades_on_error():
    client = _FakeSchedulerClient(exc=RuntimeError("boom"))
    sched = _scheduler(client)

    ok = sched.create_schedule(
        chat_id="55", schedule_expression="rate(7 days)", task="pruning"
    )

    assert ok is False  # logged and swallowed, no raise


def test_scheduler_from_env_returns_none_when_unset(monkeypatch):
    for var in (
        server.CRON_SCHEDULER_GROUP_ENV,
        server.CRON_INVOKER_FUNCTION_ARN_ENV,
        server.CRON_SCHEDULER_ROLE_ARN_ENV,
    ):
        monkeypatch.delenv(var, raising=False)

    assert SproutScheduler.from_env() is None


def test_scheduler_from_env_builds_when_configured(monkeypatch):
    monkeypatch.setenv(server.CRON_SCHEDULER_GROUP_ENV, GROUP)
    monkeypatch.setenv(server.CRON_INVOKER_FUNCTION_ARN_ENV, INVOKER_ARN)
    monkeypatch.setenv(server.CRON_SCHEDULER_ROLE_ARN_ENV, ROLE_ARN)

    sched = SproutScheduler.from_env()

    assert isinstance(sched, SproutScheduler)
    kwargs = sched.build_kwargs(
        chat_id="1", schedule_expression="rate(1 days)", task="t"
    )
    assert kwargs["GroupName"] == GROUP
    assert kwargs["Target"]["Arn"] == INVOKER_ARN
    assert kwargs["Target"]["RoleArn"] == ROLE_ARN


# =============================================================================
# Schedule-directive bridge: parsing + is_valid_schedule_expression
# =============================================================================
from server import (  # noqa: E402
    is_valid_schedule_expression,
    parse_schedule_directives,
)


def test_is_valid_schedule_expression_accepts_known_forms():
    assert is_valid_schedule_expression("at(2025-06-01T09:00:00)")
    assert is_valid_schedule_expression("rate(3 days)")
    assert is_valid_schedule_expression("cron(0 9 ? * SUN *)")
    assert is_valid_schedule_expression("  RATE(1 hours)  ")  # trimmed + case


def test_is_valid_schedule_expression_rejects_garbage():
    assert not is_valid_schedule_expression("")
    assert not is_valid_schedule_expression("every sunday at 9")
    assert not is_valid_schedule_expression("cron(0 9 ? * SUN *")  # no closing paren
    assert not is_valid_schedule_expression("drop table schedules")


def test_parse_directives_extracts_and_strips_single_tag():
    text = (
        "Got it — I'll remind you every Sunday morning. 🌱\n"
        '[[SCHEDULE expr="cron(0 9 ? * SUN *)" task="watering"]]'
    )
    cleaned, directives = parse_schedule_directives(text)
    assert directives == [{"expr": "cron(0 9 ? * SUN *)", "task": "watering"}]
    assert "[[SCHEDULE" not in cleaned
    assert cleaned == "Got it — I'll remind you every Sunday morning. 🌱"


def test_parse_directives_handles_multiple_tags():
    text = (
        'sure!\n[[SCHEDULE expr="rate(3 days)" task="watering"]]\n'
        '[[SCHEDULE expr="cron(0 8 ? * MON *)" task="feeding"]]'
    )
    cleaned, directives = parse_schedule_directives(text)
    assert len(directives) == 2
    assert {"expr": "rate(3 days)", "task": "watering"} in directives
    assert {"expr": "cron(0 8 ? * MON *)", "task": "feeding"} in directives
    assert "[[SCHEDULE" not in cleaned


def test_parse_directives_task_defaults_to_reminder():
    cleaned, directives = parse_schedule_directives('[[SCHEDULE expr="rate(1 days)"]]')
    assert directives == [{"expr": "rate(1 days)", "task": "reminder"}]
    assert cleaned == ""


def test_parse_directives_drops_invalid_expr_but_strips_tag():
    text = 'nope [[SCHEDULE expr="whenever" task="x"]] still here'
    cleaned, directives = parse_schedule_directives(text)
    assert directives == []  # invalid expr -> not scheduled
    assert "[[SCHEDULE" not in cleaned  # ...but tag still removed from reply
    assert "still here" in cleaned


def test_parse_directives_no_tag_returns_text_unchanged():
    cleaned, directives = parse_schedule_directives("just a normal reply")
    assert directives == []
    assert cleaned == "just a normal reply"


# =============================================================================
# handle_invocation wiring: directives -> create_schedule + stripped reply
# =============================================================================
from server import (  # noqa: E402
    Confidence,
    MemoryContextRecord,
    SproutRuntime,
)


class _FakeMemory:
    def __init__(self, records=None):
        self._records = records or []
        self.persisted = None

    def retrieve(self, chat_id, query):
        return list(self._records)

    def persist(self, chat_id, session_id, messages):
        self.persisted = messages
        return True


class _FakeWorkspace:
    def download(self, chat_id, dest_dir):
        return True

    def upload(self, chat_id, source_dir):
        return True


class _FakeAgent:
    def __init__(self, response):
        self._response = response

    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        return self._response


class _RecordingScheduler:
    def __init__(self, *, ok=True):
        self._ok = ok
        self.calls = []

    def create_schedule(self, *, chat_id, schedule_expression, task):
        self.calls.append(
            {"chat_id": chat_id, "expr": schedule_expression, "task": task}
        )
        return self._ok


def _runtime_with_scheduler(agent, scheduler):
    return SproutRuntime(
        memory=_FakeMemory(),
        workspace=_FakeWorkspace(),
        agent=agent,
        base_persona="You are Sprout.",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        scheduler=scheduler,
    )


def test_invocation_creates_schedule_from_directive_and_strips_reply():
    agent = _FakeAgent(
        "Got it — every Sunday 9am. 🌱\n"
        '[[SCHEDULE expr="cron(0 9 ? * SUN *)" task="watering"]]'
    )
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation(
        {"user_id": "12345", "message": "remind me to water every Sunday 9am"}
    )

    assert result["ok"] is True
    assert result["metadata"]["schedules_created"] == 1
    assert "[[SCHEDULE" not in result["text"]
    assert scheduler.calls == [
        {"chat_id": "12345", "expr": "cron(0 9 ? * SUN *)", "task": "watering"}
    ]
    # The persisted transcript uses the cleaned reply (no directive markup).
    assert "[[SCHEDULE" not in runtime._memory.persisted[1]["content"]


def test_invocation_directive_without_scheduler_still_strips_tag():
    agent = _FakeAgent('ok\n[[SCHEDULE expr="rate(1 days)" task="watering"]]')
    runtime = SproutRuntime(
        memory=_FakeMemory(),
        workspace=_FakeWorkspace(),
        agent=agent,
        base_persona="You are Sprout.",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        scheduler=None,  # scheduling not configured
    )

    result = runtime.handle_invocation({"user_id": "1", "message": "remind me daily"})

    assert result["metadata"]["schedules_created"] == 0
    assert "[[SCHEDULE" not in result["text"]
    assert result["text"] == "ok"


def test_invocation_directive_only_reply_gets_confirmation():
    agent = _FakeAgent('[[SCHEDULE expr="rate(2 days)" task="watering"]]')
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation({"user_id": "1", "message": "remind me"})

    assert result["metadata"]["schedules_created"] == 1
    assert result["text"]  # non-empty confirmation substituted
    assert "[[SCHEDULE" not in result["text"]


def test_invocation_no_directive_creates_no_schedule():
    agent = _FakeAgent("Here's how to water your basil.")
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation(
        {"user_id": "1", "message": "how do I water basil?"}
    )

    assert result["metadata"]["schedules_created"] == 0
    assert scheduler.calls == []
    assert result["text"] == "Here's how to water your basil."


# =============================================================================
# Deterministic schedule extraction (second pass): pure helpers
# =============================================================================
from server import (  # noqa: E402
    build_schedule_extraction_user_prompt,
    has_scheduling_intent,
    parse_extracted_schedules,
)


def test_has_scheduling_intent_detects_keywords():
    assert has_scheduling_intent("remind me to water every day at 9")
    assert has_scheduling_intent("can you nudge me tomorrow?")
    assert has_scheduling_intent("Reminder set for 5 minutes from now")
    assert has_scheduling_intent("set a weekly schedule")


def test_has_scheduling_intent_false_for_plain_chat():
    assert not has_scheduling_intent("how much should I water my basil?")
    assert not has_scheduling_intent("what's wrong with these leaves?")
    assert not has_scheduling_intent("")


def test_extraction_user_prompt_includes_turn_and_now():
    prompt = build_schedule_extraction_user_prompt(
        user_message="remind me in 5 min",
        assistant_reply="Reminder set for 20:02 UTC.",
        now_iso="2026-07-23T19:57:00Z",
    )
    assert "2026-07-23T19:57:00Z" in prompt
    assert "remind me in 5 min" in prompt
    assert "Reminder set for 20:02 UTC." in prompt


def test_parse_extracted_schedules_valid_json():
    raw = '{"schedules": [{"expr": "at(2026-07-23T20:02:00)", "task": "check_basil"}]}'
    assert parse_extracted_schedules(raw) == [
        {"expr": "at(2026-07-23T20:02:00)", "task": "check_basil"}
    ]


def test_parse_extracted_schedules_tolerates_prose_and_fences():
    raw = (
        "Here you go:\n```json\n"
        '{"schedules": [{"expr": "rate(3 days)", "task": "watering"}]}\n'
        "```\nHope that helps!"
    )
    assert parse_extracted_schedules(raw) == [
        {"expr": "rate(3 days)", "task": "watering"}
    ]


def test_parse_extracted_schedules_empty_and_invalid():
    assert parse_extracted_schedules('{"schedules": []}') == []
    assert parse_extracted_schedules("not json at all") == []
    assert parse_extracted_schedules("") == []
    # Non-list schedules value.
    assert parse_extracted_schedules('{"schedules": "nope"}') == []


def test_parse_extracted_schedules_drops_invalid_expr_and_defaults_task():
    raw = (
        '{"schedules": ['
        '{"expr": "whenever", "task": "x"},'
        '{"expr": "rate(1 days)"}'
        ']}'
    )
    # Bad expr dropped; missing task defaults to "reminder".
    assert parse_extracted_schedules(raw) == [{"expr": "rate(1 days)", "task": "reminder"}]


def test_parse_extracted_schedules_drops_past_one_off_at():
    import datetime as _dt

    now = _dt.datetime(2026, 7, 23, 20, 0, 0, tzinfo=_dt.timezone.utc)
    raw = (
        '{"schedules": ['
        '{"expr": "at(2026-07-23T12:00:00)", "task": "watering"},'  # past today
        '{"expr": "at(2026-07-24T08:00:00)", "task": "feeding"}'    # future
        ']}'
    )
    # Only the future one-off survives when ``now`` is supplied.
    assert parse_extracted_schedules(raw, now=now) == [
        {"expr": "at(2026-07-24T08:00:00)", "task": "feeding"}
    ]


def test_parse_extracted_schedules_keeps_recurring_regardless_of_now():
    import datetime as _dt

    now = _dt.datetime(2026, 7, 23, 20, 0, 0, tzinfo=_dt.timezone.utc)
    raw = '{"schedules": [{"expr": "cron(0 12 * * ? *)", "task": "watering"}]}'
    # Recurring cron/rate have no single instant to be "past" -> always kept.
    assert parse_extracted_schedules(raw, now=now) == [
        {"expr": "cron(0 12 * * ? *)", "task": "watering"}
    ]


# =============================================================================
# handle_invocation wiring: extraction fallback (no directive emitted)
# =============================================================================
class _ExtractingAgent:
    """Fake agent that emits no directive but returns schedules from the pass."""

    def __init__(self, response, extracted):
        self._response = response
        self._extracted = extracted
        self.extract_calls = []

    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        return self._response

    def extract_schedule_directives(self, *, user_message, assistant_reply, now_iso):
        self.extract_calls.append(
            {"user_message": user_message, "assistant_reply": assistant_reply, "now_iso": now_iso}
        )
        return list(self._extracted)


def test_invocation_extraction_fallback_creates_schedule_without_directive():
    # Model agreed in prose ("Reminder set...") but emitted no [[SCHEDULE]] tag.
    agent = _ExtractingAgent(
        "Perfect! ✅ Reminder set for 5 minutes from now (20:02 UTC).",
        [{"expr": "at(2026-07-23T20:02:00)", "task": "check_basil"}],
    )
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation(
        {"user_id": "12345", "message": "remind me to check basil in 5 minutes"}
    )

    assert result["metadata"]["schedules_created"] == 1
    assert scheduler.calls == [
        {"chat_id": "12345", "expr": "at(2026-07-23T20:02:00)", "task": "check_basil"}
    ]
    # The extraction pass saw the user message and the assistant reply.
    assert agent.extract_calls[0]["user_message"] == "remind me to check basil in 5 minutes"
    assert "Reminder set" in agent.extract_calls[0]["assistant_reply"]


def test_invocation_extraction_fallback_not_run_without_intent():
    agent = _ExtractingAgent("Your basil looks healthy!", [{"expr": "rate(1 days)", "task": "x"}])
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation(
        {"user_id": "1", "message": "how does my basil look?"}
    )

    # No scheduling keyword in the turn -> extraction pass never runs.
    assert agent.extract_calls == []
    assert result["metadata"]["schedules_created"] == 0
    assert scheduler.calls == []


def test_invocation_extraction_fallback_skipped_when_directive_already_created():
    # A directive is present -> schedules_created>0 -> no extra extraction pass.
    agent = _ExtractingAgent(
        'sure!\n[[SCHEDULE expr="rate(1 days)" task="watering"]]',
        [{"expr": "rate(2 days)", "task": "should_not_be_used"}],
    )
    scheduler = _RecordingScheduler()
    runtime = _runtime_with_scheduler(agent, scheduler)

    result = runtime.handle_invocation({"user_id": "1", "message": "remind me daily"})

    assert result["metadata"]["schedules_created"] == 1
    assert agent.extract_calls == []  # directive path already handled it
    assert scheduler.calls[0]["expr"] == "rate(1 days)"
