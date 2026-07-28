"""AgentCore Runtime HTTP server for the Sprout gardening assistant.

Amazon Bedrock AgentCore Runtime hosts a container that must implement the
runtime service contract: listen on port 8080 and expose ``GET /ping`` (health)
and ``POST /invocations`` (the agent entry point). This module is that shell — a
thin standard-library ``http.server`` wrapper (no web framework, so the image
stays small) around the OpenClaw agent, augmented with AgentCore Memory hooks
and Amazon Bedrock prompt caching.

Endpoints
---------
* ``GET /ping`` -> ``200 {"status": "Healthy"}`` — readiness/liveness probe that
  returns well within the 5-second budget (Req 3.7).
* ``POST /invocations`` -> the JSON request body is the invocation payload
  forwarded by the Webhook_Lambda / Cron_Lambda; the response is a JSON envelope
  (``{"ok": ...}``).

Invocation lifecycle (within each ``POST /invocations``)
--------------------------------------------------------
1. Parse the payload (``message``, ``user_id``, ``session_id``,
   ``invocation_type``, ``images``, ``attachments``) and validate ``user_id``
   (Req 3.2, reject with ``INVALID_USER_ID`` when missing).
2. Download the user's workspace from S3 (graceful degradation on failure,
   Req 9.2, 9.4).
3. Retrieve relevant long-term memories via ``RetrieveMemoryRecords`` scoped to
   ``sprout/{chat_id}/long_term`` with a 3-second budget (Req 5.1, 5.5).
4. Assemble the retrieved records into context — Explicit before Inferred,
   Explicit wins conflicts, capped at 50 (Req 5.2, 5.3, 5.4).
5. Inject the assembled context into the OpenClaw system prompt and place a
   Bedrock ``cachePoint`` after it so the static prefix is cached (Req 8.x,
   prompt caching).
6. Process the message (and any images, Req 8.2) through the OpenClaw agent.
7. Persist the session transcript via ``CreateEvent`` so the configured
   extraction strategies derive long-term records server-side (Req 4.4, 4.5,
   4.6). Failure is logged but never blocks the response.
8. Upload the workspace back to S3 if modified (graceful degradation, Req 9.3,
   9.5).

Testability
-----------
The deterministic logic — namespace derivation (:func:`derive_long_term_namespace`,
:func:`derive_episodic_namespace`) and memory-context assembly
(:func:`assemble_memory_context`) — is implemented as pure, module-level
functions with no I/O so they can be unit/property tested in isolation
(tasks 2.3, 2.4). All heavy/optional dependencies (``boto3``, ``requests``) are
imported lazily inside the collaborators that need them, so this module imports
cleanly in a test environment that has only the standard library.

The agent itself is the REAL OpenClaw runtime: the official OpenClaw Node app is
started as a subprocess (``openclaw gateway run``) exposing an OpenAI-compatible
``/v1/chat/completions`` endpoint on loopback, and :class:`OpenClawAgent`
forwards each turn to it over HTTP. AgentCore Memory remains the memory system.

Environment variables (UPPER_SNAKE_CASE per project standards)
--------------------------------------------------------------
* ``MODEL_ID`` — Bedrock model / cross-region inference profile id.
* ``MEMORY_ID`` — AgentCore Memory resource identifier.
* ``WORKSPACE_BUCKET`` — S3 bucket for OpenClaw workspace persistence.
* ``ENABLE_PROMPT_CACHE`` — enable Bedrock prompt caching (default ``true``).
* ``LOG_LEVEL`` — logging level (default ``INFO``).
* ``PORT`` — listen port (default ``8080``; AgentCore Runtime expects 8080).
"""

from __future__ import annotations

import base64
import re as _re
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

logger = logging.getLogger("sprout.agentcore.server")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

# --- HTTP contract constants --------------------------------------------------
PING_PATH = "/ping"
INVOCATIONS_PATH = "/invocations"
DEFAULT_PORT = 8080

# --- Memory namespace convention (Req 6.1) ------------------------------------
NAMESPACE_ROOT = "sprout"
LONG_TERM_SEGMENT = "long_term"
EPISODIC_SEGMENT = "episodic"

# --- Memory retrieval / assembly bounds ---------------------------------------
MAX_MEMORIES_IN_CONTEXT = 50  # Req 5.4
MEMORY_RETRIEVE_TIMEOUT_SECONDS = 3.0  # Req 5.5
DEFAULT_PROMPT_CACHE = True  # Req 8.x default for ENABLE_PROMPT_CACHE

# --- Environment variable names -----------------------------------------------
MODEL_ID_ENV = "MODEL_ID"
MEMORY_ID_ENV = "MEMORY_ID"
WORKSPACE_BUCKET_ENV = "WORKSPACE_BUCKET"
ENABLE_PROMPT_CACHE_ENV = "ENABLE_PROMPT_CACHE"
# The vision model is separate from the text model because image identification
# benefits from a more capable model (Sonnet) while text chat can use the faster,
# cheaper model (Haiku). Both use cross-region inference profiles.
VISION_MODEL_ID_ENV = "VISION_MODEL_ID"
DEFAULT_VISION_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# --- Proactive scheduling (EventBridge Scheduler) env vars --------------------
# The runtime self-schedules memory-driven reminders by calling the EventBridge
# Scheduler control plane directly (scheduler:CreateSchedule). CloudFormation
# injects the dedicated schedule group, the invoker (Cron) Lambda ARN the
# schedule targets, and the role EventBridge Scheduler assumes to invoke it.
CRON_SCHEDULER_ROLE_ARN_ENV = "CRON_SCHEDULER_ROLE_ARN"
CRON_INVOKER_FUNCTION_ARN_ENV = "CRON_INVOKER_FUNCTION_ARN"
CRON_SCHEDULER_GROUP_ENV = "CRON_SCHEDULER_GROUP"

# --- Album (multi-photo) grouping --------------------------------------------
# Telegram delivers an "album" (several photos sent together) as N separate
# webhook updates that share a media_group_id — there is no single multi-photo
# message. To answer once and save the album as one backyard section, each item
# is buffered in the S3 workspace bucket under an album prefix; after a short
# debounce a single invocation atomically "claims" the group (S3 conditional
# PutObject) and processes all photos in one turn. The others return an empty
# reply so Telegram receives nothing from them.
MEDIA_GROUP_ID_KEY = "media_group_id"
MESSAGE_ID_KEY = "message_id"
ALBUM_S3_PREFIX = "albums"
# Debounce window (seconds) to wait for the rest of an album to arrive before
# claiming/processing it. Telegram sends album items within ~1s; 2s is safe.
ALBUM_DEBOUNCE_SECONDS_ENV = "ALBUM_DEBOUNCE_SECONDS"
DEFAULT_ALBUM_DEBOUNCE_SECONDS = 2.0
# Cap on the number of images forwarded to the model for one album (Claude
# accepts up to 20 images; a Telegram album is at most 10).
MAX_ALBUM_IMAGES = 15

# The invocation_type value the Cron (invoker) Lambda sends for scheduled fires.
CRON_INVOCATION_TYPE = "cron"
# task_type for a generic proactive sweep (no specific user-requested task). A
# sweep fire is conditional (may yield NOTHING_DUE); any other task_type is an
# explicit reminder that must always deliver. Matches the Cron Lambda's
# DEFAULT_TASK_TYPE fallback.
SWEEP_TASK_TYPE = "scheduled_check"
# Sentinel the model emits (and the cron path strips to "") when a scheduled
# check finds nothing actually due, so no empty Telegram nudge is delivered.
NOTHING_DUE_SENTINEL = "NOTHING_DUE"
# Max length for an EventBridge Scheduler schedule name ([0-9a-zA-Z-_.]{1,64}).
MAX_SCHEDULE_NAME_LENGTH = 64

# The OpenClaw gateway config reads BEDROCK_MODEL_ID / AWS_REGION for the Bedrock
# provider and OPENCLAW_AUTH_TOKEN for the loopback gateway auth (Req 8.x, real
# OpenClaw runtime).
BEDROCK_MODEL_ID_ENV = "BEDROCK_MODEL_ID"
AWS_REGION_ENV = "AWS_REGION"
OPENCLAW_AUTH_TOKEN_ENV = "OPENCLAW_AUTH_TOKEN"  # nosec B105 - env var name, not a secret

# --- OpenClaw gateway subprocess (real OpenClaw Node runtime) -----------------
# The real OpenClaw runtime runs as a Node subprocess exposing an
# OpenAI-compatible ``/v1/chat/completions`` endpoint and a ``/health`` probe on
# loopback, authenticated with a bearer token. server.py starts it before the
# AgentCore HTTP server and forwards each turn to it.
OPENCLAW_PORT = 18789
OPENCLAW_URL = f"http://localhost:{OPENCLAW_PORT}"
# The Node gateway can take longer than a naive boot wait to become ready on a
# cold AgentCore microVM, so allow generous headroom (still under the webhook's
# 55s invoke budget) when (re)ensuring readiness before forwarding a turn.
OPENCLAW_STARTUP_TIMEOUT_SECONDS = 50
OPENCLAW_REQUEST_TIMEOUT_SECONDS = 300

# The running gateway subprocess handle and a lock guarding (re)starts. AgentCore
# Runtime freezes/thaws the container between invocations and cold-starts often
# under low traffic, so the gateway may be absent when a turn arrives; these let
# the invocation path lazily (re)start it. Guarded by the GIL + lock for the
# ThreadingHTTPServer's concurrent handlers.
_openclaw_proc: Any = None
_openclaw_start_lock = threading.Lock()
# The OpenClaw config lives in this module's directory in the image; at startup
# it is env-substituted and written to the gateway's expected config path.
OPENCLAW_CONFIG_SRC = "openclaw.json"
OPENCLAW_CONFIG_DIR = "/root/.openclaw"
OPENCLAW_CONFIG_DST = os.path.join(OPENCLAW_CONFIG_DIR, "openclaw.json")

# Directory holding the OpenClaw config + persona startup files (this module's
# own directory inside the container image).
CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))


# =============================================================================
# Pure logic: namespace derivation (Req 6.1, 6.2) — testable in isolation
# =============================================================================
def derive_long_term_namespace(chat_id: str) -> str:
    """Derive the long-term memory namespace for a Telegram chat id (Req 6.1).

    Each unique ``chat_id`` maps to exactly one namespace and no two distinct
    chat ids share a namespace, because the chat id is embedded verbatim as the
    sole variable path segment (Req 6.2).

    Args:
        chat_id: The Telegram chat id used as the AgentCore ``actorId``.

    Returns:
        The namespace ``sprout/{chat_id}/long_term``.
    """
    return f"{NAMESPACE_ROOT}/{chat_id}/{LONG_TERM_SEGMENT}"


def derive_episodic_namespace(chat_id: str, session_id: str) -> str:
    """Derive the episodic memory namespace for a chat id + session (Req 6.1).

    Args:
        chat_id: The Telegram chat id used as the AgentCore ``actorId``.
        session_id: The per-invocation session identifier.

    Returns:
        The namespace ``sprout/{chat_id}/episodic/{session_id}``.
    """
    return f"{NAMESPACE_ROOT}/{chat_id}/{EPISODIC_SEGMENT}/{session_id}"


def chat_id_from_namespace(namespace: str) -> str:
    """Extract the owning chat id from a Sprout namespace.

    Inverse of :func:`derive_long_term_namespace` / :func:`derive_episodic_namespace`
    used to enforce namespace isolation (Req 6.4).

    Args:
        namespace: A ``sprout/{chat_id}/...`` namespace string.

    Returns:
        The chat id (2nd path segment), or an empty string when the namespace
        does not follow the convention.
    """
    parts = (namespace or "").split("/")
    if len(parts) >= 3 and parts[0] == NAMESPACE_ROOT:
        return parts[1]
    return ""


# =============================================================================
# Path-injection defenses (chat id validation + directory containment)
# =============================================================================
# The Telegram ``chat_id`` (and, on download, S3 object keys) are attacker-
# influenced values that end up in filesystem paths for the per-user workspace.
# Without validation a value like ``../../etc`` could traverse outside the
# workspace root (CWE-22 / CodeQL py/path-injection). Two barriers guard this:
#   1. ``is_valid_chat_id`` — an allowlist so an id used as a path segment / S3
#      prefix cannot contain separators or ``..``. Telegram chat ids are signed
#      integers (negative for groups), so the safe charset is ``^-?[0-9]+$``.
#   2. ``resolve_within`` — normalizes a joined path and verifies it stays inside
#      the intended base directory, so a crafted S3 key can't escape on download.
_SAFE_CHAT_ID_RE = _re.compile(r"^-?[0-9]+$")


def is_valid_chat_id(chat_id: str) -> bool:
    """Return ``True`` when ``chat_id`` is a safe Telegram chat identifier.

    Telegram chat ids are integers (negative for group/channel chats). Enforcing
    this allowlist means the id is safe to embed in a filesystem path segment,
    an S3 key prefix, and a memory namespace — it cannot contain ``/``, ``\\``,
    ``..``, NUL, or other traversal characters (CWE-22 defense).

    Args:
        chat_id: The candidate chat id (already stripped).

    Returns:
        ``True`` when the id matches ``^-?[0-9]+$``.
    """
    return bool(_SAFE_CHAT_ID_RE.match(chat_id or ""))


