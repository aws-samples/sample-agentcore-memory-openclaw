"""Unit tests for Telegram album (multi-photo) grouping (``server.py``).

Telegram delivers an album as N separate webhook updates sharing a
``media_group_id``. These tests cover the S3-backed buffer that groups them so
the runtime replies once and saves the album as a single section:

* pure S3 key derivation (:func:`server.album_prefix` / ``album_item_key`` /
  ``album_claim_key``) and the grouped-turn prompt (:func:`server.build_album_prompt`);
* :class:`server.AlbumBuffer` record / atomic claim election / ordered listing /
  cleanup, exercised with a fake S3 client (no AWS); and
* :meth:`server.SproutRuntime.handle_invocation` on the album path — the single
  winner merges all photos into one reply, non-winners return an empty reply,
  and a normal single photo is unaffected.
"""

from __future__ import annotations

import io
import os
import sys

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402  (path setup must precede the import)
from server import (  # noqa: E402
    AlbumBuffer,
    Confidence,
    MemoryContextRecord,
    SproutRuntime,
    album_claim_key,
    album_item_key,
    album_prefix,
    build_album_prompt,
)

BUCKET = "sprout-workspace-test"


# =============================================================================
# Fake S3 client implementing the subset AlbumBuffer uses (incl. If-None-Match)
# =============================================================================
class FakeS3:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):
        if IfNoneMatch == "*" and Key in self.store:
            err = Exception("exists")
            err.response = {"Error": {"Code": "PreconditionFailed"}}
            raise err
        self.store[Key] = Body if isinstance(Body, bytes) else str(Body).encode()
        return {}

    def get_paginator(self, _op):
        store = self.store

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                contents = [{"Key": k} for k in sorted(store) if k.startswith(Prefix)]
                return [{"Contents": contents}]

        return _Paginator()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.store[Key])}

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.store.pop(obj["Key"], None)
        return {}


def _buffer(client=None):
    return AlbumBuffer(BUCKET, client=client or FakeS3(), debounce_seconds=0)


# =============================================================================
# Pure key derivation + prompt
# =============================================================================
def test_album_key_derivation_and_sanitization():
    assert album_prefix("12345", "998877") == "albums/12345/998877/"
    assert album_item_key("12345", "998877", "42") == "albums/12345/998877/items/42.json"
    assert album_claim_key("12345", "998877") == "albums/12345/998877/claimed"
    # Unsafe characters in tokens are collapsed (keys stay in a safe charset).
    assert "/" not in album_item_key("a/b", "c d", "..").rsplit("/", 1)[1]


def test_build_album_prompt_with_caption_names_section():
    prompt = build_album_prompt("North bed", 4)
    assert "North bed" in prompt
    assert "4 photos" in prompt
    assert "one section" in prompt.lower()


def test_build_album_prompt_without_caption_asks_for_name():
    prompt = build_album_prompt("", 3)
    assert "3 photos" in prompt
    assert "ask" in prompt.lower()  # bot should ask what to name the section


# =============================================================================
# AlbumBuffer: record / claim election / ordered listing / cleanup
# =============================================================================
def test_claim_elects_single_winner():
    buf = _buffer()
    assert buf.try_claim("12345", "g1") is True   # first invocation wins
    assert buf.try_claim("12345", "g1") is False  # all others lose (412)


def test_claim_degrades_to_true_on_non_precondition_error():
    class _BoomS3(FakeS3):
        def put_object(self, **_kw):
            raise RuntimeError("network")

    buf = AlbumBuffer(BUCKET, client=_BoomS3(), debounce_seconds=0)
    # A non-412 error must not silence the album — process (reply) anyway.
    assert buf.try_claim("12345", "g1") is True


def test_list_items_returns_all_ordered_by_message_id():
    buf = _buffer()
    for mid in ("10", "2", "1"):
        buf.record_item("12345", "g1", mid, {"message_id": mid, "caption": "", "images": []})
    items = buf.list_items("12345", "g1")
    assert [it["message_id"] for it in items] == ["1", "2", "10"]


