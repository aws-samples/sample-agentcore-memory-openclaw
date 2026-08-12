"""Unit tests for the AgentCore Runtime HTTP server (``agent-container/server.py``).

Feature: openclaw-telegram-agentcore, task 7.3.

These tests exercise the runtime shell and its collaborators with injected fakes
(no live AWS / OpenClaw), covering the scenarios required by the task:

* ``GET /ping`` returns ``200 {"status": "Healthy"}`` (Req 3.7).
* ``POST /invocations`` full lifecycle: retrieve -> process -> persist (Req 3.2, 3.3).
* Memory retrieval timeout / error degrades to no context (Req 5.5).
* ``CreateEvent`` failure is logged and non-fatal (Req 4.4, 4.6).
* Workspace download/upload failure degrades gracefully (Req 9.4, 9.5).
* Invalid / missing ``user_id`` is rejected with ``INVALID_USER_ID`` (Req 3.2).

boto3 clients (AgentCore Memory, S3) and the OpenClaw agent are all injected as
fakes/mocks via the constructor ``client=`` params and ``SproutRuntime``'s
injectable collaborators.

Validates: Requirements 3.2, 3.3, 3.7, 4.4, 4.6, 5.5, 8.4, 8.5
"""

from __future__ import annotations

import io
import json
import os
import sys
import time

import pytest

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402  (path setup must precede the import)
from server import (  # noqa: E402
    Confidence,
    MemoryContextRecord,
    OpenClawAgent,
    SproutMemory,
    SproutRuntime,
    SproutRuntimeHandler,
    WorkspaceStore,
)


# =============================================================================
# Fakes / test doubles
# =============================================================================
class FakeMemory:
    """Fake ``SproutMemory`` recording retrieve/persist calls in order."""

    def __init__(self, records=None, *, persist_result=True):
        self._records = records or []
        self._persist_result = persist_result
        self.calls: list[str] = []
        self.retrieve_args = None
        self.persist_args = None

    def retrieve(self, chat_id, query):
        self.calls.append("retrieve")
        self.retrieve_args = (chat_id, query)
        return list(self._records)

    def persist(self, chat_id, session_id, messages, metadata=None):
        self.calls.append("persist")
        self.persist_args = (chat_id, session_id, messages)
        return self._persist_result


class FakeWorkspace:
    """Fake ``WorkspaceStore`` recording download/upload calls."""

    def __init__(self, *, download_result=True, upload_result=True):
        self._download_result = download_result
        self._upload_result = upload_result
        self.calls: list[str] = []

    def download(self, chat_id, dest_dir):
        self.calls.append("download")
        return self._download_result

    def upload(self, chat_id, source_dir):
        self.calls.append("upload")
        return self._upload_result


class FakeAgent:
    """Fake ``OpenClawAgent`` returning a canned response or raising."""

    def __init__(self, response="Here is your gardening advice.", *, exc=None):
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "message": message,
                "images": images,
                "workspace_dir": workspace_dir,
            }
        )
        if self._exc is not None:
            raise self._exc
        return self._response


def _make_runtime(memory=None, workspace=None, agent=None):
    """Assemble a SproutRuntime from fakes with sensible defaults."""
    return SproutRuntime(
        memory=memory or FakeMemory(),
        workspace=workspace or FakeWorkspace(),
        agent=agent or FakeAgent(),
        base_persona="You are Sprout.",
        model_id="us.anthropic.claude-haiku-4-5-v1",
    )


