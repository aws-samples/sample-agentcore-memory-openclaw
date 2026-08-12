"""Unit tests for the memory-driven cron nudge path (``server.py``).

A scheduled ("cron") fire carries no human message. The runtime retrieves the
user's long-term memory, injects it into the system prompt, and asks the model
to compose a short personalized "what's due" nudge — or nothing when nothing is
due (the ``NOTHING_DUE`` sentinel or an empty response). These tests cover:

* the pure helpers :func:`server.build_cron_nudge_prompt` /
  :func:`server.interpret_cron_response`; and
* :meth:`server.SproutRuntime.handle_invocation` on the cron path with injected
  fakes (memory retrieved -> nudge composed; empty/nothing-due -> no nudge, no
  persistence), asserting no real AWS is touched.
"""

from __future__ import annotations

import os
import sys

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402  (path setup must precede the import)
from server import (  # noqa: E402
    Confidence,
    MemoryContextRecord,
    SproutRuntime,
    build_cron_nudge_prompt,
    interpret_cron_response,
)


# =============================================================================
# Fakes
# =============================================================================
class FakeMemory:
    def __init__(self, records=None):
        self._records = records or []
        self.calls = []
        self.retrieve_args = None

    def retrieve(self, chat_id, query):
        self.calls.append("retrieve")
        self.retrieve_args = (chat_id, query)
        return list(self._records)

    def persist(self, chat_id, session_id, messages, metadata=None):
        self.calls.append("persist")
        return True


class FakeWorkspace:
    def __init__(self):
        self.calls = []

    def download(self, chat_id, dest_dir):
        self.calls.append("download")
        return True

    def upload(self, chat_id, source_dir):
        self.calls.append("upload")
        return True


class FakeAgent:
    def __init__(self, response="Water your basil this morning."):
        self._response = response
        self.calls = []

    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        self.calls.append(
            {"system_prompt": system_prompt, "message": message, "images": images}
        )
        return self._response

    def compose_reminder(self, *, system_prompt, message):
        # Cron fires compose the nudge via a direct one-shot call (no images).
        self.calls.append(
            {"system_prompt": system_prompt, "message": message, "images": []}
        )
        return self._response


def _runtime(memory=None, agent=None, workspace=None):
    return SproutRuntime(
        memory=memory or FakeMemory(),
        workspace=workspace or FakeWorkspace(),
        agent=agent or FakeAgent(),
        base_persona="You are Sprout.",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )


# =============================================================================
# Pure helpers
# =============================================================================
def test_build_cron_nudge_prompt_explicit_reminder_always_delivers():
    # A specific task is an explicit user reminder: it must always be sent, and
    # the model must be told NOT to reply with the NOTHING_DUE sentinel.
    prompt = build_cron_nudge_prompt("feed_tomatoes")
    assert "feed tomatoes" in prompt  # snake_case humanized
    assert "Always write" in prompt
    assert f"do NOT reply {server.NOTHING_DUE_SENTINEL}" in prompt


def test_build_cron_nudge_prompt_sweep_is_conditional():
    # The generic sweep may legitimately produce nothing, so it keeps the
    # NOTHING_DUE escape hatch and does not force a reminder.
    prompt = build_cron_nudge_prompt(server.SWEEP_TASK_TYPE)
    assert server.NOTHING_DUE_SENTINEL in prompt
    assert "Always write" not in prompt


def test_build_cron_nudge_prompt_defaults_blank_task_to_sweep():
    # A blank task falls back to the conditional sweep prompt.
    assert build_cron_nudge_prompt("") == build_cron_nudge_prompt(server.SWEEP_TASK_TYPE)


def test_interpret_cron_response_real_text_passthrough():
    assert interpret_cron_response("  Water the tomatoes.  ") == "Water the tomatoes."


def test_interpret_cron_response_empty_is_dropped():
    assert interpret_cron_response("") == ""
    assert interpret_cron_response("   ") == ""


def test_interpret_cron_response_sentinel_is_dropped():
    assert interpret_cron_response("NOTHING_DUE") == ""
    assert interpret_cron_response("  nothing_due  ") == ""
    assert interpret_cron_response("NOTHING_DUE.") == ""


