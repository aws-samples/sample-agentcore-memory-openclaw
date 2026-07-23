"""Webhook Lambda entry point for the Sprout Telegram assistant.

This module is the serverless front door between Telegram and the OpenClaw agent
running on Amazon Bedrock AgentCore Runtime. API Gateway (HTTP API, payload
format v2) proxies every Telegram webhook ``POST /webhook`` to
:func:`lambda_handler`, which implements the *Telegram Webhook* contract from the
design:

1. **Validate the webhook** (Req 2.4) — Telegram echoes the secret configured at
   ``setWebhook`` time in the ``X-Telegram-Bot-Api-Secret-Token`` header. The
   handler compares it (in constant time) against the bot token fetched from
   Secrets Manager and returns HTTP 401, discarding the request, when it does not
   match.
2. **Read the bot token** (Req 11.8) — the token is fetched fresh from Secrets
   Manager on every invocation (no value caching, so rotations take effect).
   A retrieval failure returns HTTP 500 and logs the error.
3. **Parse the Telegram Update** (Req 2.1) — extract the chat id, message text /
   caption, image attachments, and non-image document attachments.
4. **Download images** (Req 8.1, 8.7) — JPEG/PNG/GIF/WEBP attachments up to 20 MB
   are downloaded via the Telegram ``getFile`` API, base64-encoded, and attached
   to the invocation payload as ``images[]`` with a ``media_type`` so the agent
   can identify plants from photos. Other documents up to 20 MB are forwarded as
   ``attachments[]`` carrying the file URL (Req 2.5).
5. **Invoke AgentCore Runtime** (Req 2.1) — with a payload of
   ``{message, user_id=chat_id, session_id, invocation_type="webhook", images,
   attachments}`` under a 55-second timeout (Req 2.2).
6. **Reply to the user** (Req 2.3, 2.8) — the response text is split at
   paragraph/sentence boundaries when it exceeds Telegram's 4096-character limit
   and sent sequentially via :mod:`telegram_api`.
7. **Always acknowledge Telegram** — a runtime timeout (Req 2.2) or runtime error
   (Req 2.7) sends a user-friendly message to the chat and still returns HTTP 200
   so Telegram does not redeliver the update.

The Telegram API base URL is read from ``TELEGRAM_API_BASE`` (shared with
:mod:`telegram_api`) so tests can point the client at a local stub. The target
runtime ARN and bot-token secret ARN are read from ``AGENTCORE_RUNTIME_ARN`` and
``BOT_TOKEN_SECRET_ARN`` respectively, matching the environment variables the
CloudFormation template injects into the Webhook Lambda.

Dependency injection
--------------------
The boto3 Secrets Manager / AgentCore Runtime clients and the ``requests``
session are built lazily and cached for cold-start reuse, but every external
seam can be injected into :func:`lambda_handler` (``runtime_client``,
``secrets_client``, ``session``) so unit tests never touch AWS or the network.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import uuid
from typing import Any, Optional

import boto3
import requests
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError

import telegram_api

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Environment variable names (UPPER_SNAKE_CASE per project standards) -------
AGENTCORE_RUNTIME_ARN_ENV = "AGENTCORE_RUNTIME_ARN"
BOT_TOKEN_SECRET_ARN_ENV = "BOT_TOKEN_SECRET_ARN"  # nosec B105 - env var name, not a secret
# Dedicated webhook secret configured at setWebhook time and echoed back by
# Telegram in the secret-token header. It is a separate value from the bot token
# because Telegram restricts secret_token to [A-Za-z0-9_-] (the bot token
# contains a ':' and cannot be used). When unset (local/tests), validation falls
# back to comparing against the bot token.
WEBHOOK_SECRET_TOKEN_ENV = "WEBHOOK_SECRET_TOKEN"  # nosec B105 - env var name, not a secret
TELEGRAM_API_BASE_ENV = telegram_api.TELEGRAM_API_BASE_ENV
DEFAULT_TELEGRAM_API_BASE = telegram_api.DEFAULT_TELEGRAM_API_BASE

# --- Webhook validation -------------------------------------------------------
# Telegram echoes the secret configured via ``setWebhook`` in this header on
# every webhook delivery; we validate it against the bot token (Req 2.4). HTTP
# API v2 lowercases all header names.
SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"  # nosec B105 - HTTP header name, not a secret

# --- Attachment handling (Req 2.5, 8.7) ---------------------------------------
# Telegram's getFile/download path supports files up to 20 MB; we also cap our
# own handling at the same limit.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Image MIME types the agent can interpret as plant photos (Req 8.7).
IMAGE_MEDIA_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)
# Telegram ``photo`` sizes carry no MIME type; they are always JPEG.
DEFAULT_PHOTO_MEDIA_TYPE = "image/jpeg"

# --- AgentCore Runtime invocation (Req 2.1, 2.2) ------------------------------
# Wall-clock budget for the runtime call; a breach surfaces a timeout message to
# the chat (Req 2.2). Kept below the Lambda's 60s timeout and API Gateway limit.
RUNTIME_INVOCATION_TIMEOUT_SECONDS = 55.0
RUNTIME_CONNECT_TIMEOUT_SECONDS = 10.0
INVOCATION_TYPE_WEBHOOK = "webhook"
# AgentCore Runtime requires a runtimeSessionId of 33-128 characters; the
# ``webhook-`` prefix plus a 32-char uuid hex yields 40 characters.
_SESSION_ID_PREFIX = "webhook-"

# --- User-facing fallback messages (design Error Handling table) --------------
TIMEOUT_MESSAGE = "I'm taking longer than expected. Please try again."
RUNTIME_ERROR_MESSAGE = "I'm temporarily unavailable. Please try again in a moment."

# --- Per-request HTTP timeout for Telegram getFile/download calls --------------
TELEGRAM_FILE_TIMEOUT_SECONDS = 20.0

# --- Lazily-built, cold-start-reused clients (injectable seam) ----------------
_runtime_client = None
_session: Optional[requests.Session] = None


def _api_base() -> str:
    """Return the Telegram Bot API base URL (shared with :mod:`telegram_api`).

    Returns:
        The value of ``TELEGRAM_API_BASE`` with any trailing slash removed, or
        the public Telegram API base when the variable is unset.
    """
    return os.environ.get(TELEGRAM_API_BASE_ENV, DEFAULT_TELEGRAM_API_BASE).rstrip("/")


def _get_runtime_client():
    """Return the process-cached AgentCore Runtime data-plane client.

    The client is configured with a 55-second read timeout so a slow runtime
    invocation raises a timeout the handler can translate into a user-facing
    message (Req 2.2) rather than hanging until the Lambda is killed. Automatic
    retries are disabled so the single attempt honours the wall-clock budget.

    Returns:
        A boto3 ``bedrock-agentcore`` client.
    """
    global _runtime_client
    if _runtime_client is None:
        config = Config(
            connect_timeout=RUNTIME_CONNECT_TIMEOUT_SECONDS,
            read_timeout=RUNTIME_INVOCATION_TIMEOUT_SECONDS,
            retries={"max_attempts": 0},
        )
        _runtime_client = boto3.client("bedrock-agentcore", config=config)
    return _runtime_client


def _get_session() -> requests.Session:
    """Return the process-cached ``requests`` session for Telegram file calls.

    Returns:
        A shared :class:`requests.Session` reused across invocations.
    """
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _required_env(name: str) -> str:
    """Return a required environment variable or raise a descriptive error.

    Args:
        name: The environment variable name to read.

    Returns:
        The environment variable value.

    Raises:
        RuntimeError: If the variable is unset or empty, so misconfiguration
            surfaces clearly rather than as an opaque failure.
    """
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


def _response(status_code: int, body: str = "") -> dict[str, Any]:
    """Build an API Gateway HTTP API v2 proxy response.

    Args:
        status_code: The HTTP status code to return to API Gateway / Telegram.
        body: An optional response body (Telegram ignores the body content).

    Returns:
        A response dict in the HTTP API v2 proxy format.
    """
    return {"statusCode": status_code, "body": body}


def _lower_headers(event: dict) -> dict[str, str]:
    """Return the request headers with lowercased keys.

    HTTP API v2 already lowercases header names, but normalizing defensively
    keeps the secret-token lookup robust against direct/test invocations.

    Args:
        event: The API Gateway HTTP API v2 event.

    Returns:
        A dict mapping lowercased header names to their values (empty when the
        event carries no headers).
    """
    headers = event.get("headers") or {}
    return {str(k).lower(): v for k, v in headers.items()}


def _parse_body(event: dict) -> Optional[dict]:
    """Parse the JSON request body from an API Gateway HTTP API v2 event.

    Handles base64-encoded bodies (``isBase64Encoded``) and returns ``None`` when
    the body is absent or not valid JSON so the caller can acknowledge Telegram
    without processing.

    Args:
        event: The API Gateway HTTP API v2 event.

    Returns:
        The decoded Telegram Update object, or ``None`` when the body is missing
        or unparseable.
    """
    raw = event.get("body")
    if raw is None:
        return None
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            logger.warning("Failed to base64-decode webhook body")
            return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Webhook body is not valid JSON")
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_valid_secret_token(headers: dict[str, str], expected_secret: str) -> bool:
    """Validate the Telegram webhook secret-token header (Req 2.4).

    Telegram sends the secret configured at ``setWebhook`` time back in the
    ``X-Telegram-Bot-Api-Secret-Token`` header. Sprout configures that secret to
    a dedicated random value (see ``WEBHOOK_SECRET_TOKEN``), so a constant-time
    comparison against it both authenticates the caller and rejects spoofed
    requests.

    Args:
        headers: The request headers with lowercased keys.
        expected_secret: The expected secret-token value the header must match.

    Returns:
        ``True`` when the header is present and matches ``expected_secret``,
        ``False`` otherwise.
    """
    provided = headers.get(SECRET_TOKEN_HEADER)
    if not provided:
        return False
    return hmac.compare_digest(str(provided), expected_secret)


def _get_message(update: dict) -> Optional[dict]:
    """Return the message object from a Telegram Update.

    Telegram delivers user content under several keys depending on the update
    kind; this prefers a fresh ``message`` and falls back to edits and channel
    posts.

    Args:
        update: The parsed Telegram Update object.

    Returns:
        The message dict, or ``None`` when the update carries no message
        (e.g. a callback query, which Sprout ignores).
    """
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def _extract_chat_id(message: dict) -> Optional[str]:
    """Extract the chat id from a Telegram message.

    The chat id is used as the ``user_id``/``actorId`` for per-user memory
    isolation, so it is returned as a string for a stable namespace key.

    Args:
        message: The Telegram message object.

    Returns:
        The chat id as a string, or ``None`` when it is absent.
    """
    chat = message.get("chat")
    if isinstance(chat, dict) and "id" in chat:
        return str(chat["id"])
    return None


def _extract_text(message: dict) -> str:
    """Extract the user text from a Telegram message.

    Uses the message ``text`` for plain messages and falls back to the
    ``caption`` that accompanies a photo or document.

    Args:
        message: The Telegram message object.

    Returns:
        The message text or caption, or an empty string when neither is present.
    """
    text = message.get("text")
    if isinstance(text, str) and text:
        return text
    caption = message.get("caption")
    if isinstance(caption, str):
        return caption
    return ""


def _get_file_info(file_id: str, bot_token: str, session: requests.Session) -> Optional[dict]:
    """Resolve a Telegram ``file_id`` to its file metadata via ``getFile``.

    Args:
        file_id: The Telegram file identifier to resolve.
        bot_token: The bot token used to authenticate the request.
        session: The ``requests`` session used for the HTTP call.

    Returns:
        The ``result`` object from ``getFile`` (containing ``file_path`` and
        ``file_size``), or ``None`` when the lookup fails.
    """
    url = f"{_api_base()}/bot{bot_token}/getFile"
    try:
        response = session.post(
            url, json={"file_id": file_id}, timeout=TELEGRAM_FILE_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        logger.warning("Telegram getFile request failed: %s", exc)
        return None
    if response.status_code != 200:
        logger.warning("Telegram getFile returned HTTP %s", response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Telegram getFile returned non-JSON response")
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def _file_url(file_path: str, bot_token: str) -> str:
    """Build the Telegram file download URL for a resolved ``file_path``.

    Args:
        file_path: The ``file_path`` returned by ``getFile``.
        bot_token: The bot token used to authenticate the download.

    Returns:
        The fully-qualified download URL.
    """
    return f"{_api_base()}/file/bot{bot_token}/{file_path}"


def _download_file(file_path: str, bot_token: str, session: requests.Session) -> Optional[bytes]:
    """Download a Telegram file's bytes from its resolved ``file_path``.

    Args:
        file_path: The ``file_path`` returned by ``getFile``.
        bot_token: The bot token used to authenticate the download.
        session: The ``requests`` session used for the HTTP call.

    Returns:
        The raw file bytes, or ``None`` when the download fails or the file
        exceeds :data:`MAX_ATTACHMENT_BYTES`.
    """
    url = _file_url(file_path, bot_token)
    try:
        response = session.get(url, timeout=TELEGRAM_FILE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("Telegram file download failed: %s", exc)
        return None
    if response.status_code != 200:
        logger.warning("Telegram file download returned HTTP %s", response.status_code)
        return None
    content = response.content
    if len(content) > MAX_ATTACHMENT_BYTES:
        logger.warning(
            "Telegram file exceeds %d bytes; skipping", MAX_ATTACHMENT_BYTES
        )
        return None
    return content


def _largest_photo(photos: list) -> Optional[dict]:
    """Return the highest-resolution ``PhotoSize`` within the size limit.

    Telegram sends a ``photo`` as an array of progressively larger renditions;
    the largest rendition that still fits within :data:`MAX_ATTACHMENT_BYTES` is
    preferred for the best plant-identification fidelity.

    Args:
        photos: The Telegram ``photo`` array of ``PhotoSize`` objects.

    Returns:
        The chosen ``PhotoSize`` dict, or ``None`` when the array is empty.
    """
    candidates = [p for p in photos if isinstance(p, dict) and "file_id" in p]
    if not candidates:
        return None
    within_limit = [
        p for p in candidates if (p.get("file_size") or 0) <= MAX_ATTACHMENT_BYTES
    ]
    pool = within_limit or candidates
    # PhotoSize objects are ordered smallest-first; prefer the largest by area,
    # falling back to file_size when width/height are unavailable.
    return max(
        pool,
        key=lambda p: (
            (p.get("width") or 0) * (p.get("height") or 0),
            p.get("file_size") or 0,
        ),
    )


def _collect_attachments(
    message: dict, bot_token: str, session: requests.Session
) -> tuple[list[dict], list[dict]]:
    """Collect base64 image payloads and forwarded file attachments (Req 2.5, 8.7).

    Photos and image documents (JPEG/PNG/GIF/WEBP) up to 20 MB are downloaded and
    base64-encoded into ``images[]`` so the agent can identify plants from the
    pixels. Non-image documents up to 20 MB are forwarded as ``attachments[]``
    carrying the resolved file URL so the runtime can fetch them on demand.

    Args:
        message: The Telegram message object.
        bot_token: The bot token used for ``getFile``/download calls.
        session: The ``requests`` session used for the HTTP calls.

    Returns:
        A ``(images, attachments)`` tuple. ``images`` entries are
        ``{"data": base64_str, "media_type": str}``; ``attachments`` entries are
        ``{"url": str, "type": str, "size": int}``.
    """
    images: list[dict] = []
    attachments: list[dict] = []

    photos = message.get("photo")
    if isinstance(photos, list):
        chosen = _largest_photo(photos)
        if chosen is not None:
            image = _build_image_payload(
                chosen["file_id"], DEFAULT_PHOTO_MEDIA_TYPE, bot_token, session
            )
            if image is not None:
                images.append(image)

    document = message.get("document")
    if isinstance(document, dict) and "file_id" in document:
        size = document.get("file_size") or 0
        if size and size > MAX_ATTACHMENT_BYTES:
            logger.info("Skipping document over 20 MB (size=%s)", size)
        else:
            mime_type = document.get("mime_type") or "application/octet-stream"
            if mime_type in IMAGE_MEDIA_TYPES:
                image = _build_image_payload(
                    document["file_id"], mime_type, bot_token, session
                )
                if image is not None:
                    images.append(image)
            else:
                attachment = _build_attachment_payload(
                    document["file_id"], mime_type, size, bot_token, session
                )
                if attachment is not None:
                    attachments.append(attachment)

    return images, attachments


def _build_image_payload(
    file_id: str, media_type: str, bot_token: str, session: requests.Session
) -> Optional[dict]:
    """Download an image attachment and base64-encode it for the payload.

    Args:
        file_id: The Telegram file identifier of the image.
        media_type: The image MIME type to record alongside the bytes.
        bot_token: The bot token used for ``getFile``/download calls.
        session: The ``requests`` session used for the HTTP calls.

    Returns:
        An ``{"data": base64_str, "media_type": str}`` dict, or ``None`` when the
        image could not be resolved or downloaded within the size limit.
    """
    info = _get_file_info(file_id, bot_token, session)
    if not info or "file_path" not in info:
        return None
    if (info.get("file_size") or 0) > MAX_ATTACHMENT_BYTES:
        logger.info("Skipping image over 20 MB")
        return None
    data = _download_file(info["file_path"], bot_token, session)
    if data is None:
        return None
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "media_type": media_type,
    }


def _build_attachment_payload(
    file_id: str,
    file_type: str,
    size: int,
    bot_token: str,
    session: requests.Session,
) -> Optional[dict]:
    """Resolve a non-image document into a forwarded file-URL attachment (Req 2.5).

    Args:
        file_id: The Telegram file identifier of the document.
        file_type: The document MIME type.
        size: The document size in bytes (``0`` when Telegram omits it).
        bot_token: The bot token used for the ``getFile`` call.
        session: The ``requests`` session used for the HTTP call.

    Returns:
        A ``{"url": str, "type": str, "size": int}`` dict, or ``None`` when the
        file URL could not be resolved.
    """
    info = _get_file_info(file_id, bot_token, session)
    if not info or "file_path" not in info:
        return None
    return {
        "url": _file_url(info["file_path"], bot_token),
        "type": file_type,
        "size": size or info.get("file_size") or 0,
    }


def _new_session_id() -> str:
    """Generate a runtime/memory session id satisfying AgentCore's length rule.

    Returns:
        A unique session id of the form ``webhook-<uuid4 hex>`` (40 characters),
        comfortably within the AgentCore Runtime 33-128 character requirement.
    """
    return f"{_SESSION_ID_PREFIX}{uuid.uuid4().hex}"


def _extract_response_text(raw_body: bytes) -> str:
    """Extract the agent's reply text from the runtime response body.

    The container's ``server.py`` returns a JSON envelope
    ``{"ok": bool, "text": str, ...}``. This decodes that envelope and returns
    the ``text`` field, falling back to the raw decoded body when the response is
    not the expected envelope so a plain-text runtime still produces a reply.

    Args:
        raw_body: The bytes read from the runtime invocation response stream.

    Returns:
        The reply text to send to the user (empty string when none is present).
    """
    if not raw_body:
        return ""
    decoded = raw_body.decode("utf-8", errors="replace")
    try:
        envelope = json.loads(decoded)
    except (ValueError, TypeError):
        return decoded
    if isinstance(envelope, dict):
        text = envelope.get("text")
        if isinstance(text, str):
            return text
        return ""
    return decoded


def _read_runtime_payload(response: dict) -> bytes:
    """Read the response payload bytes from an ``invoke_agent_runtime`` result.

    Args:
        response: The dict returned by ``invoke_agent_runtime``; its ``response``
            entry is a streaming body for JSON content types.

    Returns:
        The full response body as bytes (empty when no body is present).
    """
    body = response.get("response")
    if body is None:
        return b""
    if hasattr(body, "read"):
        return body.read()
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    # Event-stream style iterables: concatenate any chunk bytes.
    try:
        return b"".join(bytes(chunk) for chunk in body)
    except TypeError:
        return b""


def _invoke_runtime(
    runtime_client,
    runtime_arn: str,
    payload: dict,
    session_id: str,
) -> str:
    """Invoke the AgentCore Runtime and return the agent's reply text.

    Args:
        runtime_client: The boto3 ``bedrock-agentcore`` client.
        runtime_arn: The target AgentCore Runtime ARN.
        payload: The invocation payload (message, user_id, images, ...).
        session_id: The runtime session id (used as ``runtimeSessionId``).

    Returns:
        The agent reply text extracted from the runtime response.

    Raises:
        botocore.exceptions.ReadTimeoutError: If the runtime call exceeds the
            configured read timeout (translated to a timeout message by the
            caller, Req 2.2).
        botocore.exceptions.ClientError: If the runtime returns an error.
        botocore.exceptions.BotoCoreError: For other client-side failures.
    """
    response = runtime_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload).encode("utf-8"),
    )
    return _extract_response_text(_read_runtime_payload(response))


def lambda_handler(
    event: dict,
    context: Any = None,
    *,
    runtime_client=None,
    secrets_client=None,
    session: Optional[requests.Session] = None,
) -> dict[str, Any]:
    """Handle a Telegram webhook delivery from API Gateway (HTTP API v2).

    Implements the full webhook contract documented in the module docstring:
    validate the secret-token header (Req 2.4), parse the Telegram Update
    (Req 2.1), download image/document attachments (Req 2.5, 8.1, 8.7), invoke
    AgentCore Runtime under a 55-second budget (Req 2.1, 2.2), and reply to the
    chat with the (possibly split) response (Req 2.3, 2.8) — always returning
    HTTP 200 to Telegram for application-level outcomes (Req 2.7), HTTP 401 for an
    invalid secret token (Req 2.4), and HTTP 500 only for a bot-token retrieval
    failure (Req 11.8).

    Args:
        event: The API Gateway HTTP API v2 proxy event.
        context: The Lambda context object (unused).
        runtime_client: An optional injected ``bedrock-agentcore`` client.
        secrets_client: An optional injected Secrets Manager client.
        session: An optional injected ``requests.Session``.

    Returns:
        An HTTP API v2 proxy response dict with the appropriate ``statusCode``.
    """
    secret_arn = _required_env(BOT_TOKEN_SECRET_ARN_ENV)
    runtime_arn = _required_env(AGENTCORE_RUNTIME_ARN_ENV)
    http_session = session if session is not None else _get_session()

    # 1. Fetch the bot token; a retrieval failure is an infrastructure error and
    #    returns HTTP 500 (Req 11.8).
    try:
        bot_token = telegram_api.get_bot_token(secret_arn, secrets_client=secrets_client)
    except (ClientError, BotoCoreError, KeyError, ValueError) as exc:
        logger.error("Failed to retrieve bot token from Secrets Manager: %s", exc)
        return _response(500, "secret retrieval failure")

    # 2. Validate the webhook secret-token header; reject spoofed callers (Req 2.4).
    #    The expected value is the dedicated WEBHOOK_SECRET_TOKEN when configured,
    #    falling back to the bot token for local/test use.
    expected_secret = os.environ.get(WEBHOOK_SECRET_TOKEN_ENV) or bot_token
    headers = _lower_headers(event)
    if not _is_valid_secret_token(headers, expected_secret):
        logger.warning("Rejected webhook with missing/invalid secret token")
        return _response(401, "unauthorized")

    # 3. Parse the Telegram Update; acknowledge non-message updates with 200.
    update = _parse_body(event)
    if update is None:
        return _response(200)
    message = _get_message(update)
    if message is None:
        logger.info("Webhook update carried no message; acknowledging")
        return _response(200)

    chat_id = _extract_chat_id(message)
    if chat_id is None:
        logger.warning("Message has no chat id; acknowledging without processing")
        return _response(200)

    text = _extract_text(message)
    images, attachments = _collect_attachments(message, bot_token, http_session)

    # Extract reply-to context: when the user replies to a previous message,
    # Telegram includes the original text in reply_to_message. Prepending it
    # gives the model conversational context even without full chat history.
    reply_context = ""
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict):
        reply_text = reply_to.get("text") or reply_to.get("caption") or ""
        if reply_text:
            reply_context = f"[Replying to: {reply_text}]\n\n"

    if not text and not images and not attachments:
        logger.info("Message has no text or supported attachments; acknowledging")
        return _response(200)

    # 4. Invoke AgentCore Runtime with the assembled payload (Req 2.1).
    # Use a session id that is stable per chat (so the OpenClaw gateway keeps
    # conversational state across turns) but salted with the deploy version:
    # AgentCore pins a runtimeSessionId to the runtime version it was created
    # on, so a purely stable id would keep serving an old container after a
    # redeploy. Rotating the salt each deploy migrates the session to the newest
    # runtime version. AgentCore requires 33-128 chars, so we pad to the minimum.
    salt = os.environ.get("RUNTIME_SESSION_SALT", "")
    raw_session = f"{_SESSION_ID_PREFIX}{salt}-chat-{chat_id}"
    session_id = raw_session.ljust(33, "0")
    payload = {
        "message": f"{reply_context}{text}" if reply_context else text,
        "user_id": chat_id,
        "session_id": session_id,
        "invocation_type": INVOCATION_TYPE_WEBHOOK,
        "images": images,
        "attachments": attachments,
    }
    client = runtime_client if runtime_client is not None else _get_runtime_client()

    try:
        reply = _invoke_runtime(client, runtime_arn, payload, session_id)
    except ReadTimeoutError:
        # Runtime exceeded the 55s budget: tell the user and still ack Telegram (Req 2.2).
        logger.warning("AgentCore Runtime invocation timed out for chat %s", chat_id)
        telegram_api.send_message(chat_id, TIMEOUT_MESSAGE, bot_token, session=http_session)
        return _response(200)
    except (ClientError, BotoCoreError) as exc:
        # Runtime error/unreachable: user-friendly message, HTTP 200 (Req 2.7).
        logger.error("AgentCore Runtime invocation failed for chat %s: %s", chat_id, exc)
        telegram_api.send_message(
            chat_id, RUNTIME_ERROR_MESSAGE, bot_token, session=http_session
        )
        return _response(200)

    # 5. Deliver the reply, splitting at boundaries when over 4096 chars (Req 2.3, 2.8).
    if reply:
        telegram_api.send_long_message(chat_id, reply, bot_token, session=http_session)
    else:
        logger.info("Runtime returned an empty reply for chat %s", chat_id)

    # 6. Always acknowledge Telegram so the update is not redelivered.
    return _response(200)