# =============================================================================
# GET /ping health check (Req 3.7)
# =============================================================================
class _RecordingHandler(SproutRuntimeHandler):
    """A handler instance that bypasses socket setup and records the response.

    ``BaseHTTPRequestHandler.__init__`` would immediately try to parse a request
    off a socket, so we construct the instance without calling it and stub the
    header-writing plumbing, capturing the status code and body written to a
    fake ``wfile``.
    """

    def __init__(self):  # noqa: D107 - deliberately skips super().__init__
        self.wfile = io.BytesIO()
        self.status = None
        self.headers_sent: list[tuple[str, str]] = []

    def send_response(self, code, message=None):  # noqa: D102
        self.status = code

    def send_header(self, key, value):  # noqa: D102
        self.headers_sent.append((key, value))

    def end_headers(self):  # noqa: D102
        pass

    def body_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def test_ping_returns_200_healthy():
    """GET /ping returns HTTP 200 with a healthy status body (Req 3.7)."""
    handler = _RecordingHandler()
    handler.path = "/ping"

    handler.do_GET()

    assert handler.status == 200
    assert handler.body_json() == {"status": "Healthy"}
    assert ("Content-Type", "application/json") in handler.headers_sent


def test_ping_unknown_path_returns_404():
    """An unknown GET path returns a 404 error envelope."""
    handler = _RecordingHandler()
    handler.path = "/does-not-exist"

    handler.do_GET()

    assert handler.status == 404
    assert handler.body_json()["error"] == "NOT_FOUND"


# =============================================================================
# POST /invocations full lifecycle: retrieve -> process -> persist (Req 3.2, 3.3)
# =============================================================================
def test_handle_invocation_full_lifecycle_order_and_envelope():
    """Full lifecycle runs download->retrieve->process->persist->upload (Req 3.3)."""
    memory = FakeMemory(
        records=[MemoryContextRecord("Grows basil", Confidence.EXPLICIT, topic="plants")]
    )
    workspace = FakeWorkspace()
    agent = FakeAgent(response="Water your basil in the morning.")
    runtime = _make_runtime(memory=memory, workspace=workspace, agent=agent)

    result = runtime.handle_invocation(
        {"user_id": "12345", "message": "How do I care for my basil?", "session_id": "sess-1"}
    )

    # Success envelope shape (Req 3.2).
    assert result["ok"] is True
    assert result["text"] == "Water your basil in the morning."
    assert result["metadata"]["model_id"] == "us.anthropic.claude-haiku-4-5-v1"
    assert result["metadata"]["memory_records_retrieved"] == 1
    assert result["metadata"]["session_id"] == "sess-1"
    assert result["metadata"]["transcript_persisted"] is True

    # Retrieve used the user's message as the query, keyed by chat id.
    assert memory.retrieve_args == ("12345", "How do I care for my basil?")

    # Lifecycle order: retrieve before persist; download before upload.
    assert memory.calls == ["retrieve", "persist"]
    assert workspace.calls == ["download", "upload"]

    # The persisted transcript carries the user turn then the assistant turn.
    _, session_id, messages = memory.persist_args
    assert session_id == "sess-1"
    assert messages[0] == {"role": "USER", "content": "How do I care for my basil?"}
    assert messages[1] == {
        "role": "ASSISTANT",
        "content": "Water your basil in the morning.",
    }


def test_handle_invocation_injects_memory_context_into_system_prompt():
    """Retrieved memories are assembled into the system prompt passed to the agent."""
    memory = FakeMemory(
        records=[MemoryContextRecord("Lives in zone 9b", Confidence.EXPLICIT, topic="zone")]
    )
    agent = FakeAgent()
    runtime = _make_runtime(memory=memory, agent=agent)

    runtime.handle_invocation({"user_id": "42", "message": "hi"})

    system_prompt = agent.calls[0]["system_prompt"]
    assert "You are Sprout." in system_prompt
    assert "Lives in zone 9b" in system_prompt


def test_handle_invocation_passes_images_to_agent():
    """Image attachments are forwarded to the agent for plant identification (Req 8.4, 8.5)."""
    agent = FakeAgent()
    runtime = _make_runtime(agent=agent)
    images = [{"data": "Zm9v", "media_type": "image/jpeg"}]

    runtime.handle_invocation({"user_id": "7", "message": "what plant?", "images": images})

    assert agent.calls[0]["images"] == images


