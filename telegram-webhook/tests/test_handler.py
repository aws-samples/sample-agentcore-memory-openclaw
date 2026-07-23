"""Unit tests for the Sprout Telegram Webhook Lambda (``handler.py``).

Covers the webhook contract exercised by :func:`handler.lambda_handler`:

* Secret-token validation — valid token proceeds (HTTP 200), invalid/missing
  token is rejected with HTTP 401 (Req 2.4).
* Telegram Update parsing — chat id and text are extracted and forwarded to the
  AgentCore Runtime invocation payload (Req 2.1).
* Runtime timeout handling — a ``ReadTimeoutError`` surfaces the user-friendly
  timeout message and still returns HTTP 200 (Req 2.2).
* Runtime error handling — a runtime ``ClientError`` surfaces the unavailable
  message and returns HTTP 200 to Telegram (Req 2.7).
* Attachment forwarding — a document <= 20 MB is resolved via ``getFile`` and
  forwarded as an ``attachments[]`` file URL (Req 2.5).
* Secret retrieval failure — a Secrets Manager error returns HTTP 500 (Req 11.8).

Every external seam (AgentCore Runtime client, Secrets Manager client, the
``requests`` session used for Telegram calls) is injected as a fake, so the
tests never touch AWS or the network.
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

# Make the Lambda modules importable without packaging them.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handler  # noqa: E402  (path setup must precede the import)
import telegram_api  # noqa: E402

BOT_TOKEN = "123456:TEST-BOT-TOKEN"  # nosec B105 - dummy test fixture, not a real token
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/sprout"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:111122223333:secret:bot-token"  # nosec B105 - dummy test ARN, not a secret
API_BASE = "https://api.telegram.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Set the environment variables the handler requires."""
    monkeypatch.setenv(handler.AGENTCORE_RUNTIME_ARN_ENV, RUNTIME_ARN)
    monkeypatch.setenv(handler.BOT_TOKEN_SECRET_ARN_ENV, SECRET_ARN)
    monkeypatch.setenv(telegram_api.TELEGRAM_API_BASE_ENV, API_BASE)


# --- Test doubles -------------------------------------------------------------
class FakeResponse:
    """Minimal ``requests``-style response."""

    def __init__(self, status_code=200, json_body=None, content=b""):
        self.status_code = status_code
        self._json = json_body
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """A ``requests.Session`` stand-in with URL-routed canned responses.

    ``post_router``/``get_router`` map a substring found in the URL to a
    :class:`FakeResponse`. All calls are recorded for assertions.
    """

    def __init__(self, post_router=None, get_router=None):
        self._post_router = post_router or {}
        self._get_router = get_router or {}
        self.post_calls = []
        self.get_calls = []

    def post(self, url, json=None, timeout=None):
        self.post_calls.append({"url": url, "json": json})
        for needle, response in self._post_router.items():
            if needle in url:
                return response
        return FakeResponse(200, {"ok": True, "result": {}})

    def get(self, url, timeout=None):
        self.get_calls.append({"url": url})
        for needle, response in self._get_router.items():
            if needle in url:
                return response
        return FakeResponse(200, content=b"")


class FakeSecretsClient:
    """Secrets Manager stand-in returning a fixed token or raising."""

    def __init__(self, token=BOT_TOKEN, error=None):
        self._token = token
        self._error = error

    def get_secret_value(self, SecretId=None):  # noqa: N803 (boto3 kwarg)
        if self._error is not None:
            raise self._error
        return {"SecretString": self._token}


