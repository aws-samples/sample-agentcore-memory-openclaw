"""Telegram Bot API client for the Sprout Webhook/Cron Lambdas.

This module is the thin integration layer between the Lambda handlers and the
Telegram Bot API. It owns three responsibilities described in the design's
*Telegram Webhook* section:

* **Bot token retrieval** — :func:`get_bot_token` reads the Telegram bot token
  from AWS Secrets Manager on every call, never caching the secret *value*, so a
  rotated secret is picked up on the next invocation (Req 11.6). The boto3
  client itself is cached for cold-start reuse — only the secret value is always
  re-fetched.
* **Message delivery with retries** — :func:`send_message` posts a single
  message to a chat and retries delivery up to 3 total attempts (the initial
  attempt plus 2 retries) with a 1-second delay between attempts before logging
  the failure and giving up (Req 2.8).
* **Long-message splitting** — :func:`split_message` splits text that exceeds
  Telegram's 4096-character per-message limit at paragraph boundaries first,
  then sentence boundaries, then whitespace, and finally a hard cut, so that no
  chunk exceeds the limit and the original content is preserved in order
  (Req 2.3). :func:`send_long_message` splits and sends the chunks sequentially.

The HTTP base URL is read from the ``TELEGRAM_API_BASE`` environment variable
(defaulting to ``https://api.telegram.org``) so tests can point the client at a
local stub.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

import boto3
import requests

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


# --- Markdown → Telegram HTML conversion --------------------------------------
# LLM output typically uses standard Markdown (e.g. **bold**, *italic*, `code`,
# ```code blocks```). Telegram's HTML parse_mode supports <b>, <i>, <code>, <pre>.
# We convert common patterns so formatting renders natively in the Telegram client.
# Unsupported/malformed markup passes through as-is with the plain-text fallback.

def _to_telegram_html(text: str) -> str:
    """Convert common Markdown patterns in LLM output to Telegram-safe HTML.

    Handles: **bold**, *italic*, `inline code`, ```code blocks```, and # headers.
    Escapes HTML entities first so raw < > & in the text don't break parsing.
    """
    # 1. Escape HTML entities (must come first).
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 2. Code blocks (``` ... ```) → <pre>...</pre>
    text = re.sub(
        r"```(?:\w*\n)?(.*?)```",
        lambda m: f"<pre>{m.group(1).strip()}</pre>",
        text,
        flags=re.DOTALL,
    )

    # 3. Inline code (`...`) → <code>...</code>
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

    # 4. Bold (**...**) → <b>...</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 5. Italic (*...*) → <i>...</i> (single asterisk, not inside bold)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)

    # 6. Headers (# ...) → <b>...</b> (Telegram has no heading tag)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    return text


# --- Telegram limits and delivery tuning --------------------------------------
# Telegram rejects messages longer than this many characters (Req 2.3).
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# Total number of delivery attempts: the initial send plus 2 retries (Req 2.8).
MAX_SEND_ATTEMPTS = 3
# Delay between delivery attempts, in seconds (Req 2.8).
RETRY_DELAY_SECONDS = 1.0
# Per-request HTTP timeout for Telegram Bot API calls, in seconds.
REQUEST_TIMEOUT_SECONDS = 10.0

# --- Configuration ------------------------------------------------------------
TELEGRAM_API_BASE_ENV = "TELEGRAM_API_BASE"
DEFAULT_TELEGRAM_API_BASE = "https://api.telegram.org"

# Sentence-ending punctuation followed by whitespace, used as a secondary split
# boundary when no paragraph break fits within the limit.
_SENTENCE_BOUNDARY = re.compile(r"[.!?]\s")
_PARAGRAPH_BOUNDARY = "\n\n"

# --- Cold-start-reused Secrets Manager client (the value is never cached) ------
_secrets_client = None


def _get_secrets_client():
    """Return the process-cached Secrets Manager client.

    Only the boto3 client is reused across invocations; the secret *value* is
    always re-fetched by :func:`get_bot_token` so rotations take effect on the
    next call (Req 11.6).

    Returns:
        A boto3 Secrets Manager client.
    """
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


def _api_base() -> str:
    """Return the Telegram Bot API base URL.

    Returns:
        The value of the ``TELEGRAM_API_BASE`` environment variable, or the
        public Telegram API base when unset.
    """
    return os.environ.get(TELEGRAM_API_BASE_ENV, DEFAULT_TELEGRAM_API_BASE).rstrip("/")


def get_bot_token(secret_arn: str, *, secrets_client=None) -> str:
    """Fetch the Telegram bot token from Secrets Manager without caching it.

    The secret value is retrieved on every call so that a rotated token is used
    on the next invocation rather than a stale cached value (Req 11.6). The
    stored secret may be either a plain string token or a JSON object; when it is
    JSON, the value under a ``token``/``bot_token``/``TelegramBotToken`` key is
    returned, falling back to the sole value when the object has a single entry.

    Args:
        secret_arn: The Secrets Manager ARN (or name) of the bot token secret.
        secrets_client: An optional injected Secrets Manager client for testing.

    Returns:
        The Telegram bot token string.

    Raises:
        KeyError: If the secret response carries no ``SecretString``.
        ValueError: If a JSON secret contains no recognizable token value.
    """
    client = secrets_client if secrets_client is not None else _get_secrets_client()
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string = response["SecretString"]

    # The secret is most commonly a plain token. Only treat it as JSON when it
    # parses into an object; a bare token is returned unchanged.
    try:
        parsed = json.loads(secret_string)
    except (ValueError, TypeError):
        return secret_string

    if not isinstance(parsed, dict):
        return secret_string

    for key in ("token", "bot_token", "TelegramBotToken"):
        if key in parsed:
            return str(parsed[key])
    if len(parsed) == 1:
        return str(next(iter(parsed.values())))
    raise ValueError("Bot token secret JSON contains no recognizable token value")


def split_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split ``text`` into chunks that each fit within ``max_length`` characters.

    Splitting prefers natural boundaries within the limit, in priority order:
    paragraph breaks (``\\n\\n``), then sentence boundaries (``.``/``!``/``?``
    followed by whitespace), then any whitespace, and finally a hard cut at
    ``max_length`` when no boundary is available. Whitespace at the split points
    is trimmed so the chunks read cleanly; the non-whitespace content is
    preserved in its original order (Req 2.3).

    Args:
        text: The message text to split.
        max_length: The maximum number of characters per chunk (default 4096).

    Returns:
        A list of chunks, each no longer than ``max_length``. An empty input
        yields an empty list, and an input already within the limit yields a
        single-element list containing the input unchanged.
    """
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        window = remaining[:max_length]
        split_at = _find_split_point(window, max_length)
        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _find_split_point(window: str, max_length: int) -> int:
    """Return the index within ``window`` at which to cut the next chunk.

    The window is the leading ``max_length`` characters of the remaining text.
    The function returns the position just after the best available boundary,
    guaranteeing a value in ``1..max_length`` so that splitting always makes
    progress.

    Args:
        window: The leading slice of the remaining text (length ``max_length``).
        max_length: The maximum chunk length, used as the hard-cut fallback.

    Returns:
        The cut index (number of characters to take for this chunk).
    """
    # 1. Paragraph boundary: cut just after the last blank line within the window.
    paragraph_idx = window.rfind(_PARAGRAPH_BOUNDARY)
    if paragraph_idx > 0:
        return paragraph_idx + len(_PARAGRAPH_BOUNDARY)

    # 2. Sentence boundary: cut just after the last sentence terminator.
    sentence_end = -1
    for match in _SENTENCE_BOUNDARY.finditer(window):
        sentence_end = match.end()
    if sentence_end > 0:
        return sentence_end

    # 3. Whitespace boundary: cut just after the last space or newline.
    whitespace_idx = max(window.rfind(" "), window.rfind("\n"))
    if whitespace_idx > 0:
        return whitespace_idx + 1

    # 4. No boundary available: hard cut at the limit.
    return max_length