def test_cleanup_removes_items_but_keeps_claim_marker():
    client = FakeS3()
    buf = _buffer(client)
    buf.record_item("12345", "g1", "1", {"message_id": "1", "caption": "", "images": []})
    assert buf.try_claim("12345", "g1") is True
    assert any(k.startswith("albums/12345/g1/items/") for k in client.store)

    buf.cleanup("12345", "g1")

    # Buffered items are gone...
    assert not any(k.startswith("albums/12345/g1/items/") for k in client.store)
    # ...but the claim marker remains so a late sibling cannot re-claim and
    # reprocess an emptied album (exactly-once reply).
    assert album_claim_key("12345", "g1") in client.store
    assert buf.try_claim("12345", "g1") is False


# =============================================================================
# handle_invocation album path (winner / loser / single-photo)
# =============================================================================
class _FakeMemory:
    def __init__(self):
        self.persisted = None
        self.written_records = []

    def retrieve(self, chat_id, query, *, metadata_filters=None):
        return []

    def persist(self, chat_id, session_id, messages):
        self.persisted = messages
        return True

    def write_records(self, records):
        self.written_records.extend(records)
        return len(records)


class _FakeWorkspace:
    def download(self, chat_id, dest):
        return True

    def upload(self, chat_id, src):
        return True


class _FakeAgent:
    def __init__(self, response="Here is your North bed."):
        self._response = response
        self.calls = []

    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        self.calls.append({"message": message, "images": images or []})
        return self._response


def _img(tag):
    return {"data": tag, "media_type": "image/jpeg"}


def _runtime(agent, album_buffer):
    return SproutRuntime(
        memory=_FakeMemory(),
        workspace=_FakeWorkspace(),
        agent=agent,
        base_persona="You are Sprout.",
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        album_buffer=album_buffer,
    )


def test_album_winner_merges_all_photos_into_one_reply():
    client = FakeS3()
    buf = _buffer(client)
    # Two sibling album items already buffered (arrived as earlier invocations).
    buf.record_item("12345", "g1", "1", {"message_id": "1", "caption": "North bed", "images": [_img("a")]})
    buf.record_item("12345", "g1", "2", {"message_id": "2", "caption": "", "images": [_img("b")]})

    agent = _FakeAgent()
    runtime = _runtime(agent, buf)

    # The final album item arrives and wins the claim.
    result = runtime.handle_invocation(
        {
            "user_id": "12345",
            "message": "",
            "images": [_img("c")],
            "invocation_type": "webhook",
            "media_group_id": "g1",
            "message_id": "3",
        }
    )

    assert result["ok"] is True
    assert result["text"] == "Here is your North bed."
    # The single grouped turn saw ALL three photos.
    assert len(agent.calls) == 1
    assert [i["data"] for i in agent.calls[0]["images"]] == ["a", "b", "c"]
    # The caption became the section name in the grouped prompt.
    assert "North bed" in agent.calls[0]["message"]


def test_album_loser_returns_empty_and_does_not_call_agent():
    client = FakeS3()
    buf = _buffer(client)
    # Another invocation already claimed this album.
    assert buf.try_claim("12345", "g1") is True

    agent = _FakeAgent()
    runtime = _runtime(agent, buf)

    result = runtime.handle_invocation(
        {
            "user_id": "12345",
            "message": "",
            "images": [_img("x")],
            "invocation_type": "webhook",
            "media_group_id": "g1",
            "message_id": "9",
        }
    )

    assert result["ok"] is True
    assert result["text"] == ""
    assert result["metadata"]["album_role"] == "buffered"
    assert agent.calls == []  # non-winner must not invoke the model


def test_single_photo_without_media_group_is_unaffected():
    agent = _FakeAgent(response="That's a lovely basil.")
    runtime = _runtime(agent, _buffer())

    result = runtime.handle_invocation(
        {"user_id": "12345", "message": "what is this?", "images": [_img("solo")], "invocation_type": "webhook"}
    )

    assert result["text"] == "That's a lovely basil."
    assert len(agent.calls) == 1
    assert [i["data"] for i in agent.calls[0]["images"]] == ["solo"]
    assert "album_role" not in result["metadata"]
