"""Unit tests for the Sprout Cron Lambda (``cron_handler.py``).

Covers the scheduled-task path exercised by :func:`cron_handler.lambda_handler`:

* EventBridge Scheduler payload parsing — ``task_type``/``chat_ids`` extraction,
  default task type, and malformed/empty payloads (Req 11.2).
* Per-chat AgentCore Runtime invocation — one cron-type payload
  (``invocation_type="cron"``) per chat id with the chat id as ``user_id`` and a
  session id satisfying the runtime's minimum length (Req 11.2).
* Message delivery and retry accounting — successful text is delivered via
  Telegram, non-success/empty runtime output is skipped, and failed delivery is
  counted (Req 11.3 / 11.5).
* Timeout handling — the handler stops launching new work once the remaining
  Lambda budget drops below the floor (Req 11.6).

Every external seam (the AgentCore Runtime data-plane client, bot-token
retrieval, and Telegram delivery) is injected as a fake, so the tests never
touch AWS or the network.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Make the Lambda modules importable without packaging them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cron_handler  # noqa: E402  (path setup must precede the import)
import telegram_api  # noqa: E402

BOT_TOKEN = "123456:TEST-BOT-TOKEN"  # nosec B105 - dummy test fixture, not a real token
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/sprout"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:bot-token"  # nosec B105 - dummy test ARN, not a secret


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Set the environment variables the cron handler requires."""
    monkeypatch.setenv(cron_handler.RUNTIME_ARN_ENV, RUNTIME_ARN)
    monkeypatch.setenv(cron_handler.BOT_TOKEN_SECRET_ARN_ENV, SECRET_ARN)