def test_handle_invocation_generates_session_id_when_missing():
    """A session id is generated when the payload omits one."""
    runtime = _make_runtime()

    result = runtime.handle_invocation({"user_id": "9", "message": "hello"})

    assert result["metadata"]["session_id"]  # non-empty generated id


# =============================================================================
# Invalid / missing user_id rejection (Req 3.2)
# =============================================================================
@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hi"},  # missing user_id
        {"user_id": "", "message": "hi"},  # blank
        {"user_id": "   ", "message": "hi"},  # whitespace only
        {"user_id": 12345, "message": "hi"},  # non-string
    ],
)
def test_handle_invocation_rejects_invalid_user_id(payload):
    """Missing/blank/non-string user_id is rejected before any work (Req 3.2)."""
    memory = FakeMemory()
    workspace = FakeWorkspace()
    agent = FakeAgent()
    runtime = _make_runtime(memory=memory, workspace=workspace, agent=agent)

    result = runtime.handle_invocation(payload)

    assert result == {
        "ok": False,
        "error": "INVALID_USER_ID",
        "message": "A non-empty user_id is required.",
    }
    # No collaborator should have been touched on rejection.
    assert memory.calls == []
    assert workspace.calls == []
    assert agent.calls == []


# =============================================================================
# Path-injection defenses (CWE-22 / CodeQL py/path-injection)
# =============================================================================
@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "12345/../../secret", "a b", "foo", "12/34", "..", "12345\x00"],
)
def test_handle_invocation_rejects_traversal_user_id(bad_id):
    """A non-numeric / path-traversal user_id is rejected (never reaches disk)."""
    memory = FakeMemory()
    workspace = FakeWorkspace()
    agent = FakeAgent()
    runtime = _make_runtime(memory=memory, workspace=workspace, agent=agent)

    result = runtime.handle_invocation({"user_id": bad_id, "message": "hi"})

    assert result["ok"] is False
    assert result["error"] == "INVALID_USER_ID"
    # Rejected before any workspace/memory/agent work.
    assert workspace.calls == []
    assert memory.calls == []
    assert agent.calls == []


def test_is_valid_chat_id_allows_only_integers():
    assert server.is_valid_chat_id("12345")
    assert server.is_valid_chat_id("-1001234567890")  # Telegram group id
    assert not server.is_valid_chat_id("../../etc")
    assert not server.is_valid_chat_id("12345/6")
    assert not server.is_valid_chat_id("")
    assert not server.is_valid_chat_id("abc")