def resolve_within(base_dir: str, *segments: str) -> str:
    """Join ``segments`` under ``base_dir`` and confirm the result stays inside.

    Defends against path traversal (CWE-22) when an attacker-influenced value
    (a chat id, or an S3 object key on download) is used to build a local path:
    the joined path is normalized with :func:`os.path.realpath` and verified to
    be the base directory itself or a descendant of it.

    Args:
        base_dir: The directory the result must remain within.
        *segments: Path segments to join under ``base_dir``.

    Returns:
        The absolute, normalized path.

    Raises:
        ValueError: When the joined path escapes ``base_dir``.
    """
    base = os.path.realpath(base_dir)
    candidate = os.path.realpath(os.path.join(base, *segments))
    if candidate != base and not candidate.startswith(base + os.sep):
        raise ValueError("resolved path escapes the base directory")
    return candidate


# =============================================================================
# Pure logic: proactive-schedule construction — testable in isolation
# =============================================================================
def _slugify_schedule_token(value: str) -> str:
    """Reduce a string to the EventBridge Scheduler name charset.

    Schedule (and group) names must match ``[0-9a-zA-Z-_.]+``. Any character
    outside that set is collapsed to ``-`` so an arbitrary chat id / task label
    yields a valid, stable token.

    Args:
        value: The raw token (chat id or task label).

    Returns:
        The sanitized token (may be empty when ``value`` had no valid chars).
    """
    import re

    return re.sub(r"[^0-9a-zA-Z._-]+", "-", (value or "").strip()).strip("-")


def derive_schedule_name(chat_id: str, task: str) -> str:
    """Derive a deterministic, valid schedule name unique per chat + task.

    The name is deterministic (no random component) so re-requesting the same
    reminder for the same chat targets the same schedule — a subsequent
    ``UpdateSchedule`` (or an idempotent ``CreateSchedule``) rather than a
    duplicate. Distinct ``(chat_id, task)`` pairs never collide because both are
    embedded, and the result is truncated to the 64-char Scheduler limit.

    Args:
        chat_id: The Telegram chat id the reminder belongs to.
        task: The task label / type carried in the reminder payload.

    Returns:
        A schedule name matching ``[0-9a-zA-Z-_.]{1,64}``.
    """
    chat_token = _slugify_schedule_token(chat_id) or "chat"
    task_token = _slugify_schedule_token(task) or "reminder"
    name = f"sprout-{chat_token}-{task_token}"
    return name[:MAX_SCHEDULE_NAME_LENGTH]


def build_create_schedule_kwargs(
    *,
    chat_id: str,
    schedule_expression: str,
    task: str,
    group_name: str,
    invoker_arn: str,
    role_arn: str,
    description: Optional[str] = None,
) -> dict[str, Any]:
    """Build the boto3 ``scheduler.create_schedule`` kwargs for a reminder.

    Pure/deterministic: given the same inputs it always produces the same
    request dict (no I/O, no randomness), so it is unit-testable in isolation.
    The schedule targets the Cron (invoker) Lambda; EventBridge Scheduler assumes
    ``role_arn`` to invoke it, and the ``Input`` carries the payload the invoker
    forwards to the runtime (``{"chat_ids": [chat_id], "task_type": task}``),
    matching the Cron Lambda's event contract.

    Args:
        chat_id: The Telegram chat id to remind.
        schedule_expression: An EventBridge Scheduler expression, e.g.
            ``at(2025-01-01T09:00:00)``, ``rate(1 days)``, or ``cron(0 9 * * ? *)``.
        task: The task label / type (becomes ``task_type`` in the payload).
        group_name: The schedule group that holds all reminder schedules.
        invoker_arn: The Cron (invoker) Lambda ARN the schedule targets.
        role_arn: The role EventBridge Scheduler assumes to invoke the target.
        description: Optional human-readable description.

    Returns:
        The kwargs dict to splat into ``scheduler.create_schedule(**kwargs)``.
    """
    payload = {"chat_ids": [chat_id], "task_type": task}
    kwargs: dict[str, Any] = {
        "Name": derive_schedule_name(chat_id, task),
        "GroupName": group_name,
        "ScheduleExpression": schedule_expression,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        # Self-clean after the schedule completes so fired one-off ``at(...)``
        # reminders don't accumulate (they never auto-delete otherwise) and the
        # group stays within Scheduler quotas. Recurring ``rate(...)``/``cron(...)``
        # schedules only "complete" when they have no future occurrence, so this
        # is safe for them too.
        "ActionAfterCompletion": "DELETE",
        "Target": {
            "Arn": invoker_arn,
            "RoleArn": role_arn,
            "Input": json.dumps(payload),
        },
    }
    if description:
        kwargs["Description"] = description
    return kwargs


# =============================================================================
# Pure logic: cron nudge composition — testable in isolation
# =============================================================================
def build_cron_nudge_prompt(task_type: str) -> str:
    """Build the user-turn instruction that asks the model for a reminder.

    A scheduled fire has no human message. There are two kinds of fire:

    * **Explicit reminder** — the gardener earlier asked to be reminded about a
      specific task (``watering``, ``check_basil``, ``feed_tomatoes``, ...). The
      intent lives in the schedule's ``task_type``, *not* necessarily in
      long-term memory, so the model MUST always send the reminder for that task
      (personalized with whatever it remembers). It must not skip it just
      because memory has nothing "due" — the reminder itself is the due task.
    * **Generic sweep** — a periodic check with no specific task
      (:data:`SWEEP_TASK_TYPE`). Here the model decides whether anything is due
      from memory and replies :data:`NOTHING_DUE_SENTINEL` when nothing is, so
      the cron path suppresses an empty nudge.

    Args:
        task_type: The scheduled task label (e.g. ``watering`` or
            ``scheduled_check``).

    Returns:
        The instruction string used as the user message for the cron turn.
    """
    label = (task_type or SWEEP_TASK_TYPE).strip() or SWEEP_TASK_TYPE

    # Generic proactive sweep: conditional on memory, may produce nothing.
    if label == SWEEP_TASK_TYPE:
        return (
            "This is an automated proactive check — the gardener did not just "
            "message you. Using only what you remember about this gardener "
            "(above), decide whether anything is due right now (watering, "
            "feeding, pruning, harvesting, frost protection). If something is "
            "due, write one short, warm Telegram reminder (no preamble, under "
            "400 characters). If nothing is actually due, or you have no "
            f"relevant memory to act on, reply with exactly {NOTHING_DUE_SENTINEL} "
            "and nothing else."
        )

    # Explicit user-requested reminder: ALWAYS deliver a nudge for this task.
    human_label = label.replace("_", " ").strip()
    return (
        f"This is a reminder you promised the gardener about: \"{human_label}\". "
        "It is now time to send it — the gardener did not just message you. "
        "Write one short, warm Telegram reminder (no preamble, under 400 "
        "characters) nudging them about this task, personalized with whatever "
        "you remember about them and their plants (above). Always write the "
        f"reminder — do NOT reply {NOTHING_DUE_SENTINEL} and do not skip it, "
        "even if you have no stored context about this specific task."
    )


def build_album_prompt(section_caption: str, num_photos: int) -> str:
    """Build the single grouped-turn instruction for a photo album (pure).

    When the gardener sends several photos of a backyard area together, they
    arrive as one album. This composes one instruction covering all of them so
    the model replies once and saves them as a single section — using the album
    caption as the section name when present, or asking for one when absent.

    Args:
        section_caption: The album caption (first non-empty), or ``""``.
        num_photos: How many photos are in the album.

    Returns:
        The user-message string for the single grouped album turn.
    """
    caption = (section_caption or "").strip()
    count = max(int(num_photos or 0), 1)
    if caption:
        return (
            f"{caption}\n\n[The gardener sent {count} photos together as one album "
            "of a single backyard area. Identify the plants and beddings across ALL "
            "the photos and remember them together as one section named from the "
            "caption above. Reply once with a single grouped summary of the whole "
            "section — do not describe the photos one by one.]"
        )
    return (
        f"[The gardener sent {count} photos together as one album of a single "
        "backyard area, with no caption. Identify the plants and beddings across "
        "ALL the photos, reply once with a single grouped summary, and ask the "
        "gardener what to name this section/region so you can save it together.]"
    )