# --- Test doubles -------------------------------------------------------------
class _StreamBody:
    """A minimal streaming body exposing ``read`` like the runtime response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class FakeRuntimeClient:
    """AgentCore Runtime data-plane stand-in.

    ``replies`` maps a chat id (as ``user_id`` in the payload) to the reply text
    the runtime should produce, or to an ``Exception`` instance to simulate an
    invocation failure. When a chat id is absent from ``replies`` the
    ``default_reply`` is used. A reply of ``None`` produces a non-success
    envelope so the handler skips delivery. All invocation kwargs are recorded.
    """

    def __init__(self, replies=None, default_reply="Water your basil today."):
        self._replies = replies or {}
        self._default_reply = default_reply
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["payload"].decode("utf-8"))
        user_id = payload["user_id"]
        reply = self._replies.get(user_id, self._default_reply)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            envelope = json.dumps({"ok": False, "error": "nothing"}).encode("utf-8")
        else:
            envelope = json.dumps({"ok": True, "text": reply}).encode("utf-8")
        return {"response": _StreamBody(envelope)}


class FakeTelegram:
    """Records ``send_long_message`` calls and simulates delivery outcomes.

    ``failures`` is a set of chat ids for which delivery returns ``False``;
    ``errors`` is a set of chat ids for which delivery raises. All other chat
    ids deliver successfully (return ``True``).
    """

    def __init__(self, failures=None, errors=None):
        self._failures = set(failures or ())
        self._errors = set(errors or ())
        self.calls = []

    def send_long_message(self, chat_id, text, bot_token):
        self.calls.append({"chat_id": chat_id, "text": text, "bot_token": bot_token})
        if chat_id in self._errors:
            raise RuntimeError(f"telegram boom for {chat_id}")
        return chat_id not in self._failures


class FakeContext:
    """Lambda context stand-in with a configurable remaining-time budget.

    When ``remaining_ms`` is ``None`` the object does not expose
    ``get_remaining_time_in_millis`` at all (mirrors local invocation). When it
    is a list, successive calls pop the next value so a countdown can be
    simulated.
    """

    def __init__(self, remaining_ms):
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self):
        if isinstance(self._remaining, list):
            return self._remaining.pop(0)
        return self._remaining


class _NoTimerContext:
    """Context lacking the timing helper entirely."""


def _install(monkeypatch, runtime=None, telegram=None, token=BOT_TOKEN, token_error=None):
    """Wire fakes into the cron_handler module and return (runtime, telegram)."""
    runtime = runtime if runtime is not None else FakeRuntimeClient()
    telegram = telegram if telegram is not None else FakeTelegram()

    monkeypatch.setattr(cron_handler, "_get_agentcore_client", lambda: runtime)

    def _get_bot_token(secret_arn):
        if token_error is not None:
            raise token_error
        return token

    monkeypatch.setattr(cron_handler.telegram_api, "get_bot_token", _get_bot_token)
    monkeypatch.setattr(
        cron_handler.telegram_api, "send_long_message", telegram.send_long_message
    )
    return runtime, telegram


def _ctx(remaining_ms=10 * 60 * 1000):
    """A context with plenty of remaining budget by default."""
    return FakeContext(remaining_ms)


# --- EventBridge payload parsing (Req 11.2) ----------------------------------
def test_parse_event_extracts_task_type_and_chat_ids():
    task_type, chat_ids = cron_handler._parse_event(
        {"task_type": "morning_digest", "chat_ids": ["1", "2", "3"]}
    )
    assert task_type == "morning_digest"
    assert chat_ids == ["1", "2", "3"]


def test_parse_event_defaults_task_type_when_missing():
    task_type, chat_ids = cron_handler._parse_event({"chat_ids": [7]})
    assert task_type == cron_handler.DEFAULT_TASK_TYPE
    assert chat_ids == ["7"]


def test_parse_event_non_list_chat_ids_yields_empty():
    task_type, chat_ids = cron_handler._parse_event(
        {"task_type": "check", "chat_ids": "not-a-list"}
    )
    assert task_type == "check"
    assert chat_ids == []


def test_parse_event_skips_none_and_blank_chat_ids_and_strips():
    _, chat_ids = cron_handler._parse_event(
        {"chat_ids": [None, "  ", " 42 ", "", 99]}
    )
    assert chat_ids == ["42", "99"]


def test_parse_event_non_dict_event_returns_defaults_and_empty():
    task_type, chat_ids = cron_handler._parse_event("not-a-dict")
    assert task_type == cron_handler.DEFAULT_TASK_TYPE
    assert chat_ids == []


def test_empty_chat_ids_processes_nothing(monkeypatch):
    runtime, telegram = _install(monkeypatch)

    summary = cron_handler.lambda_handler({"task_type": "check", "chat_ids": []}, _ctx())

    assert summary == {
        "task_type": "check",
        "processed": 0,
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert runtime.calls == []
    assert telegram.calls == []


# --- Missing configuration ----------------------------------------------------
def test_missing_runtime_arn_returns_early_without_invoking(monkeypatch):
    monkeypatch.delenv(cron_handler.RUNTIME_ARN_ENV, raising=False)
    runtime, telegram = _install(monkeypatch)

    summary = cron_handler.lambda_handler(
        {"task_type": "check", "chat_ids": ["1"]}, _ctx()
    )

    assert summary["processed"] == 0
    assert runtime.calls == []
    assert telegram.calls == []


def test_missing_secret_arn_returns_early_without_invoking(monkeypatch):
    monkeypatch.delenv(cron_handler.BOT_TOKEN_SECRET_ARN_ENV, raising=False)
    runtime, telegram = _install(monkeypatch)

    summary = cron_handler.lambda_handler(
        {"task_type": "check", "chat_ids": ["1"]}, _ctx()
    )

    assert summary["processed"] == 0
    assert runtime.calls == []
    assert telegram.calls == []


# --- Bot token retrieval failure ---------------------------------------------
def test_get_bot_token_failure_returns_early_without_invoking(monkeypatch):
    runtime, telegram = _install(
        monkeypatch, token_error=RuntimeError("secrets manager down")
    )

    summary = cron_handler.lambda_handler(
        {"task_type": "check", "chat_ids": ["1", "2"]}, _ctx()
    )

    assert summary["processed"] == 0
    assert runtime.calls == []
    assert telegram.calls == []


# --- Runtime invocation per chat id (Req 11.2) -------------------------------
def test_runtime_invoked_once_per_chat_with_cron_payload(monkeypatch):
    runtime, telegram = _install(monkeypatch)

    cron_handler.lambda_handler(
        {"task_type": "morning_digest", "chat_ids": ["11", "22"]}, _ctx()
    )

    assert len(runtime.calls) == 2
    seen_user_ids = []
    for call in runtime.calls:
        assert call["agentRuntimeArn"] == RUNTIME_ARN
        assert len(call["runtimeSessionId"]) >= cron_handler._MIN_SESSION_ID_LENGTH
        payload = json.loads(call["payload"].decode("utf-8"))
        assert payload["invocation_type"] == "cron"
        assert payload["task_type"] == "morning_digest"
        assert payload["message"] == "morning_digest"
        assert payload["session_id"] == call["runtimeSessionId"]
        seen_user_ids.append(payload["user_id"])
    assert seen_user_ids == ["11", "22"]


# --- Delivery and retry accounting (Req 11.3 / 11.5) -------------------------
def test_successful_reply_is_delivered_via_telegram(monkeypatch):
    runtime, telegram = _install(
        monkeypatch, runtime=FakeRuntimeClient(replies={"5": "Prune the tomatoes."})
    )

    summary = cron_handler.lambda_handler({"chat_ids": ["5"]}, _ctx())

    assert summary["delivered"] == 1
    assert summary["skipped"] == 0
    assert summary["failed"] == 0
    assert len(telegram.calls) == 1
    assert telegram.calls[0]["chat_id"] == "5"
    assert telegram.calls[0]["text"] == "Prune the tomatoes."
    assert telegram.calls[0]["bot_token"] == BOT_TOKEN


def test_non_success_envelope_is_skipped_without_delivery(monkeypatch):
    # reply=None => the fake runtime returns an {"ok": False} envelope.
    runtime, telegram = _install(
        monkeypatch, runtime=FakeRuntimeClient(replies={"5": None})
    )

    summary = cron_handler.lambda_handler({"chat_ids": ["5"]}, _ctx())

    assert summary["processed"] == 1
    assert summary["skipped"] == 1
    assert summary["delivered"] == 0
    assert telegram.calls == []


def test_empty_text_envelope_is_skipped(monkeypatch):
    runtime, telegram = _install(
        monkeypatch, runtime=FakeRuntimeClient(replies={"5": "   "})
    )

    summary = cron_handler.lambda_handler({"chat_ids": ["5"]}, _ctx())

    assert summary["skipped"] == 1
    assert summary["delivered"] == 0
    assert telegram.calls == []


def test_runtime_exception_is_skipped(monkeypatch):
    runtime, telegram = _install(
        monkeypatch,
        runtime=FakeRuntimeClient(replies={"5": RuntimeError("runtime exploded")}),
    )

    summary = cron_handler.lambda_handler({"chat_ids": ["5"]}, _ctx())

    assert summary["processed"] == 1
    assert summary["skipped"] == 1
    assert summary["delivered"] == 0
    assert telegram.calls == []


def test_failed_delivery_is_counted(monkeypatch):
    runtime, telegram = _install(
        monkeypatch,
        runtime=FakeRuntimeClient(replies={"5": "deliverable text"}),
        telegram=FakeTelegram(failures={"5"}),
    )

    summary = cron_handler.lambda_handler({"chat_ids": ["5"]}, _ctx())

    assert summary["failed"] == 1
    assert summary["delivered"] == 0
    assert summary["skipped"] == 0
    assert len(telegram.calls) == 1


# --- Timeout / 120s budget handling (Req 11.6) -------------------------------
def test_low_remaining_budget_stops_before_processing_all_chats(monkeypatch):
    runtime, telegram = _install(monkeypatch)
    # Below the floor from the very first check: no chats should be processed.
    ctx = FakeContext(cron_handler._TIME_BUDGET_FLOOR_MS - 1)

    summary = cron_handler.lambda_handler(
        {"chat_ids": ["1", "2", "3"]}, ctx
    )

    assert summary["processed"] == 0
    assert summary["processed"] < 3
    assert runtime.calls == []


def test_budget_countdown_stops_partway_through(monkeypatch):
    runtime, telegram = _install(monkeypatch)
    floor = cron_handler._TIME_BUDGET_FLOOR_MS
    # First two checks pass, the third is below the floor -> only 2 processed.
    ctx = FakeContext([floor + 5000, floor + 1000, floor - 1000])

    summary = cron_handler.lambda_handler(
        {"chat_ids": ["1", "2", "3"]}, ctx
    )

    assert summary["processed"] == 2
    assert len(runtime.calls) == 2


def test_ample_budget_processes_all_chats(monkeypatch):
    runtime, telegram = _install(monkeypatch)
    ctx = FakeContext(10 * 60 * 1000)

    summary = cron_handler.lambda_handler(
        {"chat_ids": ["1", "2", "3"]}, ctx
    )

    assert summary["processed"] == 3
    assert len(runtime.calls) == 3


def test_context_without_timer_processes_all_chats(monkeypatch):
    runtime, telegram = _install(monkeypatch)

    summary = cron_handler.lambda_handler(
        {"chat_ids": ["1", "2", "3"]}, _NoTimerContext()
    )

    assert summary["processed"] == 3
    assert len(runtime.calls) == 3


# --- Summary correctness for a mixed batch -----------------------------------
def test_mixed_batch_summary_counts(monkeypatch):
    runtime = FakeRuntimeClient(
        replies={
            "deliver": "ok text",       # delivered
            "skip": None,                # non-success envelope -> skipped
            "boom": RuntimeError("x"),  # runtime failure -> skipped
            "faildeliver": "text",      # delivery returns False -> failed
        }
    )
    telegram = FakeTelegram(failures={"faildeliver"})
    _install(monkeypatch, runtime=runtime, telegram=telegram)

    summary = cron_handler.lambda_handler(
        {"task_type": "digest", "chat_ids": ["deliver", "skip", "boom", "faildeliver"]},
        _ctx(),
    )

    assert summary == {
        "task_type": "digest",
        "processed": 4,
        "delivered": 1,
        "failed": 1,
        "skipped": 2,
    }