# =============================================================================
# Cron path through handle_invocation
# =============================================================================
def test_cron_path_composes_nudge_from_memory():
    memory = FakeMemory(
        records=[
            MemoryContextRecord("Grows basil in containers", Confidence.EXPLICIT, topic="plants")
        ]
    )
    agent = FakeAgent(response="Time to water your container basil.")
    runtime = _runtime(memory=memory, agent=agent)

    result = runtime.handle_invocation(
        {"user_id": "12345", "invocation_type": "cron", "task_type": "watering"}
    )

    assert result["ok"] is True
    assert result["text"] == "Time to water your container basil."
    assert result["metadata"]["invocation_type"] == "cron"
    assert result["metadata"]["task_type"] == "watering"
    assert result["metadata"]["nudge_delivered"] is True
    assert result["metadata"]["memory_records_retrieved"] == 1
    # Cron turns are never persisted (no synthetic prompt into memory).
    assert result["metadata"]["transcript_persisted"] is False
    assert "persist" not in memory.calls

    # The memory context was injected into the system prompt, and the agent got
    # the nudge instruction (not a raw user message).
    system_prompt = agent.calls[0]["system_prompt"]
    assert "Grows basil in containers" in system_prompt
    assert server.NOTHING_DUE_SENTINEL in agent.calls[0]["message"]
    # Retrieval query is task-relevant, keyed by chat id.
    assert memory.retrieve_args[0] == "12345"
    assert "watering" in memory.retrieve_args[1]


def test_cron_path_nothing_due_yields_empty_text():
    agent = FakeAgent(response="NOTHING_DUE")
    runtime = _runtime(agent=agent)

    result = runtime.handle_invocation(
        {"user_id": "12345", "invocation_type": "cron", "task_type": "scheduled_check"}
    )

    assert result["ok"] is True
    assert result["text"] == ""
    assert result["metadata"]["nudge_delivered"] is False
    assert result["metadata"]["transcript_persisted"] is False


def test_cron_path_empty_memory_produces_no_nudge():
    # On a generic sweep with no memory the model has nothing to act on and
    # returns the sentinel; the cron path suppresses it so no empty Telegram
    # message is delivered. (An explicit reminder, by contrast, always delivers.)
    memory = FakeMemory(records=[])
    agent = FakeAgent(response="NOTHING_DUE")
    runtime = _runtime(memory=memory, agent=agent)

    result = runtime.handle_invocation(
        {"user_id": "9", "invocation_type": "cron", "task_type": server.SWEEP_TASK_TYPE}
    )

    assert result["text"] == ""
    assert result["metadata"]["memory_records_retrieved"] == 0
    assert result["metadata"]["nudge_delivered"] is False
    # Base persona only when no memory was retrieved.
    assert agent.calls[0]["system_prompt"] == "You are Sprout."


def test_cron_path_ignores_images():
    agent = FakeAgent(response="Prune now.")
    runtime = _runtime(agent=agent)

    runtime.handle_invocation(
        {
            "user_id": "1",
            "invocation_type": "cron",
            "task_type": "pruning",
            "images": [{"data": "Zm9v", "media_type": "image/jpeg"}],
        }
    )

    assert agent.calls[0]["images"] == []


def test_non_cron_path_unaffected_and_persists():
    memory = FakeMemory(
        records=[MemoryContextRecord("Lives in zone 9b", Confidence.EXPLICIT, topic="zone")]
    )
    agent = FakeAgent(response="Here is your advice.")
    runtime = _runtime(memory=memory, agent=agent)

    result = runtime.handle_invocation(
        {"user_id": "12345", "message": "How do I care for my basil?"}
    )

    assert result["ok"] is True
    assert result["text"] == "Here is your advice."
    assert "invocation_type" not in result["metadata"]
    assert result["metadata"]["transcript_persisted"] is True
    assert "persist" in memory.calls
    # A normal turn forwards the user's actual message (a current-time context
    # hint may be appended so the model can schedule reminders correctly).
    assert agent.calls[0]["message"].startswith("How do I care for my basil?")