def interpret_cron_response(text: str) -> str:
    """Normalize a model cron response into a deliverable nudge or ``""``.

    Returns an empty string when the model produced nothing to send — either an
    empty/whitespace response or the :data:`NOTHING_DUE_SENTINEL` (compared
    case-insensitively after stripping surrounding whitespace/punctuation). Any
    other text is returned stripped, ready for delivery.

    Args:
        text: The raw model response for the cron turn.

    Returns:
        The nudge text to deliver, or ``""`` when nothing should be sent.
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    if stripped.strip(".!\"' ").upper() == NOTHING_DUE_SENTINEL:
        return ""
    return stripped


# --- Schedule directive bridge (agent output -> create_schedule) --------------
# When the agent and user agree on a reminder, the persona instructs the model
# to embed a directive of the form:
#     [[SCHEDULE expr="cron(0 9 ? * SUN *)" task="watering"]]
# server.py parses these out of the agent's reply, creates the corresponding
# EventBridge Scheduler reminders, and strips the directive(s) from the text
# before it is shown to the user. Only at()/rate()/cron() expressions are
# accepted so a stray/hallucinated tag can't produce a bogus schedule call.
# (``re`` is aliased as ``_re`` in the top-level imports.)
_SCHEDULE_TAG_RE = _re.compile(r"\[\[SCHEDULE\b[^\]]*?\]\]", _re.IGNORECASE | _re.DOTALL)
_SCHEDULE_EXPR_RE = _re.compile(r'expr\s*=\s*"([^"]*)"', _re.IGNORECASE)
_SCHEDULE_TASK_RE = _re.compile(r'task\s*=\s*"([^"]*)"', _re.IGNORECASE)
_SCHEDULE_EXPR_PREFIXES = ("at(", "rate(", "cron(")


def is_valid_schedule_expression(expr: str) -> bool:
    """Return True when ``expr`` looks like an EventBridge Scheduler expression.

    Accepts only ``at(...)``, ``rate(...)``, or ``cron(...)`` forms (the three
    ScheduleExpression syntaxes), guarding against a hallucinated/garbage tag
    producing a bogus ``create_schedule`` call. Final validity is still enforced
    by the Scheduler API (a bad expression makes create_schedule degrade to
    False), this is just a cheap pre-filter.

    Args:
        expr: The candidate schedule expression.

    Returns:
        ``True`` when the trimmed, lowercased value starts with a known prefix
        and is closed with ``)``.
    """
    value = (expr or "").strip()
    lowered = value.lower()
    return value.endswith(")") and lowered.startswith(_SCHEDULE_EXPR_PREFIXES)


def parse_schedule_directives(text: str) -> tuple[str, list[dict[str, str]]]:
    """Extract ``[[SCHEDULE ...]]`` directives and strip them from the reply.

    Pure/deterministic: no I/O. Returns the user-facing text with every
    directive tag removed (and surrounding blank lines tidied) plus the list of
    valid directives found, each ``{"expr", "task"}``. A tag whose ``expr`` is
    missing or not a recognized schedule expression (see
    :func:`is_valid_schedule_expression`) is still stripped from the text but not
    returned as a directive, so the user never sees the raw tag and no bogus
    schedule is created. ``task`` defaults to ``"reminder"`` when absent.

    Args:
        text: The raw agent reply that may contain schedule directives.

    Returns:
        A ``(cleaned_text, directives)`` tuple.
    """
    if not text:
        return "", []

    directives: list[dict[str, str]] = []
    for tag in _SCHEDULE_TAG_RE.findall(text):
        expr_match = _SCHEDULE_EXPR_RE.search(tag)
        if not expr_match:
            continue
        expr = expr_match.group(1).strip()
        if not is_valid_schedule_expression(expr):
            continue
        task_match = _SCHEDULE_TASK_RE.search(tag)
        task = (task_match.group(1).strip() if task_match else "") or "reminder"
        directives.append({"expr": expr, "task": task})

    cleaned = _SCHEDULE_TAG_RE.sub("", text)
    # Tidy whitespace left by removed tags (trailing spaces, tripled newlines).
    cleaned = _re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, directives


# --- Deterministic schedule extraction (second pass) --------------------------
# Relying on the chat model to embed a ``[[SCHEDULE ...]]`` directive in its
# free-form reply proved unreliable (a small/fast model such as Haiku confidently
# says "reminder set" without emitting the tag, and its built-in cron tool writes
# only to the ephemeral in-container crontab that never reaches EventBridge). So
# instead of trusting the conversational turn, the runtime runs a focused,
# JSON-only *second pass* over the turn: it feeds the user's message and the
# assistant's own reply (which already states the agreed absolute time) to the
# model and asks solely for a structured list of schedules. Extraction of an
# explicitly stated time into an EventBridge expression is a narrow, constrained
# task the model does reliably — unlike volunteering magic markup mid-chat. The
# pass is gated on :func:`has_scheduling_intent` so ordinary chat never triggers
# an extra model call.

# Keyword gate: cheap pre-filter so the second extraction pass only runs when the
# turn plausibly concerns a reminder/schedule (keeps normal chat at one model
# call). Matched case-insensitively as substrings/word-fragments.
_SCHEDULING_INTENT_KEYWORDS = (
    "remind",
    "reminder",
    "schedule",
    "every day",
    "every morning",
    "every week",
    "each day",
    "each week",
    "daily",
    "weekly",
    "nightly",
    "recurring",
    "wake me",
    "nudge",
    "notify",
    "alert me",
    "ping me",
    "in a few minutes",
    "minutes from now",
    "hours from now",
    "tomorrow",
    "next week",
    "at 9",
    "o'clock",
)


def has_scheduling_intent(text: str) -> bool:
    """Return ``True`` when ``text`` plausibly concerns a reminder/schedule.

    A cheap, case-insensitive keyword pre-filter used to decide whether to run
    the (model-backed) schedule-extraction second pass, so ordinary gardening
    chat does not incur an extra model call. False positives are harmless — the
    extraction pass simply returns no schedules — so the gate errs toward
    recall.

    Args:
        text: Any turn text (the user message or the assistant reply).

    Returns:
        ``True`` when any scheduling keyword is present.
    """
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in _SCHEDULING_INTENT_KEYWORDS)


# The extraction pass is asked to return strictly this shape and nothing else.
SCHEDULE_EXTRACTION_SYSTEM_PROMPT = (
    "You are a precise scheduling extractor. You are given a gardener's message "
    "and the assistant's reply to it. Your ONLY job is to output the reminders "
    "the assistant actually agreed to set, as strict JSON — no prose, no "
    "markdown, no code fences. Output exactly one JSON object of the form "
    '{"schedules": [{"expr": "<expression>", "task": "<short_label>"}]}. '
    "Rules: (1) Include a reminder ONLY when the assistant clearly confirmed it "
    "would remind/nudge the gardener; if it is still asking a question or has "
    "not committed, output {\"schedules\": []}. "
    "(2) expr MUST be a valid Amazon EventBridge Scheduler expression, all times "
    "in UTC, computed from the provided current UTC time. "
    "(3) If the reminder RECURS (the gardener said 'every', 'daily', 'each "
    "morning', 'weekly', etc.) you MUST use a recurring expression — "
    "cron(minutes hours day-of-month month day-of-week year) or "
    "rate(<n> <minutes|hours|days>) — NEVER a one-off at(). Convert the stated "
    "local time to UTC (e.g. 8am US/Eastern in summer = 12:00 UTC -> "
    "cron(0 12 * * ? *)). "
    "(4) Use one-off at(YYYY-MM-DDTHH:MM:SS) ONLY for a single non-recurring "
    "reminder, and the timestamp MUST be strictly in the FUTURE relative to the "
    "current UTC time; if the stated clock time already passed today, roll it to "
    "the next day. Prefer the exact absolute time the assistant already stated. "
    "(5) task is a short snake_case label (e.g. watering, frost_check, "
    "feed_tomatoes). (6) Output ONLY the JSON object."
)


def _at_expression_is_future(expr: str, now: datetime) -> bool:
    """Return ``True`` when a one-off ``at(...)`` timestamp is in the future.

    Non-``at`` expressions (``rate``/``cron``) always return ``True`` — they are
    recurring and have no single past/future instant to validate here. An
    ``at(...)`` whose timestamp cannot be parsed is treated as invalid
    (``False``) so a malformed one-off is dropped rather than created.

    Args:
        expr: A schedule expression already shape-validated by
            :func:`is_valid_schedule_expression`.
        now: The current time (timezone-aware, UTC) to compare against.

    Returns:
        ``True`` to keep the expression, ``False`` to drop it.
    """
    value = (expr or "").strip()
    if not value.lower().startswith("at("):
        return True
    inner = value[value.find("(") + 1 : value.rfind(")")].strip()
    # EventBridge Scheduler at() timestamps are naive UTC: YYYY-MM-DDTHH:MM:SS.
    try:
        when = datetime.strptime(inner, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return when > now


def build_schedule_extraction_user_prompt(
    *, user_message: str, assistant_reply: str, now_iso: str
) -> str:
    """Build the user turn for the schedule-extraction second pass (pure).

    Args:
        user_message: The gardener's original message.
        assistant_reply: The assistant's reply to that message (which typically
            already states the agreed absolute reminder time).
        now_iso: The current time as an ISO-8601 UTC string, used as "now" when
            turning a relative time ("in 5 minutes") into an absolute ``at(...)``.

    Returns:
        The user-message string for the extraction call.
    """
    return (
        f"Current UTC time: {now_iso}\n\n"
        f"Gardener message:\n{user_message}\n\n"
        f"Assistant reply:\n{assistant_reply}\n\n"
        "Extract the reminders the assistant agreed to set as JSON now."
    )


def parse_extracted_schedules(
    raw: str, *, now: Optional[datetime] = None
) -> list[dict[str, str]]:
    """Parse the extraction pass's JSON output into validated directives (pure).

    Tolerant of a model that wraps the JSON in prose or code fences: the first
    ``{...}`` object is located and parsed. Each schedule is validated with
    :func:`is_valid_schedule_expression`; invalid or malformed entries are
    dropped. When ``now`` is provided, a one-off ``at(...)`` whose timestamp is
    not strictly in the future is also dropped (see
    :func:`_at_expression_is_future`) so a stale/past reminder is never created
    (it would otherwise be immediately completed-and-deleted, a phantom).
    Never raises — any parse failure yields an empty list.

    Args:
        raw: The raw text returned by the extraction model call.
        now: Optional current time (UTC) used to reject past one-off reminders.

    Returns:
        A list of ``{"expr", "task"}`` dicts (possibly empty).
    """
    text = (raw or "").strip()
    if not text:
        return []
    # Locate the outermost JSON object even if surrounded by prose/code fences.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    raw_schedules = data.get("schedules") if isinstance(data, dict) else None
    if not isinstance(raw_schedules, list):
        return []
    directives: list[dict[str, str]] = []
    for entry in raw_schedules:
        if not isinstance(entry, dict):
            continue
        expr = str(entry.get("expr", "")).strip()
        if not is_valid_schedule_expression(expr):
            continue
        if now is not None and not _at_expression_is_future(expr, now):
            logger.warning("Dropping past/invalid one-off schedule expr: %s", expr)
            continue
        task = str(entry.get("task", "")).strip() or "reminder"
        directives.append({"expr": expr, "task": task})
    return directives


# =============================================================================
# Pure logic: memory-context assembly (Req 5.2, 5.3, 5.4) — testable
# =============================================================================
class Confidence(str, Enum):
    """Whether a retrieved memory was stated explicitly or inferred (Req 5.2)."""

    EXPLICIT = "EXPLICIT"
    INFERRED = "INFERRED"


# =============================================================================
# Structured long-term records (garden sections) — pure builders + filtering
# =============================================================================
# Two complementary write paths into AgentCore Memory:
#   * CreateEvent (SproutMemory.persist) — the conversational transcript, from
#     which the configured strategies asynchronously EXTRACT long-term records.
#   * BatchCreateMemoryRecords (SproutMemory.write_records) — a record the app
#     writes DELIBERATELY, with queryable metadata attached. Used when the
#     gardener registers a backyard section (e.g. a photo album of one bed) so
#     "what's in the north bed" is a durable structured fact rather than
#     something extraction may or may not derive from chat text.
#
# IMPORTANT (verified against the API): custom metadata keys are stored on the
# record and returned on retrieval, but RetrieveMemoryRecords' server-side
# ``metadataFilters`` only accepts a small set of reserved
# ``x-amz-agentcore-memory-*`` keys — a custom key such as ``section`` is
# rejected with "not a valid filter key". So retrieval stays semantic (+ the
# per-user namespace) and custom metadata is filtered CLIENT-SIDE via
# :func:`filter_records_by_metadata`.
SECTION_RECORD_TYPE = "section"
# Metadata keys written on a section record (custom, client-side filterable).
META_TYPE = "type"
META_SECTION = "section"
META_PLANTS = "plants"
META_PHOTO_COUNT = "photo_count"
# Reserved prefix AgentCore adds to its own metadata keys on stored records.
RESERVED_METADATA_PREFIX = "x-amz-agentcore-memory-"


def derive_section_slug(name: str) -> str:
    """Normalize a free-text section name into a stable metadata token.

    Lowercases, collapses any run of non-alphanumeric characters to ``_``, and
    trims leading/trailing separators so "North Bed!" and "north  bed" both
    yield ``north_bed`` — making the same physical bed match across turns.

    Args:
        name: The section name as the gardener wrote it (album caption).

    Returns:
        The slug, or ``""`` when ``name`` has no usable characters.
    """
    return _re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")


def build_metadata_map(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert plain Python values into the AgentCore metadata union (pure).

    Maps each value to the matching ``metadataValue`` member: ``str`` ->
    ``stringValue``, ``bool``/``int``/``float`` -> ``numberValue``, list/tuple of
    strings -> ``stringListValue``, ``datetime`` -> ``dateTimeValue``. ``None``
    values and empty lists are omitted so no empty metadata is written.

    Args:
        values: Plain-value metadata to attach to a record.

    Returns:
        The metadata map accepted by ``BatchCreateMemoryRecords``.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, value in (values or {}).items():
        if value is None:
            continue
        if isinstance(value, str):
            if value:
                out[key] = {"stringValue": value}
        elif isinstance(value, bool):
            out[key] = {"numberValue": float(value)}
        elif isinstance(value, (int, float)):
            out[key] = {"numberValue": float(value)}
        elif isinstance(value, datetime):
            out[key] = {"dateTimeValue": value}
        elif isinstance(value, (list, tuple)):
            items = [str(v) for v in value if v is not None and str(v)]
            if items:
                out[key] = {"stringListValue": items}
    return out


def flatten_metadata(metadata: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Flatten a returned AgentCore metadata map into plain Python values (pure).

    Inverse of :func:`build_metadata_map` for reading records back: unwraps each
    ``{"stringValue"|"numberValue"|"stringListValue"|"dateTimeValue": v}`` union
    into the bare value. Reserved ``x-amz-agentcore-memory-*`` keys are dropped
    so callers see only their own metadata. Tolerates already-flat values.

    Args:
        metadata: The ``metadata`` map from a retrieved record, or ``None``.

    Returns:
        A flat ``{key: value}`` dict (empty when there is no usable metadata).
    """
    flat: dict[str, Any] = {}
    for key, wrapped in (metadata or {}).items():
        if key.startswith(RESERVED_METADATA_PREFIX):
            continue
        if isinstance(wrapped, dict):
            for member in ("stringValue", "numberValue", "stringListValue", "dateTimeValue"):
                if member in wrapped:
                    flat[key] = wrapped[member]
                    break
        else:
            flat[key] = wrapped
    return flat