def test_resolve_within_blocks_escape(tmp_path):
    base = str(tmp_path)
    # A benign relative path resolves inside base.
    inside = server.resolve_within(base, "sub", "file.txt")
    assert inside.startswith(os.path.realpath(base) + os.sep)
    # Traversal escapes are rejected.
    for evil in ("../outside", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(ValueError):
            server.resolve_within(base, evil)


def test_workspace_download_skips_unsafe_s3_key():
    """A crafted S3 object key containing '..' is skipped, not written outside."""
    class _EvilKeyClient:
        def get_paginator(self, _op):
            class _P:
                def paginate(self, **_kw):
                    return [{"Contents": [{"Key": "workspace/12345/../../escape.txt"}]}]
            return _P()

        def download_file(self, *_a, **_k):  # pragma: no cover - must not run
            raise AssertionError("download_file called for an unsafe key")

    store = WorkspaceStore(bucket="b", client=_EvilKeyClient())
    # Should return True (graceful) and NOT call download_file for the bad key.
    assert store.download("12345", "/tmp/sprout-cwe22-test") is True


# =============================================================================
# Agent error handling -> AGENT_ERROR envelope
# =============================================================================
def test_handle_invocation_agent_exception_returns_agent_error():
    """An agent failure is surfaced as an AGENT_ERROR envelope, not a crash."""
    agent = FakeAgent(exc=RuntimeError("boom"))
    runtime = _make_runtime(agent=agent)

    result = runtime.handle_invocation({"user_id": "1", "message": "hi"})

    assert result["ok"] is False
    assert result["error"] == "AGENT_ERROR"


# =============================================================================
# CreateEvent failure graceful degradation (Req 4.6)
# =============================================================================
def test_handle_invocation_persist_failure_still_returns_response():
    """A persist (CreateEvent) failure is non-fatal: response still returned (Req 4.6)."""
    memory = FakeMemory(persist_result=False)
    agent = FakeAgent(response="advice")
    runtime = _make_runtime(memory=memory, agent=agent)

    result = runtime.handle_invocation({"user_id": "1", "message": "hi"})

    assert result["ok"] is True
    assert result["text"] == "advice"
    assert result["metadata"]["transcript_persisted"] is False


class _RaisingAgentcoreClient:
    """Fake boto3 agentcore client whose CreateEvent raises."""

    def create_event(self, **kwargs):
        raise RuntimeError("CreateEvent boom")

    def retrieve_memory_records(self, **kwargs):
        return {"memoryRecordSummaries": []}


def test_sprout_memory_persist_swallows_createevent_error(caplog):
    """SproutMemory.persist returns False and logs when CreateEvent fails (Req 4.4, 4.6)."""
    mem = SproutMemory("mem-123", client=_RaisingAgentcoreClient())

    ok = mem.persist("chat-1", "sess-1", [{"role": "USER", "content": "hi"}])

    assert ok is False


class _RecordingAgentcoreClient:
    """Fake agentcore client capturing CreateEvent kwargs for assertions."""

    def __init__(self):
        self.create_event_kwargs = None

    def create_event(self, **kwargs):
        self.create_event_kwargs = kwargs
        return {"event": {"eventId": "evt-1"}}


def test_sprout_memory_persist_success_keys_event_by_actor():
    """A successful persist keys CreateEvent by memoryId/actorId/sessionId (Req 4.4)."""
    client = _RecordingAgentcoreClient()
    mem = SproutMemory("mem-123", client=client)

    ok = mem.persist(
        "chat-1", "sess-1", [{"role": "USER", "content": "hello"}]
    )

    assert ok is True
    kwargs = client.create_event_kwargs
    assert kwargs["memoryId"] == "mem-123"
    assert kwargs["actorId"] == "chat-1"
    assert kwargs["sessionId"] == "sess-1"
    assert kwargs["payload"][0]["conversational"]["content"]["text"] == "hello"
    assert kwargs["payload"][0]["conversational"]["role"] == "USER"


# =============================================================================
# Memory retrieval timeout / error graceful degradation (Req 5.5)
# =============================================================================
def test_sprout_memory_retrieve_error_returns_empty():
    """A retrieve error degrades to an empty context list (Req 5.5)."""

    class _ErroringClient:
        def retrieve_memory_records(self, **kwargs):
            raise RuntimeError("retrieve boom")

    mem = SproutMemory("mem-123", client=_ErroringClient())

    assert mem.retrieve("chat-1", "query") == []


def test_sprout_memory_retrieve_timeout_returns_empty(monkeypatch):
    """A retrieve exceeding the timeout budget degrades to no context (Req 5.5).

    The 3s budget is shrunk to keep the test fast; the blocking client sleeps
    past the shortened budget so the FuturesTimeout path is exercised.
    """
    monkeypatch.setattr(server, "MEMORY_RETRIEVE_TIMEOUT_SECONDS", 0.1)

    class _SlowClient:
        def retrieve_memory_records(self, **kwargs):
            time.sleep(1.0)
            return {"memoryRecordSummaries": [{"content": {"text": "late"}}]}

    mem = SproutMemory("mem-123", client=_SlowClient())

    assert mem.retrieve("chat-1", "query") == []


def test_sprout_memory_retrieve_success_normalizes_records():
    """A successful retrieve normalizes summaries into MemoryContextRecords."""

    class _OkClient:
        def __init__(self):
            self.kwargs = None

        def retrieve_memory_records(self, **kwargs):
            self.kwargs = kwargs
            return {
                "memoryRecordSummaries": [
                    {
                        "content": {"text": "Grows tomatoes"},
                        "metadata": {"confidence_class": "EXPLICIT", "topic": "plants"},
                        "memoryRecordId": "r1",
                    }
                ]
            }

    client = _OkClient()
    mem = SproutMemory("mem-123", client=client)

    records = mem.retrieve("chat-1", "tomato care")

    assert len(records) == 1
    assert records[0].content == "Grows tomatoes"
    assert records[0].confidence_class is Confidence.EXPLICIT
    # Retrieval is scoped to the user's long-term subtree via namespacePath (not
    # the exact `namespace`, which would omit session summaries stored one level
    # deeper at sprout/{chat_id}/long_term/{sessionId}).
    assert client.kwargs["namespacePath"] == "sprout/chat-1/long_term"
    assert "namespace" not in client.kwargs
    assert client.kwargs["searchCriteria"] == {"searchQuery": "tomato care"}
    assert client.kwargs["maxResults"] == server.MAX_MEMORIES_IN_CONTEXT


def test_handle_invocation_degrades_when_retrieve_returns_empty():
    """When retrieval yields nothing, the agent still runs with base persona only (Req 5.5)."""
    memory = FakeMemory(records=[])
    agent = FakeAgent(response="advice without memory")
    runtime = _make_runtime(memory=memory, agent=agent)

    result = runtime.handle_invocation({"user_id": "1", "message": "hi"})

    assert result["ok"] is True
    assert result["metadata"]["memory_records_retrieved"] == 0
    # No memory context appended -> system prompt is just the base persona.
    assert agent.calls[0]["system_prompt"] == "You are Sprout."


# =============================================================================
# Workspace download/upload failure graceful degradation (Req 9.4, 9.5)
# =============================================================================
def test_workspace_download_failure_returns_false():
    """A download failure returns False (continue with default state, Req 9.4)."""

    class _ErroringS3:
        def get_paginator(self, name):
            raise RuntimeError("s3 boom")

    store = WorkspaceStore("bucket", client=_ErroringS3())

    assert store.download("chat-1", "/tmp/does-not-matter") is False


def test_workspace_download_no_bucket_returns_false():
    """With no bucket configured, download is a no-op returning False."""
    store = WorkspaceStore(None)

    assert store.download("chat-1", "/tmp/x") is False


def test_workspace_upload_failure_returns_true(tmp_path):
    """An upload failure is non-fatal and still returns True (update lost, Req 9.5)."""
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    class _ErroringS3:
        def upload_file(self, *args, **kwargs):
            raise RuntimeError("upload boom")

    store = WorkspaceStore("bucket", client=_ErroringS3())

    assert store.upload("chat-1", str(tmp_path)) is True


def test_handle_invocation_survives_workspace_failures():
    """Lifecycle completes even when workspace download and upload both fail (Req 9.4, 9.5)."""
    workspace = FakeWorkspace(download_result=False, upload_result=True)
    agent = FakeAgent(response="advice")
    runtime = _make_runtime(workspace=workspace, agent=agent)

    result = runtime.handle_invocation({"user_id": "1", "message": "hi"})

    assert result["ok"] is True
    assert result["text"] == "advice"
    assert workspace.calls == ["download", "upload"]


# =============================================================================
# POST /invocations through the HTTP handler (Req 3.2)
# =============================================================================
def _post_handler(body_bytes: bytes) -> _RecordingHandler:
    handler = _RecordingHandler()
    handler.path = "/invocations"
    handler.headers = {"Content-Length": str(len(body_bytes))}
    handler.rfile = io.BytesIO(body_bytes)
    return handler


def test_post_invocations_delegates_to_runtime(monkeypatch):
    """POST /invocations delegates to get_runtime().handle_invocation and returns 200."""
    runtime = _make_runtime(agent=FakeAgent(response="hello from sprout"))
    monkeypatch.setattr(server, "get_runtime", lambda: runtime)

    body = json.dumps({"user_id": "1", "message": "hi"}).encode("utf-8")
    handler = _post_handler(body)

    handler.do_POST()

    assert handler.status == 200
    result = handler.body_json()
    assert result["ok"] is True
    assert result["text"] == "hello from sprout"


def test_post_invocations_invalid_json_returns_400():
    """A non-JSON body returns a 400 INVALID_REQUEST envelope."""
    handler = _post_handler(b"not-json{")

    handler.do_POST()

    assert handler.status == 400
    assert handler.body_json()["error"] == "INVALID_REQUEST"


def test_post_invocations_invalid_user_id_returns_200_envelope(monkeypatch):
    """A rejected invocation surfaces the INVALID_USER_ID envelope at HTTP 200 (Req 3.2)."""
    runtime = _make_runtime()
    monkeypatch.setattr(server, "get_runtime", lambda: runtime)

    body = json.dumps({"message": "hi"}).encode("utf-8")
    handler = _post_handler(body)

    handler.do_POST()

    assert handler.status == 200
    result = handler.body_json()
    assert result["ok"] is False
    assert result["error"] == "INVALID_USER_ID"


def test_post_unknown_path_returns_404():
    """POST to an unknown path returns a 404 error envelope."""
    handler = _RecordingHandler()
    handler.path = "/nope"
    handler.headers = {"Content-Length": "0"}
    handler.rfile = io.BytesIO(b"")

    handler.do_POST()

    assert handler.status == 404
    assert handler.body_json()["error"] == "NOT_FOUND"


# =============================================================================
# OpenClawAgent -> real OpenClaw gateway HTTP forwarding (Req 3.3, 8.x)
# =============================================================================
class _FakeResponse:
    """Minimal stand-in for a ``requests.Response``."""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _install_fake_requests(monkeypatch, *, response=None, exc=None):
    """Install a fake ``requests`` module exposing ``post``/``get`` capturing calls.

    ``OpenClawAgent.process`` and ``wait_for_openclaw`` do ``import requests``
    then call ``requests.post`` / ``requests.get``; injecting a module into
    ``sys.modules`` lets the tests mock the gateway without the real dependency.
    """
    import types

    calls = {}

    def _post(url, **kwargs):
        calls["url"] = url
        calls["json"] = kwargs.get("json")
        calls["headers"] = kwargs.get("headers")
        calls["timeout"] = kwargs.get("timeout")
        if exc is not None:
            raise exc
        return response

    fake = types.SimpleNamespace(post=_post, get=_post)
    monkeypatch.setitem(sys.modules, "requests", fake)
    # process() ensures the gateway is up before forwarding; in unit tests there
    # is no real gateway, so stub the readiness check to a healthy no-op.
    monkeypatch.setattr(server, "ensure_openclaw_ready", lambda *a, **k: True)
    return calls


def test_openclaw_agent_posts_to_gateway_and_returns_content(monkeypatch):
    """process() POSTs OpenAI-style messages to the gateway and returns the text."""
    payload = {"choices": [{"message": {"content": "Prune in early spring."}}]}
    calls = _install_fake_requests(monkeypatch, response=_FakeResponse(200, payload))
    monkeypatch.setenv("OPENCLAW_AUTH_TOKEN", "secret-token")

    agent = OpenClawAgent("us.model-v1", enable_prompt_cache=True)
    text = agent.process(system_prompt="You are Sprout.", message="When do I prune?")

    assert text == "Prune in early spring."
    # POSTs to the loopback gateway chat completions endpoint with bearer auth.
    assert calls["url"] == f"{server.OPENCLAW_URL}/v1/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer secret-token"
    assert calls["json"]["model"] == "us.model-v1"
    assert calls["json"]["messages"][0] == {
        "role": "system",
        "content": "You are Sprout.",
    }
    assert calls["json"]["messages"][1] == {
        "role": "user",
        "content": "When do I prune?",
    }


def test_openclaw_agent_non_200_raises_runtime_error(monkeypatch):
    """A non-200 gateway response raises RuntimeError (mapped to AGENT_ERROR)."""
    _install_fake_requests(monkeypatch, response=_FakeResponse(500, text="boom"))
    agent = OpenClawAgent("m", enable_prompt_cache=True)

    with pytest.raises(RuntimeError):
        agent.process(system_prompt="s", message="hi")


def test_openclaw_agent_request_exception_raises_runtime_error(monkeypatch):
    """A transport error contacting the gateway raises RuntimeError."""
    _install_fake_requests(monkeypatch, exc=OSError("connection refused"))
    agent = OpenClawAgent("m", enable_prompt_cache=True)

    with pytest.raises(RuntimeError):
        agent.process(system_prompt="s", message="hi")


def test_openclaw_agent_unparseable_response_raises_runtime_error(monkeypatch):
    """A 200 response missing choices[].message.content raises RuntimeError."""
    _install_fake_requests(monkeypatch, response=_FakeResponse(200, {"nope": True}))
    agent = OpenClawAgent("m", enable_prompt_cache=True)

    with pytest.raises(RuntimeError):
        agent.process(system_prompt="s", message="hi")


def test_openclaw_agent_forwards_images_via_bedrock_converse(monkeypatch):
    """Images are routed to Bedrock Converse directly (vision path, Req 8.2)."""
    # Mock boto3.client("bedrock-runtime").converse(...)
    converse_args = {}

    class FakeBedrockClient:
        def converse(self, **kwargs):
            converse_args.update(kwargs)
            return {
                "output": {
                    "message": {
                        "content": [{"text": "That's a peace lily (Spathiphyllum)."}]
                    }
                }
            }

    class FakeBoto3:
        def client(self, service_name, **kwargs):
            return FakeBedrockClient()

    import types
    fake_boto3 = types.SimpleNamespace(client=FakeBoto3().client)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    # Set the vision model env var so the test is deterministic.
    monkeypatch.setenv("VISION_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0")

    agent = OpenClawAgent("us.anthropic.claude-haiku-4-5-20251001-v1:0", enable_prompt_cache=True)
    images = [{"data": "Zm9v", "media_type": "image/jpeg"}]
    text = agent.process(system_prompt="You are Sprout.", message="what plant?", images=images)

    assert text == "That's a peace lily (Spathiphyllum)."
    # Verify the Converse call used the VISION model (Sonnet), not the text model (Haiku).
    assert converse_args["modelId"] == "us.anthropic.claude-sonnet-4-20250514-v1:0"
    user_content = converse_args["messages"][0]["content"]
    image_blocks = [b for b in user_content if "image" in b]
    text_blocks = [b for b in user_content if "text" in b]
    assert len(image_blocks) == 1
    assert text_blocks[0]["text"] == "what plant?"


def test_openclaw_agent_vision_no_text_uses_default_prompt(monkeypatch):
    """Vision path with no text caption uses a default identification prompt."""
    converse_args = {}

    class FakeBedrockClient:
        def converse(self, **kwargs):
            converse_args.update(kwargs)
            return {"output": {"message": {"content": [{"text": "It's basil."}]}}}

    import types
    fake_boto3 = types.SimpleNamespace(client=lambda svc, **kw: FakeBedrockClient())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    agent = OpenClawAgent("m", enable_prompt_cache=True)
    text = agent.process(system_prompt="s", message="", images=[{"data": "Zm9v", "media_type": "image/png"}])

    assert text == "It's basil."
    user_content = converse_args["messages"][0]["content"]
    text_blocks = [b for b in user_content if "text" in b]
    assert "identify" in text_blocks[0]["text"].lower()


def test_openclaw_agent_joins_list_content(monkeypatch):
    """Content returned as a list of text parts is joined into a string."""
    payload = {
        "choices": [
            {"message": {"content": [{"text": "Hello "}, {"text": "gardener"}]}}
        ]
    }
    _install_fake_requests(monkeypatch, response=_FakeResponse(200, payload))
    agent = OpenClawAgent("m", enable_prompt_cache=True)

    text = agent.process(system_prompt="s", message="hi")

    assert text == "Hello gardener"
