"""Cron Lambda entry point for the Sprout gardening assistant.

This handler is invoked by the EventBridge Scheduler rule defined in
``openclaw-telegram.yaml`` (``CronSchedule``). It drives the scheduled-task path
described in the design's *Cron Lambda* section and Requirement 12:

1. Parse the EventBridge Scheduler payload (``task_type``, ``chat_ids``).
2. For each target chat id, invoke the AgentCore Runtime with a cron-type
   payload (``invocation_type="cron"``) (Req 11.2 / 12.2).
3. If the runtime returns a user-facing message, deliver it via the Telegram
   Bot API, splitting messages longer than 4096 characters into sequential
   chunks (Req 11.3 / 12.3).
4. Telegram delivery retries up to 3 total attempts per chunk; if delivery
   still fails, the failure is logged with the chat id and task identifier and
   the message is discarded (Req 11.5 / 12.5).
5. The handler is bounded to complete within the Lambda's 120-second timeout
   (Req 11.6 / 12.6) by tracking the remaining execution budget and stopping
   before it is exhausted.

The runtime invocation is kept self-contained here (it does not depend on
``handler.py``) while Telegram delivery and bot-token retrieval are reused from
:mod:`telegram_api`.

Environment variables (injected by CloudFormation):

* ``AGENTCORE_RUNTIME_ARN`` — ARN of the AgentCore Runtime to invoke.
* ``BOT_TOKEN_SECRET_ARN`` — Secrets Manager ARN of the Telegram bot token.
* ``TELEGRAM_API_BASE`` — Telegram Bot API base URL (consumed by
  :mod:`telegram_api`).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Optional

import boto3
from botocore.config import Config

import telegram_api

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment variable names ----------------------------------------------
RUNTIME_ARN_ENV = "AGENTCORE_RUNTIME_ARN"
BOT_TOKEN_SECRET_ARN_ENV = "BOT_TOKEN_SECRET_ARN"  # nosec B105 - env var name, not a secret

# --- Cron invocation tuning ---------------------------------------------------
# Default task type when the scheduler payload omits one (Req 12.1/12.2).
DEFAULT_TASK_TYPE = "scheduled_check"
# AgentCore requires a runtimeSessionId of at least 33 characters.
_MIN_SESSION_ID_LENGTH = 33
# Per-invocation runtime read timeout. The Lambda has a 120s ceiling; keep each
# runtime call well under that so multiple chat ids can be processed and so we
# always have headroom to deliver responses (Req 12.6).
_RUNTIME_READ_TIMEOUT_SECONDS = 45
_RUNTIME_CONNECT_TIMEOUT_SECONDS = 10
# Stop starting new runtime invocations once fewer than this many milliseconds
# of Lambda execution budget remain, leaving time for in-flight delivery.
_TIME_BUDGET_FLOOR_MS = 8000

# --- Cold-start-reused AgentCore data-plane client ---------------------------
_agentcore_client = None


def _get_agentcore_client():
    """Return the process-cached AgentCore Runtime data-plane client.

    The client is created once per execution environment and reused across
    invocations. A bounded read timeout keeps a slow runtime call from
    consuming the entire Lambda budget (Req 12.6).

    Returns:
        A boto3 ``bedrock-agentcore`` client.
    """
    global _agentcore_client
    if _agentcore_client is None:
        _agentcore_client = boto3.client(
            "bedrock-agentcore",
            config=Config(
                read_timeout=_RUNTIME_READ_TIMEOUT_SECONDS,
                connect_timeout=_RUNTIME_CONNECT_TIMEOUT_SECONDS,
                retries={"max_attempts": 1},
            ),
        )
    return _agentcore_client


def _new_session_id(chat_id: str) -> str:
    """Build a runtime session id for a cron invocation.

    AgentCore requires the ``runtimeSessionId`` to be at least 33 characters;
    this combines a ``cron`` prefix, the chat id, and a uuid4 hex suffix and
    pads if necessary to satisfy the minimum length.

    Args:
        chat_id: The target Telegram chat id.

    Returns:
        A session id string at least 33 characters long.
    """
    session_id = f"cron-{chat_id}-{uuid.uuid4().hex}"
    if len(session_id) < _MIN_SESSION_ID_LENGTH:
        session_id = (session_id + uuid.uuid4().hex)[:_MIN_SESSION_ID_LENGTH]
    return session_id


def _parse_event(event: Any) -> tuple[str, list[str]]:
    """Extract the task type and target chat ids from the scheduler payload.

    The EventBridge Scheduler target is configured without a static input, so
    the event may be an empty object. When present, the payload carries
    ``task_type`` and ``chat_ids`` (Req 12.2). Chat ids are normalized to
    non-empty strings; malformed entries are skipped.

    Args:
        event: The Lambda event delivered by EventBridge Scheduler.

    Returns:
        A ``(task_type, chat_ids)`` tuple.
    """
    if not isinstance(event, dict):
        return DEFAULT_TASK_TYPE, []

    task_type = event.get("task_type") or DEFAULT_TASK_TYPE

    raw_chat_ids = event.get("chat_ids", [])
    if not isinstance(raw_chat_ids, list):
        logger.warning("chat_ids is not a list; ignoring (task_type=%s)", task_type)
        return str(task_type), []

    chat_ids: list[str] = []
    for raw in raw_chat_ids:
        if raw is None:
            continue
        chat_id = str(raw).strip()
        if chat_id:
            chat_ids.append(chat_id)
    return str(task_type), chat_ids


def _extract_response_text(raw_body: bytes | str) -> Optional[str]:
    """Parse the runtime response body and return its user-facing text.

    The container's ``/invocations`` handler returns a JSON envelope
    ``{"ok": bool, "text": str, ...}``. This returns the stripped ``text`` when
    the envelope reports success and carries a non-empty message; otherwise it
    returns ``None`` so no Telegram message is sent (Req 11.3).

    Args:
        raw_body: The raw runtime response body (bytes or string).

    Returns:
        The user-facing message text, or ``None`` when there is nothing to send.
    """
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode("utf-8", errors="replace")
    raw_body = raw_body.strip()
    if not raw_body:
        return None

    try:
        envelope = json.loads(raw_body)
    except ValueError:
        logger.warning("Runtime response was not valid JSON; skipping delivery")
        return None

    if not isinstance(envelope, dict) or not envelope.get("ok"):
        logger.warning("Runtime returned a non-success envelope; skipping delivery")
        return None

    text = envelope.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _read_runtime_body(response: dict) -> bytes:
    """Read the AgentCore ``invoke_agent_runtime`` response body to bytes.

    The data-plane returns the agent output under the ``response`` key as a
    streaming body; some botocore versions surface an already-materialized
    bytes/str value or an iterable of chunks. This normalizes all of those to a
    single ``bytes`` value.

    Args:
        response: The dict returned by ``invoke_agent_runtime``.

    Returns:
        The response body as bytes (empty when absent).
    """
    body = response.get("response")
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    if hasattr(body, "read"):
        return body.read()
    # Fall back to treating it as an iterable of chunks.
    try:
        return b"".join(
            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
            for chunk in body
        )
    except TypeError:
        return b""


def _invoke_runtime(
    runtime_arn: str,
    chat_id: str,
    task_type: str,
) -> Optional[str]:
    """Invoke the AgentCore Runtime for one chat id and return its message text.

    Sends a cron-type payload (``invocation_type="cron"``) with the chat id as
    the user identifier (Req 11.2 / 12.2). Runtime failures are logged and
    swallowed so that one failing chat id does not abort the whole cron cycle.

    Args:
        runtime_arn: The AgentCore Runtime ARN to invoke.
        chat_id: The target Telegram chat id (used as ``user_id``).
        task_type: The scheduled task type forwarded in the payload.

    Returns:
        The user-facing message text, or ``None`` when there is nothing to send
        or the invocation failed.
    """
    session_id = _new_session_id(chat_id)
    payload = {
        "message": task_type,
        "user_id": chat_id,
        "session_id": session_id,
        "invocation_type": "cron",
        "task_type": task_type,
    }

    try:
        response = _get_agentcore_client().invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
    except Exception as exc:  # noqa: BLE001 — isolate per-chat failures.
        logger.error(
            "AgentCore invocation failed (chat_id=%s, task=%s): %s",
            chat_id,
            task_type,
            exc,
        )
        return None

    return _extract_response_text(_read_runtime_body(response))


def _remaining_millis(context: Any) -> float:
    """Return the Lambda's remaining execution time in milliseconds.

    Falls back to a large value when the context does not expose the timing
    helper (e.g., in local tests) so processing is not artificially cut short.

    Args:
        context: The Lambda context object.

    Returns:
        Remaining execution time in milliseconds.
    """
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if callable(getter):
        try:
            return float(getter())
        except Exception:  # noqa: BLE001 — be defensive about context shims.
            return float("inf")
    return float("inf")


def lambda_handler(event: dict, context: Any) -> dict:
    """Cron Lambda entry point invoked by EventBridge Scheduler.

    Parses the scheduler payload, invokes the AgentCore Runtime per target chat
    id, and delivers any user-facing responses via Telegram, all within the
    120-second Lambda budget (Req 11.2, 11.3, 11.5, 11.6).

    Args:
        event: The EventBridge Scheduler payload (``task_type``, ``chat_ids``).
        context: The Lambda context object.

    Returns:
        A summary dict ``{"task_type", "processed", "delivered", "failed",
        "skipped"}`` describing the cycle outcome.
    """
    runtime_arn = os.environ.get(RUNTIME_ARN_ENV)
    secret_arn = os.environ.get(BOT_TOKEN_SECRET_ARN_ENV)
    task_type, chat_ids = _parse_event(event)

    summary = {
        "task_type": task_type,
        "processed": 0,
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
    }

    if not runtime_arn or not secret_arn:
        # Log only the NAMES of the env vars that are unset — never the values
        # (or anything derived from them), so no sensitive-named value reaches
        # the log sink (CodeQL py/clear-text-logging-sensitive-data).
        missing = [
            name
            for name, is_set in (
                (RUNTIME_ARN_ENV, bool(runtime_arn)),
                (BOT_TOKEN_SECRET_ARN_ENV, bool(secret_arn)),
            )
            if not is_set
        ]
        logger.error("Missing required configuration (unset): %s", ", ".join(missing))
        return summary

    if not chat_ids:
        logger.info("No chat_ids in cron payload (task=%s); nothing to do", task_type)
        return summary

    # Fetch the bot token once per cycle (still re-fetched on each invocation so
    # rotations take effect — Req 11.6 of the Telegram client).
    try:
        bot_token = telegram_api.get_bot_token(secret_arn)
    except Exception as exc:  # noqa: BLE001 — without a token nothing can ship.
        logger.error("Failed to retrieve bot token for cron cycle: %s", exc)
        return summary

    for chat_id in chat_ids:
        # Stop launching new work if we are about to run out of time so that any
        # already-sent messages are not interrupted mid-delivery (Req 12.6).
        if _remaining_millis(context) < _TIME_BUDGET_FLOOR_MS:
            logger.warning(
                "Stopping cron cycle early to stay within timeout "
                "(task=%s, processed=%d/%d)",
                task_type,
                summary["processed"],
                len(chat_ids),
            )
            break

        summary["processed"] += 1
        message_text = _invoke_runtime(runtime_arn, chat_id, task_type)
        if message_text is None:
            summary["skipped"] += 1
            continue

        # send_long_message splits at the 4096-char limit and retries each chunk
        # up to 3 total attempts (Req 11.3 / 11.5).
        delivered = telegram_api.send_long_message(chat_id, message_text, bot_token)
        if delivered:
            summary["delivered"] += 1
        else:
            summary["failed"] += 1
            logger.error(
                "Telegram delivery failed after retries "
                "(chat_id=%s, task=%s); discarding message",
                chat_id,
                task_type,
            )

    logger.info("Cron cycle complete: %s", json.dumps(summary))
    return summary