def build_section_record(
    *,
    chat_id: str,
    section_name: str,
    summary: str,
    plants: Optional[list[str]] = None,
    photo_count: int = 0,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build one ``BatchCreateMemoryRecords`` record for a backyard section (pure).

    Deterministic apart from ``now`` (injectable), so the request body is
    unit-testable without AWS. The ``requestIdentifier`` is derived from the chat
    id + section slug so re-registering the same bed targets a stable identifier
    rather than accumulating unrelated ids.

    Args:
        chat_id: The Telegram chat id (owner; selects the namespace).
        section_name: The section name as written by the gardener.
        summary: The human-readable text stored as the record content.
        plants: Optional plant names identified in the section.
        photo_count: How many photos the section was registered from.
        now: Record timestamp; defaults to the current UTC time.

    Returns:
        A record dict for the ``records`` list of ``BatchCreateMemoryRecords``.
    """
    slug = derive_section_slug(section_name) or "unnamed"
    timestamp = now or datetime.now(timezone.utc)
    return {
        "requestIdentifier": f"section-{_album_token(chat_id)}-{slug}",
        "namespaces": [derive_long_term_namespace(chat_id)],
        "content": {"text": summary},
        "timestamp": timestamp,
        "metadata": build_metadata_map(
            {
                META_TYPE: SECTION_RECORD_TYPE,
                META_SECTION: slug,
                META_PLANTS: list(plants or []),
                META_PHOTO_COUNT: photo_count,
            }
        ),
    }


def filter_records_by_metadata(
    records: list["MemoryContextRecord"], filters: dict[str, Any]
) -> list["MemoryContextRecord"]:
    """Filter retrieved records by custom metadata, client-side (pure).

    Server-side ``metadataFilters`` reject custom keys (see the module note
    above), so equality/membership filtering on our own metadata happens here.
    A record matches when, for every ``(key, expected)`` pair, its metadata has
    that key and either equals ``expected`` or — when the stored value is a list
    — contains it. String comparison is case-insensitive.

    Args:
        records: Normalized records from retrieval.
        filters: Required ``{key: expected}`` metadata pairs; empty means no-op.

    Returns:
        Only the records matching every filter (input order preserved).
    """
    if not filters:
        return list(records)

    def _eq(actual: Any, expected: Any) -> bool:
        if isinstance(actual, str) and isinstance(expected, str):
            return actual.strip().lower() == expected.strip().lower()
        return actual == expected

    def _matches(record: "MemoryContextRecord") -> bool:
        for key, expected in filters.items():
            if key not in record.metadata:
                return False
            actual = record.metadata[key]
            if isinstance(actual, (list, tuple)):
                if not any(_eq(item, expected) for item in actual):
                    return False
            elif not _eq(actual, expected):
                return False
        return True

    return [r for r in records if _matches(r)]


@dataclass
class MemoryContextRecord:
    """A normalized, retrieved memory ready for context assembly.

    This is the in-process projection of an AgentCore ``RetrieveMemoryRecords``
    record reduced to exactly the fields the deterministic assembly pipeline
    needs, so the pipeline stays pure and testable.

    Attributes:
        content: The human-readable memory text injected into the prompt.
        confidence_class: Whether the memory is Explicit or Inferred (Req 5.2).
        topic: The attribute the memory describes, used for conflict resolution
            (Req 5.3). Empty string when the record carries no topic.
        record_id: The originating record id (informational; preserves identity).
    """

    content: str
    confidence_class: Confidence
    topic: str = ""
    record_id: str = ""
    # Flattened custom metadata from the stored record (reserved
    # ``x-amz-agentcore-memory-*`` keys removed). Enables client-side filtering
    # (:func:`filter_records_by_metadata`) since the API rejects custom keys in
    # server-side metadataFilters.
    metadata: dict[str, Any] = field(default_factory=dict)


def assemble_memory_context(
    records: list[MemoryContextRecord],
    *,
    max_memories: int = MAX_MEMORIES_IN_CONTEXT,
) -> list[MemoryContextRecord]:
    """Order, de-conflict, and cap retrieved memories for prompt injection.

    Implements the deterministic pipeline from the design's "Memory Retrieval and
    Context Assembly" section so confidence-priority and the cap are enforced in
    code rather than depending on model behavior. The function is pure: it does
    not mutate ``records`` or perform I/O.

    Pipeline, in order:

    1. **Explicit-over-Inferred conflict resolution.** For each ``topic`` that
       has at least one Explicit record, drop the Inferred records on that same
       topic so the Explicit value wins and the conflicting Inferred value is
       discarded (Req 5.3). Records with an empty topic never conflict and are
       all retained.
    2. **Stable ordering.** Stable-sort so every Explicit record precedes every
       Inferred record, preserving each record's original relative order within
       its confidence class (Req 5.2).
    3. **Cap.** Truncate to ``max_memories`` records (Req 5.4).

    Args:
        records: The memories retrieved for the current chat, in retrieval order.
            Not mutated.
        max_memories: The maximum number of records to retain after ordering;
            defaults to :data:`MAX_MEMORIES_IN_CONTEXT` (50) (Req 5.4).

    Returns:
        A new list with at most ``max_memories`` records: all Explicit records
        first (in original relative order), then the surviving Inferred records
        (in original relative order), with Inferred records that conflict with an
        Explicit record on a non-empty topic removed.
    """
    # 1. Drop Inferred records on any non-empty topic that also has an Explicit
    #    record (Req 5.3). An empty topic is treated as "no topic" so it never
    #    suppresses other records.
    explicit_topics = {
        r.topic
        for r in records
        if r.confidence_class is Confidence.EXPLICIT and r.topic
    }
    deconflicted = [
        r
        for r in records
        if not (
            r.confidence_class is Confidence.INFERRED
            and r.topic
            and r.topic in explicit_topics
        )
    ]

    # 2. Stable-sort Explicit (key 0) before Inferred (key 1); ``sorted`` is
    #    stable, preserving relative order within each class (Req 5.2).
    ordered = sorted(
        deconflicted,
        key=lambda r: 0 if r.confidence_class is Confidence.EXPLICIT else 1,
    )

    # 3. Cap (Req 5.4).
    return ordered[:max_memories]


def render_memory_context(records: list[MemoryContextRecord]) -> str:
    """Render assembled memory records into a system-prompt context block.

    Pure string builder: renders ``records`` in the order given (it does not
    re-sort, re-filter, or re-cap — that is :func:`assemble_memory_context`'s
    job). Returns an empty string when there are no records so the caller emits
    the base persona only (Req 5.5 graceful no-context path).

    Args:
        records: The already-assembled, ordered, capped records to render.

    Returns:
        A markdown context block, or ``""`` when ``records`` is empty.
    """
    if not records:
        return ""
    lines = [
        "## What you remember about this gardener",
        (
            "These notes were recalled from past conversations, ordered with "
            "Explicit (directly stated) facts before Inferred (interpreted) "
            "ones. Use them to give personalized, continuous advice."
        ),
        "",
    ]
    for index, record in enumerate(records, start=1):
        label = "Explicit" if record.confidence_class is Confidence.EXPLICIT else "Inferred"
        lines.append(f"{index}. [{label}] {record.content}")
    return "\n".join(lines)


def build_system_prompt(base_persona: str, memory_context: str) -> str:
    """Compose the Sprout system prompt from the persona and memory context.

    The base persona is the large, stable prefix that benefits from Bedrock
    prompt caching; the memory context is appended after it and is stable within
    a session. The user's new message (which varies per call) is supplied
    separately so the whole system prompt remains a cacheable prefix.

    Args:
        base_persona: The Sprout persona/instructions (from the startup files).
        memory_context: The rendered memory block, or ``""`` when none applies.

    Returns:
        The full system prompt string.
    """
    if not memory_context:
        return base_persona
    return f"{base_persona}\n\n{memory_context}"


def classify_raw_record(raw: dict[str, Any]) -> Confidence:
    """Classify a raw AgentCore record as Explicit or Inferred (Req 5.2).

    USER_PREFERENCE / direct-statement records are Explicit; SEMANTIC /
    behavior-inference records are Inferred. The classification is read from the
    record metadata when present, then derived from the producing strategy, and
    otherwise defaults to Inferred (the conservative choice).

    Args:
        raw: A raw record dict from ``RetrieveMemoryRecords``.

    Returns:
        The :class:`Confidence` classification.
    """
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    explicit_markers = {"EXPLICIT", "DIRECT_STATEMENT", "USER_PREFERENCE"}
    inferred_markers = {"INFERRED", "BEHAVIOR_INFERENCE", "SEMANTIC"}

    for key in ("confidence_class", "confidenceClass", "source", "strategy"):
        value = metadata.get(key) or raw.get(key)
        if isinstance(value, str):
            token = value.strip().upper()
            if token in explicit_markers:
                return Confidence.EXPLICIT
            if token in inferred_markers:
                return Confidence.INFERRED
    return Confidence.INFERRED


def _extract_record_content(raw: dict[str, Any]) -> str:
    """Extract the text content from a raw AgentCore record (shape-tolerant).

    Args:
        raw: A raw record dict from ``RetrieveMemoryRecords``.

    Returns:
        The record text, or ``""`` when none can be resolved.
    """
    content = raw.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    if isinstance(content, str):
        return content
    text = raw.get("text")
    return text if isinstance(text, str) else ""


def normalize_record(raw: dict[str, Any]) -> MemoryContextRecord:
    """Project a raw AgentCore record into a :class:`MemoryContextRecord`.

    Args:
        raw: A raw record dict from ``RetrieveMemoryRecords``.

    Returns:
        The normalized record carrying content, confidence class, and topic.
    """
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    topic = metadata.get("topic") or raw.get("topic") or ""
    record_id = (
        raw.get("memoryRecordId")
        or raw.get("recordId")
        or raw.get("record_id")
        or ""
    )
    return MemoryContextRecord(
        content=_extract_record_content(raw),
        confidence_class=classify_raw_record(raw),
        topic=str(topic),
        record_id=str(record_id),
        metadata=flatten_metadata(metadata),
    )


# =============================================================================
# Persona loading (Req 3.4, 7.1) — from the OpenClaw config startup files
# =============================================================================
_DEFAULT_PERSONA = (
    "You are Sprout, a warm and knowledgeable gardening assistant. Remember the "
    "plants the user grows, track their watering/feeding/pruning schedules, know "
    "their climate zone and growing conditions, provide seasonal advice, and "
    "recall past gardening events. When the user sends a plant photo, identify "
    "the species and its care needs; if you cannot identify it confidently, ask "
    "for a closer photo rather than guessing. Respect stated preferences "
    "(organic vs. synthetic, container vs. in-ground, watering style)."
)

# Startup files in priority order; the persona markdown files copied into the image.
_PERSONA_STARTUP_FILES = ("AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md")


def load_base_persona(config_dir: str = CONFIG_DIR) -> str:
    """Assemble the Sprout base persona from the OpenClaw startup files (Req 7.1).

    Reads the startup markdown files (``AGENTS.md``, ``SOUL.md``, ``USER.md``,
    ``TOOLS.md``, ``IDENTITY.md``) and concatenates them into the persona that
    establishes the Sprout gardening assistant. Missing files are skipped; when
    no startup file can be read, :data:`_DEFAULT_PERSONA` is returned so the
    agent always has a coherent persona.

    Args:
        config_dir: The directory containing the startup files; defaults to this
            module's directory inside the container image.

    Returns:
        The assembled persona string.
    """
    sections: list[str] = []
    for filename in _PERSONA_STARTUP_FILES:
        path = os.path.join(config_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            continue
        if text:
            sections.append(text)
    return "\n\n".join(sections) if sections else _DEFAULT_PERSONA


# =============================================================================
# AgentCore Memory hooks (Req 4.4, 5.1, 5.5)
# =============================================================================
class SproutMemory:
    """AgentCore Memory data-plane hooks for retrieval and persistence.

    Wraps a boto3 ``bedrock-agentcore`` client (created lazily so the module
    imports without boto3 installed). Retrieval is scoped to the user's
    long-term namespace and persistence keys ``CreateEvent`` by ``actorId`` so
    the configured extraction strategies route extracted records to the correct
    namespace server-side. Both methods degrade gracefully — retrieval returns
    an empty list on timeout/error (Req 5.5) and persistence logs and swallows
    failures (Req 4.6).
    """

    def __init__(self, memory_id: str, client: Optional[Any] = None) -> None:
        """Initialize the memory hooks.

        Args:
            memory_id: The AgentCore Memory resource id all operations target.
            client: A preconfigured boto3 ``bedrock-agentcore`` client; created
                lazily on first use when omitted. Injectable for testing.
        """
        self._memory_id = memory_id
        self._client = client

    def _agentcore_client(self) -> Any:
        """Return the wrapped boto3 client, creating it once on first use."""
        if self._client is None:
            import boto3  # Lazy import keeps the module testable without boto3.

            self._client = boto3.client("bedrock-agentcore")
        return self._client

    def retrieve(
        self,
        chat_id: str,
        query: str,
        *,
        metadata_filters: Optional[dict[str, Any]] = None,
    ) -> list[MemoryContextRecord]:
        """Retrieve up to 50 relevant long-term memories (Req 5.1, 5.5).

        Calls ``RetrieveMemoryRecords`` against ``sprout/{chat_id}/long_term``
        with the user's current message as the search query, enforcing a
        3-second budget. On timeout or any error the call degrades gracefully to
        an empty list so the agent still responds based on the current message
        alone (Req 5.5).

        Args:
            chat_id: The Telegram chat id (the authenticated actor).
            query: The user's current message, used as the semantic search query.
            metadata_filters: Optional ``{key: expected}`` custom-metadata pairs
                (e.g. ``{"section": "north_bed"}``). Applied CLIENT-SIDE by
                :func:`filter_records_by_metadata` because the API rejects custom
                keys in server-side ``metadataFilters``.

        Returns:
            The normalized, retrieved records (unassembled), or ``[]`` on
            timeout/error.
        """
        namespace = derive_long_term_namespace(chat_id)

        def _call() -> list[dict[str, Any]]:
            response = self._agentcore_client().retrieve_memory_records(
                memoryId=self._memory_id,
                namespace=namespace,
                searchCriteria={"searchQuery": query},
                maxResults=MAX_MEMORIES_IN_CONTEXT,
            )
            if not isinstance(response, dict):
                return []
            # Data-plane returns ``memoryRecordSummaries``; fall back to
            # ``memoryRecords`` for test fakes.
            return (
                response.get("memoryRecordSummaries")
                or response.get("memoryRecords")
                or []
            )

        # Enforce the 3-second budget without blocking the response: a slow call
        # is abandoned (the worker thread is left to finish in the background and
        # is reclaimed when its blocking call returns) rather than waited on, so
        # the invocation proceeds immediately with no context (Req 5.5).
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_call)
            raw_records = future.result(timeout=MEMORY_RETRIEVE_TIMEOUT_SECONDS)
        except FuturesTimeoutError:
            logger.warning(
                "Memory retrieval exceeded %.0fs; proceeding without context",
                MEMORY_RETRIEVE_TIMEOUT_SECONDS,
            )
            return []
        except Exception:  # noqa: BLE001 — degrade gracefully (Req 5.5).
            logger.warning("Memory retrieval failed; proceeding without context", exc_info=True)
            return []
        finally:
            executor.shutdown(wait=False)

        normalized = [normalize_record(raw) for raw in raw_records if isinstance(raw, dict)]
        return filter_records_by_metadata(normalized, metadata_filters or {})

    def persist(self, chat_id: str, session_id: str, messages: list[dict[str, str]]) -> bool:
        """Persist the session transcript via ``CreateEvent`` (Req 4.4, 4.6).

        Keys the event by ``memoryId`` + ``actorId`` (the chat id) + ``sessionId``
        with an ``eventTimestamp`` and a conversational payload entry per turn,
        triggering server-side asynchronous extraction (Req 4.5). A failure is
        logged and swallowed so the user still receives the response (Req 4.6).

        Args:
            chat_id: The Telegram chat id used as the AgentCore ``actorId``.
            session_id: The per-invocation session id.
            messages: Ordered ``{"role", "content"}`` turns (roles ``USER`` /
                ``ASSISTANT``).

        Returns:
            ``True`` when the event was created, ``False`` when persistence
            failed (the failure is non-fatal).
        """
        payload = [
            {
                "conversational": {
                    "content": {"text": str(message.get("content", ""))},
                    "role": _normalize_role(message.get("role")),
                }
            }
            for message in messages
        ]
        try:
            self._agentcore_client().create_event(
                memoryId=self._memory_id,
                actorId=chat_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(timezone.utc),
                payload=payload,
            )
            return True
        except Exception:  # noqa: BLE001 — non-fatal (Req 4.6).
            logger.error(
                "CreateEvent failed; transcript not persisted, returning response anyway",
                exc_info=True,
            )
            return False

    def write_records(self, records: list[dict[str, Any]]) -> int:
        """Write structured long-term records via ``BatchCreateMemoryRecords``.

        Complements :meth:`persist`: instead of relying on asynchronous
        extraction to derive facts from chat text, this stores records the app
        built deliberately (e.g. a registered backyard section) together with
        queryable metadata. Failures are logged and swallowed — a missing
        structured record must never break the user's turn, since the
        conversational transcript is still persisted.

        Args:
            records: Record dicts from :func:`build_section_record`.

        Returns:
            The number of records AgentCore reported as successfully created
            (``0`` when the call failed or ``records`` is empty).
        """
        if not records:
            return 0
        try:
            response = self._agentcore_client().batch_create_memory_records(
                memoryId=self._memory_id,
                records=records,
            )
        except Exception:  # noqa: BLE001 — non-fatal, mirrors persist().
            logger.error(
                "BatchCreateMemoryRecords failed; structured record not written",
                exc_info=True,
            )
            return 0
        successful = response.get("successfulRecords") or []
        failed = response.get("failedRecords") or []
        if failed:
            logger.warning(
                "BatchCreateMemoryRecords partially failed: %d ok, %d failed",
                len(successful),
                len(failed),
            )
        return len(successful)


def _normalize_role(role: Any) -> str:
    """Normalize a message role to the AgentCore conversational role enum.

    Args:
        role: The raw role value.

    Returns:
        One of ``"USER"``, ``"ASSISTANT"``, ``"TOOL"``, or ``"OTHER"``.
    """
    normalized = str(role or "OTHER").upper()
    return normalized if normalized in ("USER", "ASSISTANT", "TOOL") else "OTHER"


# =============================================================================
# Workspace persistence (Req 9.2, 9.3, 9.4, 9.5)
# =============================================================================
class WorkspaceStore:
    """S3-backed OpenClaw workspace persistence with graceful degradation.

    Downloads the user's workspace prefix at the start of an invocation and
    uploads modified files at the end. Both operations degrade gracefully: a
    download failure continues with default workspace state (Req 9.4) and an
    upload failure logs and returns success while the update is lost (Req 9.5).
    The S3 key prefix is derived per chat id so no two chat ids share a prefix
    (Req 9.6).
    """

    def __init__(self, bucket: Optional[str], client: Optional[Any] = None) -> None:
        """Initialize the workspace store.

        Args:
            bucket: The S3 bucket name; when falsy, workspace persistence is a
                no-op (used when ``WORKSPACE_BUCKET`` is unset).
            client: A preconfigured boto3 ``s3`` client; created lazily on first
                use when omitted. Injectable for testing.
        """
        self._bucket = bucket
        self._client = client

    def _s3_client(self) -> Any:
        """Return the wrapped boto3 S3 client, creating it once on first use."""
        if self._client is None:
            import boto3  # Lazy import keeps the module testable without boto3.

            self._client = boto3.client("s3")
        return self._client

    @staticmethod
    def workspace_prefix(chat_id: str) -> str:
        """Derive the per-user S3 key prefix for a chat id (Req 9.6).

        Args:
            chat_id: The Telegram chat id.

        Returns:
            The unique key prefix ``workspace/{chat_id}/``.
        """
        return f"workspace/{chat_id}/"

    def download(self, chat_id: str, dest_dir: str) -> bool:
        """Download the user's workspace from S3 (Req 9.2, 9.4).

        Args:
            chat_id: The Telegram chat id whose workspace to download.
            dest_dir: The local directory to populate.

        Returns:
            ``True`` when the workspace was downloaded (or was empty), ``False``
            when the download failed and default state is used (Req 9.4).
        """
        if not self._bucket:
            return False
        prefix = self.workspace_prefix(chat_id)
        try:
            client = self._s3_client()
            paginator = client.get_paginator("list_objects_v2")
            os.makedirs(dest_dir, exist_ok=True)
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj["Key"]
                    relative = key[len(prefix):]
                    if not relative:
                        continue
                    # Guard against a crafted S3 object key (e.g. containing
                    # ``..``) escaping dest_dir on write (CWE-22).
                    try:
                        target = resolve_within(dest_dir, relative)
                    except ValueError:
                        logger.warning(
                            "Skipping workspace object with unsafe key: %r", key
                        )
                        continue
                    os.makedirs(os.path.dirname(target) or dest_dir, exist_ok=True)
                    client.download_file(self._bucket, key, target)
            return True
        except Exception:  # noqa: BLE001 — degrade to default state (Req 9.4).
            logger.warning(
                "Workspace download failed; continuing with default state",
                exc_info=True,
            )
            return False

    def upload(self, chat_id: str, source_dir: str) -> bool:
        """Upload modified workspace files to S3 (Req 9.3, 9.5).

        Args:
            chat_id: The Telegram chat id whose workspace to upload.
            source_dir: The local workspace directory to upload.

        Returns:
            ``True`` when the upload succeeded, ``False`` when it failed and the
            update is lost (Req 9.5). Returns ``True`` (no-op) when no bucket is
            configured or the source directory does not exist.
        """
        if not self._bucket or not os.path.isdir(source_dir):
            return True
        prefix = self.workspace_prefix(chat_id)
        try:
            client = self._s3_client()
            for root, _dirs, files in os.walk(source_dir):
                for name in files:
                    local_path = os.path.join(root, name)
                    relative = os.path.relpath(local_path, source_dir)
                    client.upload_file(local_path, self._bucket, prefix + relative)
            return True
        except Exception:  # noqa: BLE001 — non-fatal, update is lost (Req 9.5).
            logger.warning("Workspace upload failed; update lost", exc_info=True)
            return True


# =============================================================================
# Album (multi-photo) buffering — testable S3 key derivation + coordination
# =============================================================================
def _album_token(value: str) -> str:
    """Sanitize a token used in an album S3 key to a safe charset.

    chat_id / media_group_id / message_id come from Telegram; restricting them to
    ``[0-9A-Za-z._-]`` keeps them safe as S3 key segments (no traversal, no
    control chars) even though S3 keys are not filesystem paths.
    """
    return _re.sub(r"[^0-9A-Za-z._-]+", "-", (value or "").strip()).strip("-") or "x"


def album_prefix(chat_id: str, media_group_id: str) -> str:
    """S3 key prefix holding all buffered items for one album."""
    return f"{ALBUM_S3_PREFIX}/{_album_token(chat_id)}/{_album_token(media_group_id)}/"


def album_item_key(chat_id: str, media_group_id: str, message_id: str) -> str:
    """S3 key for a single buffered album item (one per Telegram message)."""
    return f"{album_prefix(chat_id, media_group_id)}items/{_album_token(message_id)}.json"


def album_claim_key(chat_id: str, media_group_id: str) -> str:
    """S3 key of the atomic claim marker electing the album's single processor."""
    return f"{album_prefix(chat_id, media_group_id)}claimed"


class AlbumBuffer:
    """Buffers Telegram album items in S3 and elects one processor per album.

    Uses the existing workspace bucket (no new infrastructure). Each item is a
    small JSON object under ``albums/{chat_id}/{media_group_id}/items/``; a single
    invocation wins the album by atomically creating the ``claimed`` marker via an
    S3 conditional ``PutObject`` (``IfNoneMatch='*'``), which S3 fails with
    ``PreconditionFailed`` for everyone else. S3's strong read-after-write
    consistency makes the post-debounce listing see every item written so far.
    """

    def __init__(
        self,
        bucket: Optional[str],
        *,
        client: Optional[Any] = None,
        debounce_seconds: float = DEFAULT_ALBUM_DEBOUNCE_SECONDS,
    ) -> None:
        self._bucket = bucket
        self._client = client
        self._debounce_seconds = debounce_seconds

    @classmethod
    def from_env(cls, *, client: Optional[Any] = None) -> Optional["AlbumBuffer"]:
        """Build from ``WORKSPACE_BUCKET``; ``None`` when unset (grouping off)."""
        bucket = os.environ.get(WORKSPACE_BUCKET_ENV)
        if not bucket:
            return None
        try:
            debounce = float(
                os.environ.get(
                    ALBUM_DEBOUNCE_SECONDS_ENV, DEFAULT_ALBUM_DEBOUNCE_SECONDS
                )
            )
        except (TypeError, ValueError):
            debounce = DEFAULT_ALBUM_DEBOUNCE_SECONDS
        return cls(bucket, client=client, debounce_seconds=debounce)

    def _s3(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def wait_debounce(self) -> None:
        """Sleep the debounce window so sibling album items can arrive."""
        if self._debounce_seconds > 0:
            time.sleep(self._debounce_seconds)

    def record_item(
        self, chat_id: str, media_group_id: str, message_id: str, item: dict[str, Any]
    ) -> None:
        """Persist one album item (idempotent per message_id)."""
        self._s3().put_object(
            Bucket=self._bucket,
            Key=album_item_key(chat_id, media_group_id, message_id),
            Body=json.dumps(item).encode("utf-8"),
            ContentType="application/json",
        )

    def try_claim(self, chat_id: str, media_group_id: str) -> bool:
        """Atomically claim the album; ``True`` for the single winner.

        Uses an S3 conditional create (``IfNoneMatch='*'``). Returns ``False``
        only when the marker already exists (``PreconditionFailed`` — another
        invocation won). On any other error it degrades to ``True`` (process and
        reply) so an unexpected S3 issue never leaves the album silent — worst
        case reverts to the pre-grouping behavior of replying per item.
        """
        try:
            self._s3().put_object(
                Bucket=self._bucket,
                Key=album_claim_key(chat_id, media_group_id),
                Body=b"1",
                IfNoneMatch="*",
            )
            return True
        except Exception as exc:  # noqa: BLE001 — inspect for the 412 precondition.
            code = None
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "412"):
                return False
            logger.warning(
                "Album claim non-precondition error (processing anyway): %s", exc
            )
            return True

    def list_items(self, chat_id: str, media_group_id: str) -> list[dict[str, Any]]:
        """Return all buffered items for the album, ordered by message id."""
        prefix = album_prefix(chat_id, media_group_id) + "items/"
        items: list[dict[str, Any]] = []
        try:
            client = self._s3()
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    body = client.get_object(Bucket=self._bucket, Key=obj["Key"])[
                        "Body"
                    ].read()
                    try:
                        items.append(json.loads(body))
                    except (ValueError, TypeError):
                        continue
        except Exception:  # noqa: BLE001 — degrade to whatever was read.
            logger.warning("Album listing failed; using partial items", exc_info=True)

        def _key(it: dict[str, Any]) -> tuple[int, str]:
            mid = str(it.get("message_id", ""))
            return (int(mid), mid) if mid.lstrip("-").isdigit() else (1 << 62, mid)

        return sorted(items, key=_key)

    def cleanup(self, chat_id: str, media_group_id: str) -> None:
        """Best-effort delete of the album's buffered items.

        Deletes only the ``items/`` objects, NOT the ``claimed`` marker: keeping
        the marker means a sibling invocation that reaches ``try_claim`` after the
        winner has already processed still loses the claim (exactly-once reply)
        instead of re-claiming an emptied album. The small marker is expired by
        the bucket's ``albums/`` lifecycle rule (media_group_id is unique per
        album, so it is never reused within that window).
        """
        prefix = album_prefix(chat_id, media_group_id) + "items/"
        try:
            client = self._s3()
            paginator = client.get_paginator("list_objects_v2")
            keys = [
                {"Key": obj["Key"]}
                for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix)
                for obj in (page.get("Contents", []) or [])
            ]
            for i in range(0, len(keys), 1000):
                client.delete_objects(
                    Bucket=self._bucket, Delete={"Objects": keys[i : i + 1000]}
                )
        except Exception:  # noqa: BLE001 — cleanup is best-effort.
            logger.debug("Album cleanup failed (non-fatal)", exc_info=True)