def send_message(
    chat_id: int | str,
    text: str,
    bot_token: str,
    *,
    session: Optional[requests.Session] = None,
) -> bool:
    """Send a single message to a Telegram chat, retrying on failure.

    Posts to the Telegram ``sendMessage`` method and retries up to
    :data:`MAX_SEND_ATTEMPTS` total attempts (the initial send plus 2 retries)
    with a :data:`RETRY_DELAY_SECONDS` delay between attempts. After the final
    attempt fails the failure is logged and ``False`` is returned rather than
    raised, so callers can continue (Req 2.8).

    Args:
        chat_id: The target Telegram chat identifier.
        text: The message text (assumed to be within Telegram's length limit;
            use :func:`send_long_message` for arbitrary-length text).
        bot_token: The Telegram bot token used to authenticate the request.
        session: An optional ``requests.Session`` for connection reuse/testing.

    Returns:
        ``True`` if the message was delivered, ``False`` if every attempt failed.
    """
    url = f"{_api_base()}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": _to_telegram_html(text), "parse_mode": "HTML"}
    http = session if session is not None else requests

    last_error: Optional[str] = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = http.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 200:
                return True
            # Telegram returns 400 when the HTML is malformed. Fall back to plain
            # text so the user still receives the content rather than nothing.
            if response.status_code == 400 and "parse_mode" in payload:
                logger.info("HTML rejected by Telegram; retrying as plain text")
                payload.pop("parse_mode", None)
                payload["text"] = text  # original, unformatted
                response = http.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
                if response.status_code == 200:
                    return True
            last_error = f"HTTP {response.status_code}"
            logger.warning(
                "Telegram sendMessage failed (attempt %d/%d): %s",
                attempt,
                MAX_SEND_ATTEMPTS,
                last_error,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            logger.warning(
                "Telegram sendMessage error (attempt %d/%d): %s",
                attempt,
                MAX_SEND_ATTEMPTS,
                last_error,
            )

        if attempt < MAX_SEND_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(
        "Telegram sendMessage gave up after %d attempts (chat_id=%s): %s",
        MAX_SEND_ATTEMPTS,
        chat_id,
        last_error,
    )
    return False


def send_long_message(
    chat_id: int | str,
    text: str,
    bot_token: str,
    *,
    session: Optional[requests.Session] = None,
) -> bool:
    """Split ``text`` if needed and send the chunks sequentially in order.

    The text is split via :func:`split_message` so each chunk fits within
    Telegram's 4096-character limit, then each chunk is delivered with
    :func:`send_message` in order (Req 2.3). Delivery stops at the first chunk
    that cannot be delivered after its retries.

    Args:
        chat_id: The target Telegram chat identifier.
        text: The full message text, which may exceed the per-message limit.
        bot_token: The Telegram bot token used to authenticate the requests.
        session: An optional ``requests.Session`` for connection reuse/testing.

    Returns:
        ``True`` if every chunk was delivered, ``False`` if any chunk failed.
    """
    chunks = split_message(text)
    if not chunks:
        return True

    for chunk in chunks:
        if not send_message(chat_id, chunk, bot_token, session=session):
            return False
    return True