class _StreamBody:
    """A minimal streaming body exposing ``read`` like the runtime response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class FakeRuntimeClient:
    """AgentCore Runtime stand-in.

    Returns a canned envelope, or raises the configured exception to simulate a
    timeout / runtime error. Records the last invocation kwargs for assertions.
    """

    def __init__(self, reply_text="Here is your gardening advice.", error=None):
        self._reply_text = reply_text
        self._error = error
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        envelope = json.dumps({"ok": True, "text": self._reply_text}).encode("utf-8")
        return {"response": _StreamBody(envelope)}


# --- Event builders -----------------------------------------------------------
def _make_event(update: dict, secret_token=BOT_TOKEN, is_base64=False) -> dict:
    """Build an API Gateway HTTP API v2 proxy event wrapping a Telegram Update."""
    body = json.dumps(update)
    if is_base64:
        body = base64.b64encode(body.encode("utf-8")).decode("ascii")
    headers = {}
    if secret_token is not None:
        headers[handler.SECRET_TOKEN_HEADER] = secret_token
    return {"headers": headers, "body": body, "isBase64Encoded": is_base64}


def _text_update(chat_id=42, text="How often should I water basil?") -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


# --- Webhook validation (Req 2.4) --------------------------------------------
def test_valid_secret_token_is_accepted_and_returns_200():
    runtime = FakeRuntimeClient()
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    result = handler.lambda_handler(
        _make_event(_text_update()),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert result["statusCode"] == 200
    assert len(runtime.calls) == 1


def test_invalid_secret_token_returns_401_and_does_not_invoke_runtime():
    runtime = FakeRuntimeClient()

    result = handler.lambda_handler(
        _make_event(_text_update(), secret_token="WRONG-TOKEN"),  # nosec B106 - dummy test value, not a secret
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=FakeSession(),
    )

    assert result["statusCode"] == 401
    assert runtime.calls == []


def test_missing_secret_token_returns_401():
    runtime = FakeRuntimeClient()

    result = handler.lambda_handler(
        _make_event(_text_update(), secret_token=None),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=FakeSession(),
    )

    assert result["statusCode"] == 401
    assert runtime.calls == []


# --- Message parsing (Req 2.1) -----------------------------------------------
def test_message_parsing_forwards_chat_id_and_text_to_runtime():
    runtime = FakeRuntimeClient()
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    handler.lambda_handler(
        _make_event(_text_update(chat_id=98765, text="My tomatoes have yellow leaves")),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert len(runtime.calls) == 1
    payload = json.loads(runtime.calls[0]["payload"].decode("utf-8"))
    assert payload["user_id"] == "98765"
    assert payload["message"] == "My tomatoes have yellow leaves"
    assert payload["invocation_type"] == handler.INVOCATION_TYPE_WEBHOOK
    assert payload["images"] == []
    assert payload["attachments"] == []


def test_base64_encoded_body_is_decoded_and_processed():
    runtime = FakeRuntimeClient()
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    result = handler.lambda_handler(
        _make_event(_text_update(text="hello"), is_base64=True),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert result["statusCode"] == 200
    assert len(runtime.calls) == 1


def test_reply_is_sent_back_to_the_chat():
    runtime = FakeRuntimeClient(reply_text="Water basil in the morning.")
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    handler.lambda_handler(
        _make_event(_text_update(chat_id=7)),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    send_calls = [c for c in session.post_calls if "/sendMessage" in c["url"]]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["chat_id"] == "7"
    assert send_calls[0]["json"]["text"] == "Water basil in the morning."


def test_update_without_message_is_acknowledged_without_invoking_runtime():
    runtime = FakeRuntimeClient()

    result = handler.lambda_handler(
        _make_event({"callback_query": {"id": "1"}}),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=FakeSession(),
    )

    assert result["statusCode"] == 200
    assert runtime.calls == []


# --- Timeout handling (Req 2.2) ----------------------------------------------
def test_runtime_timeout_sends_timeout_message_and_returns_200():
    runtime = FakeRuntimeClient(error=ReadTimeoutError(endpoint_url="https://runtime"))
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    result = handler.lambda_handler(
        _make_event(_text_update(chat_id=55)),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert result["statusCode"] == 200
    send_calls = [c for c in session.post_calls if "/sendMessage" in c["url"]]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["text"] == handler.TIMEOUT_MESSAGE


# --- Runtime error handling (Req 2.7) ----------------------------------------
def test_runtime_error_sends_unavailable_message_and_returns_200():
    client_error = ClientError(
        {"Error": {"Code": "ServiceException", "Message": "boom"}},
        "InvokeAgentRuntime",
    )
    runtime = FakeRuntimeClient(error=client_error)
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})

    result = handler.lambda_handler(
        _make_event(_text_update(chat_id=55)),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert result["statusCode"] == 200
    send_calls = [c for c in session.post_calls if "/sendMessage" in c["url"]]
    assert len(send_calls) == 1
    assert send_calls[0]["json"]["text"] == handler.RUNTIME_ERROR_MESSAGE


# --- Attachment forwarding (Req 2.5) -----------------------------------------
def test_document_attachment_under_limit_is_forwarded_as_file_url():
    runtime = FakeRuntimeClient()
    get_file_response = FakeResponse(
        200, {"ok": True, "result": {"file_path": "documents/care.pdf", "file_size": 1024}}
    )
    session = FakeSession(
        post_router={
            "/getFile": get_file_response,
            "/sendMessage": FakeResponse(200, {"ok": True}),
        }
    )
    update = {
        "message": {
            "chat": {"id": 314},
            "caption": "Here is my care sheet",
            "document": {
                "file_id": "DOC123",
                "file_size": 1024,
                "mime_type": "application/pdf",
            },
        }
    }

    handler.lambda_handler(
        _make_event(update),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    assert len(runtime.calls) == 1
    payload = json.loads(runtime.calls[0]["payload"].decode("utf-8"))
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["type"] == "application/pdf"
    assert attachment["size"] == 1024
    assert "documents/care.pdf" in attachment["url"]


def test_oversized_document_is_skipped():
    runtime = FakeRuntimeClient()
    session = FakeSession(post_router={"/sendMessage": FakeResponse(200, {"ok": True})})
    update = {
        "message": {
            "chat": {"id": 314},
            "caption": "big file",
            "document": {
                "file_id": "BIG",
                "file_size": handler.MAX_ATTACHMENT_BYTES + 1,
                "mime_type": "application/pdf",
            },
        }
    }

    handler.lambda_handler(
        _make_event(update),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    payload = json.loads(runtime.calls[0]["payload"].decode("utf-8"))
    assert payload["attachments"] == []
    # No getFile lookup should have been attempted for the oversized document.
    assert not any("/getFile" in c["url"] for c in session.post_calls)


def test_image_photo_is_downloaded_and_forwarded_as_base64():
    runtime = FakeRuntimeClient()
    image_bytes = b"\xff\xd8\xff\xe0fakejpegbytes"
    session = FakeSession(
        post_router={
            "/getFile": FakeResponse(
                200, {"ok": True, "result": {"file_path": "photos/plant.jpg", "file_size": len(image_bytes)}}
            ),
            "/sendMessage": FakeResponse(200, {"ok": True}),
        },
        get_router={"photos/plant.jpg": FakeResponse(200, content=image_bytes)},
    )
    update = {
        "message": {
            "chat": {"id": 500},
            "caption": "What plant is this?",
            "photo": [
                {"file_id": "SMALL", "width": 90, "height": 90, "file_size": 900},
                {"file_id": "LARGE", "width": 800, "height": 600, "file_size": len(image_bytes)},
            ],
        }
    }

    handler.lambda_handler(
        _make_event(update),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(),
        session=session,
    )

    payload = json.loads(runtime.calls[0]["payload"].decode("utf-8"))
    assert len(payload["images"]) == 1
    image = payload["images"][0]
    assert image["media_type"] == handler.DEFAULT_PHOTO_MEDIA_TYPE
    assert base64.b64decode(image["data"]) == image_bytes


# --- Secret retrieval failure (Req 11.8) -------------------------------------
def test_secret_retrieval_failure_returns_500():
    error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
        "GetSecretValue",
    )
    runtime = FakeRuntimeClient()

    result = handler.lambda_handler(
        _make_event(_text_update()),
        runtime_client=runtime,
        secrets_client=FakeSecretsClient(error=error),
        session=FakeSession(),
    )

    assert result["statusCode"] == 500
    assert runtime.calls == []