# =============================================================================
# Proactive scheduling via EventBridge Scheduler
# =============================================================================
class SproutScheduler:
    """Creates memory-driven reminder schedules in EventBridge Scheduler.

    When the agent decides a reminder is warranted it builds a schedule in the
    dedicated schedule group whose target is the Cron (invoker) Lambda; at fire
    time EventBridge Scheduler assumes :attr:`_role_arn` to invoke that Lambda,
    which re-invokes the runtime (``invocation_type="cron"``) and delivers the
    reply to the user. Configuration comes from the CloudFormation-injected env
    vars (:meth:`from_env`). The boto3 ``scheduler`` client is created lazily so
    this module still imports without boto3 in tests, and the deterministic
    request building is delegated to the pure
    :func:`build_create_schedule_kwargs` so it can be tested without AWS.
    """

    def __init__(
        self,
        *,
        group_name: str,
        invoker_arn: str,
        role_arn: str,
        client: Optional[Any] = None,
    ) -> None:
        """Initialize the scheduler helper.

        Args:
            group_name: The EventBridge Scheduler group holding reminders.
            invoker_arn: The Cron (invoker) Lambda ARN each schedule targets.
            role_arn: The role EventBridge Scheduler assumes to invoke the target.
            client: A preconfigured boto3 ``scheduler`` client; created lazily on
                first use when omitted. Injectable for testing.
        """
        self._group_name = group_name
        self._invoker_arn = invoker_arn
        self._role_arn = role_arn
        self._client = client

    @classmethod
    def from_env(cls) -> Optional["SproutScheduler"]:
        """Build a scheduler from the injected env vars, or ``None`` when unset.

        Returns:
            A configured :class:`SproutScheduler`, or ``None`` when any of the
            required env vars (group / invoker ARN / role ARN) is missing so the
            runtime degrades to "scheduling unavailable" rather than erroring.
        """
        group_name = os.environ.get(CRON_SCHEDULER_GROUP_ENV, "")
        invoker_arn = os.environ.get(CRON_INVOKER_FUNCTION_ARN_ENV, "")
        role_arn = os.environ.get(CRON_SCHEDULER_ROLE_ARN_ENV, "")
        if not (group_name and invoker_arn and role_arn):
            return None
        return cls(group_name=group_name, invoker_arn=invoker_arn, role_arn=role_arn)

    def _scheduler_client(self) -> Any:
        """Return the wrapped boto3 scheduler client, creating it once."""
        if self._client is None:
            import boto3  # Lazy import keeps the module testable without boto3.

            self._client = boto3.client("scheduler")
        return self._client

    def build_kwargs(
        self,
        *,
        chat_id: str,
        schedule_expression: str,
        task: str,
        description: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build the ``create_schedule`` kwargs for a reminder (pure).

        Delegates to :func:`build_create_schedule_kwargs` with this scheduler's
        configured group / target / role, so the request body can be asserted in
        tests without a boto3 client.

        Args:
            chat_id: The Telegram chat id to remind.
            schedule_expression: An EventBridge Scheduler expression.
            task: The task label / type carried in the payload.
            description: Optional human-readable description.

        Returns:
            The kwargs dict for ``scheduler.create_schedule``.
        """
        return build_create_schedule_kwargs(
            chat_id=chat_id,
            schedule_expression=schedule_expression,
            task=task,
            group_name=self._group_name,
            invoker_arn=self._invoker_arn,
            role_arn=self._role_arn,
            description=description,
        )

    def create_schedule(
        self,
        *,
        chat_id: str,
        schedule_expression: str,
        task: str,
        description: Optional[str] = None,
    ) -> bool:
        """Create a reminder schedule, degrading gracefully on error.

        Args:
            chat_id: The Telegram chat id to remind.
            schedule_expression: An EventBridge Scheduler expression.
            task: The task label / type carried in the payload.
            description: Optional human-readable description.

        Returns:
            ``True`` when the schedule was created, ``False`` when the call
            failed (the failure is logged and swallowed so a scheduling error
            never crashes the invocation).
        """
        kwargs = self.build_kwargs(
            chat_id=chat_id,
            schedule_expression=schedule_expression,
            task=task,
            description=description,
        )
        try:
            self._scheduler_client().create_schedule(**kwargs)
            return True
        except Exception:  # noqa: BLE001 — degrade gracefully (log + False).
            logger.error(
                "create_schedule failed (chat_id=%s, task=%s, expr=%s)",
                chat_id,
                task,
                schedule_expression,
                exc_info=True,
            )
            return False


# =============================================================================
# OpenClaw agent integration + Bedrock prompt caching (Req 3.3, 8.2, 8.x)
# =============================================================================
# Telegram-supported image media types -> Bedrock Converse ``image.format`` value.
_IMAGE_FORMAT_BY_MEDIA_TYPE = {
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


def build_system_blocks(system_prompt: str, *, enable_prompt_cache: bool) -> list[dict[str, Any]]:
    """Build Bedrock Converse ``system`` content blocks with optional caching.

    Places a ``cachePoint`` immediately after the system prompt text so the
    static prefix (Sprout persona + retrieved memory context) is cached across
    invocations (5-minute TTL that refreshes on each call). Only the user's new
    message varies between calls, so the cached prefix avoids re-processing the
    whole system prompt — cutting cost and latency on cached input tokens
    (Req 8.x). When caching is disabled, the ``cachePoint`` is omitted.

    Args:
        system_prompt: The composed system prompt (persona + memory context).
        enable_prompt_cache: Whether to insert the ``cachePoint`` (from the
            ``ENABLE_PROMPT_CACHE`` env var, default true).

    Returns:
        The Converse ``system`` parameter as a list of content blocks.
    """
    blocks: list[dict[str, Any]] = [{"text": system_prompt}]
    if enable_prompt_cache:
        # Bedrock Converse marks the end of a cacheable prefix with a cachePoint
        # block; everything before it (the system prompt) becomes the cached
        # static prefix.
        blocks.append({"cachePoint": {"type": "default"}})
    return blocks


def build_user_content_blocks(
    message: str, images: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build Converse user-message content blocks, including any images (Req 8.2).

    Image attachments are passed as multimodal content blocks so the model can
    identify plants from photos (Req 8.2). Each image's base64 ``data`` is
    decoded to raw bytes for the Converse ``image.source.bytes`` field; images
    with an unsupported or missing media type are skipped.

    Args:
        message: The user's text message.
        images: Optional ``[{"data": <base64>, "media_type": <mime>}]`` entries.

    Returns:
        The Converse ``content`` list: image blocks followed by the text block.
    """
    blocks: list[dict[str, Any]] = []
    for image in images or []:
        media_type = str(image.get("media_type", "")).lower()
        image_format = _IMAGE_FORMAT_BY_MEDIA_TYPE.get(media_type)
        data = image.get("data")
        if not image_format or not isinstance(data, str):
            logger.warning("Skipping image with unsupported media type %r", media_type)
            continue
        try:
            raw_bytes = base64.b64decode(data)
        except (ValueError, TypeError):
            logger.warning("Skipping image with undecodable base64 data")
            continue
        blocks.append({"image": {"format": image_format, "source": {"bytes": raw_bytes}}})
    if message:
        blocks.append({"text": message})
    return blocks


class OpenClawAgent:
    """Adapter that forwards a turn to the real OpenClaw gateway subprocess.

    The real OpenClaw runtime is the official Node app (``ghcr.io/openclaw/openclaw``)
    run as ``openclaw gateway run`` on loopback, exposing an OpenAI-compatible
    ``/v1/chat/completions`` endpoint authenticated with a bearer token
    (:data:`OPENCLAW_AUTH_TOKEN_ENV`). This adapter builds the OpenAI-style
    ``messages`` array (system prompt + user message) and POSTs it to the
    gateway, returning ``choices[0].message.content``.

    Bedrock prompt caching (Req 8.x) is configured inside ``openclaw.json``
    (``cacheRetention``) and applied by the gateway, so the adapter no longer
    builds Converse ``cachePoint`` blocks itself. The adapter is injectable so
    :class:`SproutRuntime` stays testable without a live gateway — tests mock
    ``requests.post``.
    """

    def __init__(self, model_id: str, *, enable_prompt_cache: bool) -> None:
        """Initialize the agent adapter.

        Args:
            model_id: The Bedrock model / inference profile id (sent as the
                OpenAI-compatible ``model`` field the gateway maps to Bedrock).
            enable_prompt_cache: Retained for construction compatibility; prompt
                caching is now configured in ``openclaw.json`` and honored by the
                gateway rather than applied here.
        """
        self._model_id = model_id
        self._enable_prompt_cache = enable_prompt_cache

    def process(
        self,
        *,
        system_prompt: str,
        message: str,
        images: Optional[list[dict[str, Any]]] = None,
        workspace_dir: Optional[str] = None,
    ) -> str:
        """Process one conversation turn through OpenClaw or direct Bedrock Converse.

        For text-only turns, forwards to the OpenClaw gateway's
        ``/v1/chat/completions`` (which provides skills, heartbeat, session
        management). For vision turns (images present), calls the Bedrock
        Converse API directly because the in-image OpenClaw version (v2026.2.26)
        drops ``image_url`` content parts. Both paths use the same model and
        system prompt (persona + AgentCore Memory context), so the user
        experience is consistent.

        Args:
            system_prompt: The composed system prompt (persona + memory context).
            message: The user's text message.
            images: Optional image attachments for multimodal identification.
            workspace_dir: The local OpenClaw workspace directory (unused).

        Returns:
            The assistant's response text.

        Raises:
            RuntimeError: If the request cannot be processed.
        """
        if images:
            return self._process_vision(system_prompt=system_prompt, message=message, images=images)
        return self._process_text(system_prompt=system_prompt, message=message)

    def compose_reminder(self, *, system_prompt: str, message: str) -> str:
        """Compose a scheduled reminder via a direct one-shot Bedrock call.

        A cron fire needs a single, clean piece of reminder text — not an
        agentic, tool-using turn. Routing it through the OpenClaw gateway makes
        the model narrate its process and attempt (and fail) to "send" via a
        messaging tool, leaking that noise into the Telegram message. So the
        cron nudge is composed with a plain Bedrock Converse call (same model
        and persona + memory system prompt as normal chat, prompt caching
        preserved) which returns just the assistant text.

        Args:
            system_prompt: The composed system prompt (persona + memory context).
            message: The cron nudge instruction (see
                :func:`build_cron_nudge_prompt`).

        Returns:
            The assistant's reminder text (stripped).

        Raises:
            RuntimeError: If the Bedrock call cannot be completed/parsed.
        """
        import boto3

        system_blocks = build_system_blocks(
            system_prompt, enable_prompt_cache=self._enable_prompt_cache
        )
        try:
            client = boto3.client("bedrock-runtime")
            response = client.converse(
                modelId=self._model_id,
                system=system_blocks,
                messages=[{"role": "user", "content": [{"text": message}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0.4},
            )
            output = response["output"]["message"]["content"]
            text_parts = [block["text"] for block in output if "text" in block]
        except Exception as exc:  # noqa: BLE001 — surface as an agent error.
            raise RuntimeError(f"Bedrock cron nudge composition failed: {exc}") from exc
        return ("\n".join(text_parts)).strip()

    def extract_schedule_directives(
        self, *, user_message: str, assistant_reply: str, now_iso: str
    ) -> list[dict[str, str]]:
        """Second pass: extract agreed reminders from a turn as directives.

        Calls Bedrock Converse directly (bypassing OpenClaw's conversational
        agent loop) with a JSON-only extraction prompt over the user message and
        the assistant's reply, then parses the result into validated
        ``{"expr", "task"}`` directives via :func:`parse_extracted_schedules`.
        Uses the same text model as normal chat (cheap) and forces a small,
        low-temperature completion. Never raises — any failure yields ``[]`` so
        scheduling degrades to "no reminder created" rather than breaking the
        turn.

        Args:
            user_message: The gardener's original message.
            assistant_reply: The assistant's reply to that message.
            now_iso: The current UTC time as an ISO-8601 string ("now").

        Returns:
            The list of extracted, validated directives (possibly empty).
        """
        import boto3

        user_prompt = build_schedule_extraction_user_prompt(
            user_message=user_message, assistant_reply=assistant_reply, now_iso=now_iso
        )
        try:
            client = boto3.client("bedrock-runtime")
            response = client.converse(
                modelId=self._model_id,
                system=[{"text": SCHEDULE_EXTRACTION_SYSTEM_PROMPT}],
                messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0.0},
            )
            output = response["output"]["message"]["content"]
            raw = "\n".join(block["text"] for block in output if "text" in block)
        except Exception:  # noqa: BLE001 — degrade to no extracted schedules.
            logger.exception("Schedule extraction pass failed; no reminder created")
            return []
        # Reject past one-off at() reminders relative to now (avoids phantom
        # schedules that are created then immediately self-deleted).
        return parse_extracted_schedules(raw, now=datetime.now(timezone.utc))

    def _process_vision(
        self,
        *,
        system_prompt: str,
        message: str,
        images: list[dict[str, Any]],
    ) -> str:
        """Handle a vision turn via direct Bedrock Converse API call.

        Uses a dedicated vision model (VISION_MODEL_ID, defaults to Sonnet 4)
        for better image identification accuracy, while text turns use the
        cheaper default model through the OpenClaw gateway.
        """
        import boto3

        vision_model = os.environ.get(VISION_MODEL_ID_ENV, DEFAULT_VISION_MODEL)
        logger.info(
            "Vision path: calling Bedrock Converse directly with %d image(s), model=%s",
            len(images), vision_model
        )

        system_blocks = build_system_blocks(
            system_prompt, enable_prompt_cache=self._enable_prompt_cache
        )
        content_blocks = build_user_content_blocks(
            message or "Please identify and analyze this plant image.", images
        )

        try:
            client = boto3.client("bedrock-runtime")
            response = client.converse(
                modelId=vision_model,
                system=system_blocks,
                messages=[{"role": "user", "content": content_blocks}],
                inferenceConfig={"maxTokens": 4096},
            )
        except Exception as exc:
            raise RuntimeError(f"Bedrock Converse vision call failed: {exc}") from exc

        # Extract the assistant text from the Converse response.
        try:
            output = response["output"]["message"]["content"]
            text_parts = [block["text"] for block in output if "text" in block]
            return "\n".join(text_parts) if text_parts else ""
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Bedrock Converse returned an unparseable response") from exc

    def _process_text(self, *, system_prompt: str, message: str) -> str:
        """Handle a text-only turn via the OpenClaw gateway."""
        # Lazy import keeps the module importable in environments without the
        # ``requests`` dependency (e.g. some unit-test setups).
        import requests

        user_content: Any = message

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        auth_token = os.environ.get(OPENCLAW_AUTH_TOKEN_ENV, "")

        # Make sure the gateway subprocess is up and healthy before forwarding.
        ensure_openclaw_ready()

        try:
            response = requests.post(
                f"{OPENCLAW_URL}/v1/chat/completions",
                json={"model": self._model_id, "messages": messages},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {auth_token}",
                },
                timeout=OPENCLAW_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — surface as an agent error.
            raise RuntimeError("OpenClaw gateway request failed") from exc

        if response.status_code != 200:
            raise RuntimeError(
                f"OpenClaw gateway returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenClaw gateway returned an unparseable response") from exc

        if not isinstance(content, str):
            # Some providers return content as a list of parts; join text parts.
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
            else:
                content = str(content)
        return content


# =============================================================================
# Invocation orchestration (Req 3.2, 3.3) — testable with injected collaborators
# =============================================================================
class SproutRuntime:
    """Assembles the memory hooks, workspace store, and OpenClaw agent.

    A single instance is built per process and reused across invocations for
    warm-container efficiency. Every collaborator is injectable so
    :meth:`handle_invocation` can be exercised with fakes (no live AWS/OpenClaw).
    """

    def __init__(
        self,
        *,
        memory: SproutMemory,
        workspace: WorkspaceStore,
        agent: OpenClawAgent,
        base_persona: str,
        model_id: str,
        scheduler: Optional[SproutScheduler] = None,
        album_buffer: Optional[AlbumBuffer] = None,
        workspace_root: str = "/tmp/sprout-workspace",
    ) -> None:
        """Initialize the runtime with its collaborators.

        Args:
            memory: The AgentCore Memory hooks.
            workspace: The S3 workspace store.
            agent: The OpenClaw agent adapter.
            base_persona: The Sprout persona prefix.
            model_id: The Bedrock model id (surfaced in response metadata).
            scheduler: The proactive-reminder scheduler, or ``None`` when
                scheduling is not configured (env vars unset).
            album_buffer: The S3-backed album buffer for grouping multi-photo
                Telegram albums into one reply, or ``None`` when disabled (no
                workspace bucket) — each photo is then handled individually.
            workspace_root: The local root under which per-chat workspaces live.
        """
        self._memory = memory
        self._workspace = workspace
        self._agent = agent
        self._base_persona = base_persona
        self._model_id = model_id
        self._scheduler = scheduler
        self._album_buffer = album_buffer
        self._workspace_root = workspace_root

    def handle_invocation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run the full invocation lifecycle and return a response envelope.

        Args:
            payload: The invocation payload (``message``, ``user_id``,
                ``session_id``, ``invocation_type``, ``images``, ``attachments``).

        Returns:
            A success envelope ``{"ok": True, "text", "metadata"}`` or an error
            envelope ``{"ok": False, "error", "message"}``.
        """
        # 1. Validate identity (Req 3.2, 3.5). The chat id is the actor for every
        #    memory operation; reject when it is missing/blank.
        chat_id = payload.get("user_id")
        if not isinstance(chat_id, str) or not chat_id.strip():
            logger.warning("Rejecting invocation: missing user_id")
            return _error_envelope("INVALID_USER_ID", "A non-empty user_id is required.")
        chat_id = chat_id.strip()
        # Enforce the Telegram chat-id allowlist so the id is safe to use as a
        # filesystem path segment / S3 key prefix / memory namespace (prevents
        # path traversal, CWE-22 / CodeQL py/path-injection).
        if not is_valid_chat_id(chat_id):
            logger.warning("Rejecting invocation: malformed user_id")
            return _error_envelope("INVALID_USER_ID", "A valid numeric user_id is required.")

        message = str(payload.get("message", ""))
        session_id = payload.get("session_id") or uuid.uuid4().hex
        images = payload.get("images") if isinstance(payload.get("images"), list) else []

        # A scheduled ("cron") fire carries no human message; instead the Cron
        # invoker sends invocation_type="cron" with a task_type. The cron path
        # retrieves the user's long-term memory and asks the model to compose a
        # personalized "what's due" nudge (or nothing, when nothing is due).
        invocation_type = str(payload.get("invocation_type", "")).strip().lower()
        is_cron = invocation_type == CRON_INVOCATION_TYPE
        task_type = str(payload.get("task_type") or message or "scheduled_check").strip()

        # 1b. Album grouping: when this message is part of a Telegram photo album
        #     (media_group_id present), buffer it and let a single invocation
        #     process the whole album once. Non-winners return an empty reply so
        #     Telegram receives nothing from them. The winner continues below
        #     with the merged caption + all album images.
        album_section_caption = ""
        media_group_id = payload.get(MEDIA_GROUP_ID_KEY)
        if media_group_id and not is_cron and self._album_buffer is not None:
            assembled_album = self._assemble_album(chat_id, str(media_group_id), payload)
            if assembled_album is None:
                logger.info(
                    "Album %s: buffered item, another invocation will reply",
                    media_group_id,
                )
                return {
                    "ok": True,
                    "text": "",
                    "metadata": {
                        "invocation_type": invocation_type or "webhook",
                        "album_role": "buffered",
                        "media_group_id": str(media_group_id),
                        "session_id": session_id,
                        "transcript_persisted": False,
                    },
                }
            message, images, album_section_caption = assembled_album

        # 2. Workspace download (graceful degradation, Req 9.2, 9.4). chat_id is
        #    allowlist-validated above; resolve_within is a second barrier that
        #    keeps the workspace dir inside the root (CWE-22 defense).
        workspace_dir = resolve_within(self._workspace_root, chat_id)
        self._workspace.download(chat_id, workspace_dir)

        # 3. Retrieve + 4. assemble memory context (Req 5.1-5.5).
        # When the message is empty (photo-only, no caption) or this is a cron
        # fire, use a sensible task-relevant fallback query so the retrieve call
        # doesn't fail on the min-length validation (AgentCore requires
        # searchQuery to be non-empty) and surfaces schedule-relevant memories.
        if is_cron:
            search_query = f"garden care tasks due: {task_type}"
        else:
            search_query = message or "plant identification and garden care"
        retrieved = self._memory.retrieve(chat_id, search_query)
        assembled = assemble_memory_context(retrieved)
        memory_context = render_memory_context(assembled)

        # 5. Inject context into the system prompt (cached prefix, Req 8.x).
        system_prompt = build_system_prompt(self._base_persona, memory_context)

        # 6. Process through OpenClaw. For cron, the "message" is the nudge
        #    instruction and scheduled fires never carry images; for a normal
        #    turn, forward the user's message (and any images, Req 8.2).
        # Give the model the current UTC time so it can compute correct schedule
        # times (OpenClaw's cron tool needs absolute at()/cron() expressions; an
        # LLM has no clock and otherwise guesses a stale/past date). Injected into
        # the user turn (not the cached system prompt) so prompt caching is
        # preserved; the original message is what gets persisted to memory.
        now_hint = (
            "\n\n[System context: the current date and time is "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} (UTC). "
            "Use this as 'now' when scheduling reminders.]"
        )
        try:
            if is_cron:
                # A scheduled nudge is a one-shot text composition, not an
                # agentic turn — compose it via a direct Bedrock call so the
                # OpenClaw agent loop does not narrate tool use / attempt to
                # "send" it (which leaks noise into the Telegram message).
                response_text = self._agent.compose_reminder(
                    system_prompt=system_prompt,
                    message=build_cron_nudge_prompt(task_type),
                )
            else:
                response_text = self._agent.process(
                    system_prompt=system_prompt,
                    message=message + now_hint,
                    images=images,
                    workspace_dir=workspace_dir,
                )
        except Exception:  # noqa: BLE001 — return an agent-error envelope.
            logger.exception("OpenClaw agent failed to process the invocation")
            return _error_envelope("AGENT_ERROR", "The assistant failed to process the request.")

        # For a cron fire, translate the model output into a deliverable nudge:
        # an empty result (or the NOTHING_DUE sentinel) means nothing is due, so
        # return an ok envelope with empty text and skip persistence — the Cron
        # invoker then delivers nothing to Telegram. A synthetic cron turn is
        # never persisted so it can't pollute long-term memory extraction.
        if is_cron:
            nudge = interpret_cron_response(response_text)
            self._workspace.upload(chat_id, workspace_dir)
            return {
                "ok": True,
                "text": nudge,
                "metadata": {
                    "model_id": self._model_id,
                    "memory_records_retrieved": len(assembled),
                    "session_id": session_id,
                    "invocation_type": CRON_INVOCATION_TYPE,
                    "task_type": task_type,
                    "nudge_delivered": bool(nudge),
                    "transcript_persisted": False,
                },
            }

        # 6b. Create any reminders the agent committed to via [[SCHEDULE ...]]
        #     directives, then strip them from the user-facing reply.
        response_text, schedules_created = self._apply_schedule_directives(
            chat_id, response_text
        )

        # 6c. Deterministic fallback: if the model agreed to a reminder in prose
        #     but did not emit a directive (the common case with a small chat
        #     model), run the focused JSON extraction second pass over the turn
        #     and create any reminders it finds. Gated on intent keywords so
        #     ordinary chat never triggers an extra model call.
        if (
            schedules_created == 0
            and self._scheduler is not None
            and (has_scheduling_intent(message) or has_scheduling_intent(response_text))
        ):
            schedules_created += self._extract_and_create_schedules(
                chat_id=chat_id, user_message=message, assistant_reply=response_text
            )

        # 7. Persist transcript (non-fatal, Req 4.4, 4.6). Persist the cleaned
        #    reply (directives removed) so memory extraction never sees the tags.
        persisted = self._memory.persist(
            chat_id,
            session_id,
            [
                {"role": "USER", "content": message},
                {"role": "ASSISTANT", "content": response_text},
            ],
        )

        # 7b. Register a named backyard section as a STRUCTURED record when the
        #     gardener captioned a photo album (e.g. "North bed"). This is
        #     written deliberately with queryable metadata rather than left to
        #     asynchronous extraction, so "what's in the north bed" is a durable
        #     fact. Non-fatal: a failure never affects the reply.
        sections_written = 0
        if album_section_caption:
            sections_written = self._memory.write_records(
                [
                    build_section_record(
                        chat_id=chat_id,
                        section_name=album_section_caption,
                        summary=(
                            f"Backyard section '{album_section_caption}' "
                            f"(registered from {len(images)} photo(s)): {response_text}"
                        ),
                        photo_count=len(images),
                    )
                ]
            )

        # 8. Workspace upload (graceful degradation, Req 9.3, 9.5).
        self._workspace.upload(chat_id, workspace_dir)

        return {
            "ok": True,
            "text": response_text,
            "metadata": {
                "model_id": self._model_id,
                "memory_records_retrieved": len(assembled),
                "session_id": session_id,
                "transcript_persisted": persisted,
                "sections_written": sections_written,
                "schedules_created": schedules_created,
            },
        }

    def _apply_schedule_directives(self, chat_id: str, response_text: str) -> tuple[str, int]:
        """Create reminders from ``[[SCHEDULE ...]]`` directives in the reply.

        Parses the agent's reply for schedule directives (:func:`parse_schedule_directives`),
        creates each reminder via the configured scheduler, and returns the reply
        with the directive tags removed plus the count of schedules created. When
        scheduling is not configured (env vars unset -> ``self._scheduler is None``)
        the tags are still stripped so the user never sees raw markup, and the
        count is zero. If the model emitted only a directive with no prose, a
        short confirmation is substituted so the reply is not empty.

        Args:
            chat_id: The Telegram chat id the reminders belong to.
            response_text: The raw agent reply.

        Returns:
            A ``(cleaned_text, schedules_created)`` tuple.
        """
        cleaned, directives = parse_schedule_directives(response_text)
        created = 0
        if directives:
            if self._scheduler is None:
                logger.warning(
                    "Agent requested %d reminder schedule(s) but scheduling is "
                    "not configured; ignoring",
                    len(directives),
                )
            else:
                for directive in directives:
                    if self._scheduler.create_schedule(
                        chat_id=chat_id,
                        schedule_expression=directive["expr"],
                        task=directive["task"],
                    ):
                        created += 1
        if not cleaned and created:
            cleaned = "Done \u2014 I'll send you a reminder. \U0001f331"
        return cleaned, created

    def _extract_and_create_schedules(
        self, *, chat_id: str, user_message: str, assistant_reply: str
    ) -> int:
        """Run the extraction second pass and create the reminders it finds.

        Fallback for when the model agreed to a reminder in prose without
        emitting a ``[[SCHEDULE ...]]`` directive. Delegates the model call to
        :meth:`OpenClawAgent.extract_schedule_directives` (which returns
        pre-validated directives) and creates each via the configured scheduler.
        Never raises — extraction failures already degrade to an empty list, and
        any per-schedule create failure is logged and skipped by the scheduler.

        Args:
            chat_id: The Telegram chat id the reminders belong to.
            user_message: The gardener's original message.
            assistant_reply: The assistant's (directive-free) reply.

        Returns:
            The count of schedules successfully created.
        """
        if self._scheduler is None:
            return 0
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        directives = self._agent.extract_schedule_directives(
            user_message=user_message, assistant_reply=assistant_reply, now_iso=now_iso
        )
        created = 0
        for directive in directives:
            if self._scheduler.create_schedule(
                chat_id=chat_id,
                schedule_expression=directive["expr"],
                task=directive["task"],
            ):
                created += 1
        if created:
            logger.info(
                "Schedule extraction pass created %d reminder(s) for chat_id=%s",
                created,
                chat_id,
            )
        return created

    def _assemble_album(
        self, chat_id: str, media_group_id: str, payload: dict[str, Any]
    ) -> Optional[tuple[str, list[dict[str, Any]]]]:
        """Buffer this album item and, for the single winner, return the group.

        Records the current item, waits the debounce window for siblings, then
        atomically claims the album. The one invocation that wins the claim
        returns ``(merged_message, all_images)`` — the merged caption plus every
        album photo (capped at :data:`MAX_ALBUM_IMAGES`) — and the rest return
        ``None`` so the caller replies with nothing for them.

        Args:
            chat_id: The Telegram chat id (album owner).
            media_group_id: The Telegram album identifier.
            payload: This item's invocation payload (``message``/caption,
                ``images``, ``message_id``).

        Returns:
            ``(message, images, section_caption)`` for the album's single
            processor — where ``section_caption`` is the gardener's caption for
            the group (``""`` when none) so the caller can register it as a
            backyard section — or ``None`` for a buffered (non-winning) item.
        """
        buffer = self._album_buffer
        message_id = str(payload.get(MESSAGE_ID_KEY) or uuid.uuid4().hex)
        item = {
            "message_id": message_id,
            "caption": str(payload.get("message", "")),
            "images": payload.get("images") if isinstance(payload.get("images"), list) else [],
        }
        try:
            buffer.record_item(chat_id, media_group_id, message_id, item)
        except Exception:  # noqa: BLE001 — if buffering fails, fall back to
            # handling this single item normally rather than dropping it.
            logger.warning("Album buffering failed; handling item individually", exc_info=True)
            return str(payload.get("message", "")), item["images"], ""

        buffer.wait_debounce()
        if not buffer.try_claim(chat_id, media_group_id):
            return None

        items = buffer.list_items(chat_id, media_group_id)
        all_images: list[dict[str, Any]] = []
        captions: list[str] = []
        for entry in items:
            for img in entry.get("images") or []:
                if len(all_images) < MAX_ALBUM_IMAGES:
                    all_images.append(img)
            caption = (entry.get("caption") or "").strip()
            if caption:
                captions.append(caption)
        buffer.cleanup(chat_id, media_group_id)

        section_caption = captions[0] if captions else ""
        merged_message = build_album_prompt(section_caption, len(all_images))
        logger.info(
            "Album %s claimed: %d photos, %d captions",
            media_group_id,
            len(all_images),
            len(captions),
        )
        return merged_message, all_images, section_caption


def _error_envelope(error: str, message: str) -> dict[str, Any]:
    """Build a non-raising error envelope for the host.

    Args:
        error: A stable error code identifying the rejection reason.
        message: A non-PII human-readable description.

    Returns:
        ``{"ok": False, "error", "message"}``.
    """
    return {"ok": False, "error": error, "message": message}


def _prompt_cache_enabled() -> bool:
    """Resolve the ``ENABLE_PROMPT_CACHE`` env var (default true, Req 8.x).

    Returns:
        ``True`` unless the env var is explicitly set to a falsy token.
    """
    raw = os.environ.get(ENABLE_PROMPT_CACHE_ENV)
    if raw is None:
        return DEFAULT_PROMPT_CACHE
    return raw.strip().lower() not in ("0", "false", "no", "off")


# --- Lazily-built, warm-container-reused runtime singleton --------------------
_runtime: Optional[SproutRuntime] = None


def get_runtime() -> SproutRuntime:
    """Return the process-cached :class:`SproutRuntime`, building it on first use.

    Reads configuration from the environment the AgentCore Runtime injects
    (``MODEL_ID``, ``MEMORY_ID``, ``WORKSPACE_BUCKET``, ``ENABLE_PROMPT_CACHE``)
    and assembles the collaborators. Reused across invocations for warm-container
    efficiency.

    Returns:
        The assembled runtime.
    """
    global _runtime
    if _runtime is None:
        model_id = os.environ.get(MODEL_ID_ENV, "")
        memory_id = os.environ.get(MEMORY_ID_ENV, "")
        workspace_bucket = os.environ.get(WORKSPACE_BUCKET_ENV)
        enable_cache = _prompt_cache_enabled()
        _runtime = SproutRuntime(
            memory=SproutMemory(memory_id),
            workspace=WorkspaceStore(workspace_bucket),
            agent=OpenClawAgent(model_id, enable_prompt_cache=enable_cache),
            base_persona=load_base_persona(),
            model_id=model_id,
            scheduler=SproutScheduler.from_env(),
            album_buffer=AlbumBuffer.from_env(),
        )
    return _runtime


# =============================================================================
# OpenClaw gateway subprocess lifecycle (real OpenClaw Node runtime)
# =============================================================================
def _substitute_env_vars(text: str) -> str:
    """Expand ``${VAR}`` references in ``text`` from the environment.

    Mirrors the reference runtime's config substitution: any ``${VAR}`` token is
    replaced with ``os.environ[VAR]`` when set, and left untouched otherwise so a
    missing variable is visible rather than silently blanked.

    Args:
        text: The raw config text containing ``${VAR}`` tokens.

    Returns:
        The text with known environment variables substituted.
    """
    import re

    def _replace(match: "re.Match[str]") -> str:
        var_name = match.group(1)
        return os.environ.get(var_name, match.group(0))

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


def _prepare_openclaw_config(
    src: str = os.path.join(CONFIG_DIR, OPENCLAW_CONFIG_SRC),
    dst: str = OPENCLAW_CONFIG_DST,
) -> str:
    """Render ``openclaw.json`` with env substitution to the gateway config path.

    Reads the packaged ``openclaw.json``, substitutes ``${VAR}`` tokens
    (``OPENCLAW_AUTH_TOKEN``, ``AWS_REGION``, ``BEDROCK_MODEL_ID``), and writes
    the result to ``dst`` (creating its directory). Before substitution, if
    ``BEDROCK_MODEL_ID`` is unset but ``MODEL_ID`` is, it is exported from
    ``MODEL_ID`` so a single model id can drive both server.py and the gateway.

    Args:
        src: The packaged config template path.
        dst: The gateway's expected config path.

    Returns:
        ``dst`` (the written config path).
    """
    # Reconcile the model id env var names: the AgentCore Runtime injects
    # MODEL_ID; the gateway config expects BEDROCK_MODEL_ID. Export the latter
    # from the former when only MODEL_ID is provided.
    if not os.environ.get(BEDROCK_MODEL_ID_ENV) and os.environ.get(MODEL_ID_ENV):
        os.environ[BEDROCK_MODEL_ID_ENV] = os.environ[MODEL_ID_ENV]

    with open(src, "r", encoding="utf-8") as handle:
        rendered = _substitute_env_vars(handle.read())

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as handle:
        handle.write(rendered)
    return dst


def start_openclaw() -> Any:
    """Start the OpenClaw gateway subprocess and return the ``Popen`` handle.

    Renders the config (:func:`_prepare_openclaw_config`), then launches
    ``openclaw gateway run --allow-unconfigured`` with ``OPENCLAW_SKIP_ONBOARDING``
    and ``OPENCLAW_CONFIG_PATH`` set, streaming its stdout/stderr to the module
    logger.

    Returns:
        The ``subprocess.Popen`` handle for the running gateway.
    """
    import subprocess
    import threading

    config_dst = _prepare_openclaw_config()

    env = os.environ.copy()
    env["OPENCLAW_SKIP_ONBOARDING"] = "1"
    env["OPENCLAW_CONFIG_PATH"] = config_dst

    logger.info("Starting OpenClaw gateway subprocess (config=%s)", config_dst)
    proc = subprocess.Popen(
        ["openclaw", "gateway", "run", "--allow-unconfigured"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def _pump_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if not decoded:
                continue
            lowered = decoded.lower()
            if "error" in lowered or "exception" in lowered or "failed" in lowered:
                logger.error("[openclaw] %s", decoded)
            else:
                logger.info("[openclaw] %s", decoded)

    threading.Thread(target=_pump_output, daemon=True).start()
    return proc


def wait_for_openclaw(timeout: int = OPENCLAW_STARTUP_TIMEOUT_SECONDS) -> bool:
    """Poll the gateway ``/health`` endpoint until it is ready (or times out).

    Args:
        timeout: Maximum seconds to wait for a healthy response.

    Returns:
        ``True`` when the gateway reported healthy within the budget, ``False``
        otherwise (startup continues either way, matching the reference).
    """
    import time

    import requests

    auth_token = os.environ.get(OPENCLAW_AUTH_TOKEN_ENV, "")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = requests.get(
                f"{OPENCLAW_URL}/health",
                headers={"Authorization": f"Bearer {auth_token}"},
                timeout=2,
            )
            if response.status_code == 200:
                logger.info("OpenClaw gateway ready (status=%d)", response.status_code)
                return True
        except Exception:  # noqa: BLE001 — not ready yet; keep polling.
            pass
        time.sleep(1)

    logger.warning(
        "OpenClaw gateway did not report healthy within %ds; continuing anyway",
        timeout,
    )
    return False


def ensure_openclaw_ready(timeout: int = OPENCLAW_STARTUP_TIMEOUT_SECONDS) -> bool:
    """Ensure the OpenClaw gateway subprocess is running and healthy.

    AgentCore Runtime freezes/thaws the container between invocations and cold
    starts frequently under low traffic; the Node gateway can take longer than
    the initial boot wait to become ready, and a thawed container may find the
    subprocess gone. This (re)starts the gateway when it is not running, then
    polls ``/health`` until ready, so the invocation path never forwards a turn
    to a dead loopback port (the ``Connection refused`` failure mode).

    Args:
        timeout: Maximum seconds to wait for the gateway to report healthy.

    Returns:
        ``True`` when the gateway is healthy within the budget, ``False``
        otherwise (the caller may still attempt the request and surface a clear
        error).
    """
    global _openclaw_proc
    with _openclaw_start_lock:
        needs_start = _openclaw_proc is None or _openclaw_proc.poll() is not None
        if needs_start:
            if _openclaw_proc is not None:
                logger.warning("OpenClaw gateway subprocess is not running; restarting it")
            try:
                _openclaw_proc = start_openclaw()
            except Exception:  # noqa: BLE001 — surface via the health poll below.
                logger.exception("Failed to (re)start the OpenClaw gateway subprocess")
                return False
    return wait_for_openclaw(timeout)


# =============================================================================
# HTTP server contract (Req 3.2, 3.7)
# =============================================================================
class SproutRuntimeHandler(BaseHTTPRequestHandler):
    """Implements the AgentCore Runtime HTTP service contract for Sprout."""

    server_version = "SproutAgentCore/1.0"

    def _write_json(self, status: int, body: dict[str, Any]) -> None:
        """Write a JSON response with the given status code.

        Args:
            status: The HTTP status code.
            body: The JSON-serializable response body.
        """
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 (http.server callback name)
        """Handle the ``GET /ping`` health check (Req 3.7)."""
        if self.path.rstrip("/") == PING_PATH.rstrip("/") or self.path == "/":
            self._write_json(200, {"status": "Healthy"})
            return
        self._write_json(404, {"ok": False, "error": "NOT_FOUND", "message": self.path})

    def do_POST(self) -> None:  # noqa: N802 (http.server callback name)
        """Handle the ``POST /invocations`` agent entry point (Req 3.2)."""
        if self.path.rstrip("/") != INVOCATIONS_PATH.rstrip("/"):
            self._write_json(404, {"ok": False, "error": "NOT_FOUND", "message": self.path})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        try:
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
        except ValueError as exc:
            self._write_json(
                400, {"ok": False, "error": "INVALID_REQUEST", "message": str(exc)}
            )
            return

        try:
            result = get_runtime().handle_invocation(payload)
        except Exception:  # noqa: BLE001 — never crash the runtime container.
            logger.exception("Unhandled error while invoking the Sprout agent")
            self._write_json(
                500,
                {
                    "ok": False,
                    "error": "INTERNAL_ERROR",
                    "message": "The agent failed to process the request.",
                },
            )
            return

        # ``handle_invocation`` returns a non-raising envelope; surface rejections
        # (INVALID_USER_ID / AGENT_ERROR) as 200 with the envelope so the caller
        # reads the error code.
        self._write_json(200, result)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route access logs through the module logger (no stderr spam)."""
        logger.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    """Start the OpenClaw gateway then the AgentCore Runtime HTTP server.

    The real OpenClaw runtime is a Node subprocess started via
    :func:`ensure_openclaw_ready`. The HTTP server (which answers ``/ping`` fast
    for AgentCore health checks) starts regardless of gateway readiness; each
    ``/invocations`` turn re-ensures the gateway is up before forwarding, so a
    slow cold start or a freeze/thaw that dropped the subprocess is recovered on
    demand rather than permanently failing the container.
    """
    try:
        ensure_openclaw_ready()
    except Exception:  # noqa: BLE001 — never let gateway startup crash the container.
        logger.exception("Initial OpenClaw gateway startup failed; will retry per invocation")

    port = int(os.environ.get("PORT", DEFAULT_PORT))
    # AgentCore Runtime contract requires the container to listen on 0.0.0.0:8080
    # so the runtime host can reach it; binding a narrower interface is not viable
    # inside the managed container. Not externally exposed (see Dockerfile).
    server = ThreadingHTTPServer(("0.0.0.0", port), SproutRuntimeHandler)  # nosec B104 - required by AgentCore container contract
    logger.info("Sprout AgentCore Runtime server listening on :%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - container stop signal
        logger.info("Shutting down Sprout AgentCore Runtime server")
    finally:
        server.server_close()
        if _openclaw_proc is not None:
            _openclaw_proc.terminate()


if __name__ == "__main__":
    main()
