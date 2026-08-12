"""Unit tests for structured long-term records + metadata (``server.py``).

The gardener's backyard sections are written DELIBERATELY as structured
AgentCore Memory records (``BatchCreateMemoryRecords``) with queryable metadata,
rather than relying only on asynchronous extraction from chat text.

IMPORTANT (verified against the live service): custom metadata keys ARE filterable
server-side, but only when declared as ``IndexedKeys`` on the memory resource —
AgentCore then applies them BEFORE the vector search. Filtering on a key that is
not indexed raises ``ValidationException`` ("not a valid filter key"); such keys
are still stored and returned, so they are filtered in-process instead. These
tests pin down that split so filters are not silently stopped from being pushed
down (which would let a record be missed because it did not rank in the top-K).

Covers the pure builders (:func:`server.derive_section_slug`,
:func:`server.build_metadata_map`, :func:`server.flatten_metadata`,
:func:`server.build_section_record`, :func:`server.filter_records_by_metadata`),
:meth:`server.SproutMemory.write_records` with a fake client, and the album ->
section registration path through ``handle_invocation``.
"""

from __future__ import annotations

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from server import (  # noqa: E402
    Confidence,
    MemoryContextRecord,
    SproutMemory,
    build_metadata_map,
    build_section_record,
    derive_section_slug,
    filter_records_by_metadata,
    flatten_metadata,
    normalize_record,
)


# =============================================================================
# derive_section_slug
# =============================================================================
def test_section_slug_normalizes_equivalent_names():
    assert derive_section_slug("North Bed") == "north_bed"
    assert derive_section_slug("  north   bed  ") == "north_bed"
    assert derive_section_slug("North Bed!") == "north_bed"
    # Same physical bed written differently maps to one token.
    assert derive_section_slug("North-Bed") == derive_section_slug("north bed")


def test_section_slug_empty_when_no_usable_chars():
    assert derive_section_slug("") == ""
    assert derive_section_slug("!!!") == ""


# =============================================================================
# build_metadata_map / flatten_metadata (round trip)
# =============================================================================
def test_build_metadata_map_maps_each_python_type():
    now = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    out = build_metadata_map(
        {"s": "north_bed", "n": 5, "f": 1.5, "l": ["basil", "tomato"], "d": now}
    )
    assert out["s"] == {"stringValue": "north_bed"}
    assert out["n"] == {"numberValue": 5.0}
    assert out["f"] == {"numberValue": 1.5}
    assert out["l"] == {"stringListValue": ["basil", "tomato"]}
    assert out["d"] == {"dateTimeValue": now}


def test_build_metadata_map_omits_empty_values():
    out = build_metadata_map({"a": None, "b": "", "c": [], "d": "keep"})
    assert out == {"d": {"stringValue": "keep"}}


def test_flatten_metadata_unwraps_and_drops_reserved_keys():
    flat = flatten_metadata(
        {
            "section": {"stringValue": "north_bed"},
            "plants": {"stringListValue": ["basil"]},
            "photo_count": {"numberValue": 2.0},
            "x-amz-agentcore-memory-recordType": {"stringValue": "BASE"},
        }
    )
    assert flat == {"section": "north_bed", "plants": ["basil"], "photo_count": 2.0}


def test_metadata_round_trip():
    values = {"section": "north_bed", "plants": ["basil", "tomato"], "photo_count": 3}
    assert flatten_metadata(build_metadata_map(values)) == {
        "section": "north_bed",
        "plants": ["basil", "tomato"],
        "photo_count": 3.0,
    }


# =============================================================================
# build_section_record
# =============================================================================
def test_build_section_record_shape_and_metadata():
    now = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    rec = build_section_record(
        chat_id="12345",
        section_name="North Bed",
        summary="Basil and tomato, full sun.",
        plants=["basil", "tomato"],
        photo_count=4,
        now=now,
    )
    assert rec["namespaces"] == [server.derive_long_term_namespace("12345")]
    assert rec["content"] == {"text": "Basil and tomato, full sun."}
    assert rec["timestamp"] == now
    assert rec["metadata"]["type"] == {"stringValue": "section"}
    assert rec["metadata"]["section"] == {"stringValue": "north_bed"}
    assert rec["metadata"]["plants"] == {"stringListValue": ["basil", "tomato"]}
    assert rec["metadata"]["photo_count"] == {"numberValue": 4.0}


def test_build_section_record_identifier_is_stable_per_chat_and_section():
    a = build_section_record(chat_id="1", section_name="North Bed", summary="x")
    b = build_section_record(chat_id="1", section_name="north bed", summary="y")
    c = build_section_record(chat_id="2", section_name="North Bed", summary="x")
    d = build_section_record(chat_id="1", section_name="South Bed", summary="x")
    assert a["requestIdentifier"] == b["requestIdentifier"]  # same bed, re-registered
    assert a["requestIdentifier"] != c["requestIdentifier"]  # different gardener
    assert a["requestIdentifier"] != d["requestIdentifier"]  # different bed


def test_build_section_record_unnamed_fallback():
    rec = build_section_record(chat_id="1", section_name="!!!", summary="x")
    assert rec["metadata"]["section"] == {"stringValue": "unnamed"}


# =============================================================================
# filter_records_by_metadata (in-process; for keys that are NOT indexed)
# =============================================================================
def _rec(content, **metadata):
    return MemoryContextRecord(
        content=content, confidence_class=Confidence.EXPLICIT, metadata=metadata
    )


def test_filter_by_scalar_metadata():
    records = [
        _rec("north", section="north_bed", type="section"),
        _rec("south", section="south_bed", type="section"),
    ]
    out = filter_records_by_metadata(records, {"section": "north_bed"})
    assert [r.content for r in out] == ["north"]


def test_filter_matches_membership_in_list_values():
    records = [
        _rec("north", plants=["basil", "tomato"]),
        _rec("south", plants=["rose"]),
    ]
    assert [r.content for r in filter_records_by_metadata(records, {"plants": "basil"})] == ["north"]


def test_filter_is_case_insensitive_and_requires_all_keys():
    records = [_rec("north", section="north_bed", type="section")]
    assert filter_records_by_metadata(records, {"section": "North_Bed"})
    # A record missing a filtered key never matches.
    assert filter_records_by_metadata(records, {"missing": "x"}) == []
    # All filters must match.
    assert filter_records_by_metadata(records, {"section": "north_bed", "type": "plant"}) == []


def test_filter_empty_is_noop():
    records = [_rec("a"), _rec("b")]
    assert filter_records_by_metadata(records, {}) == records


def test_normalize_record_exposes_flat_metadata():
    rec = normalize_record(
        {
            "content": {"text": "north bed"},
            "metadata": {
                "section": {"stringValue": "north_bed"},
                "x-amz-agentcore-memory-recordType": {"stringValue": "BASE"},
            },
        }
    )
    assert rec.metadata == {"section": "north_bed"}


# =============================================================================
# SproutMemory.write_records
# =============================================================================
class _FakeClient:
    def __init__(self, *, successful=1, failed=0, exc=None):
        self._successful, self._failed, self._exc = successful, failed, exc
        self.calls = []

    def batch_create_memory_records(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return {
            "successfulRecords": [{"status": "SUCCEEDED"}] * self._successful,
            "failedRecords": [{"status": "FAILED"}] * self._failed,
        }


def _memory(client):
    return SproutMemory(memory_id="mem-1", client=client)


def test_write_records_calls_batch_create_and_counts_successes():
    client = _FakeClient(successful=2)
    rec = build_section_record(chat_id="1", section_name="North Bed", summary="x")

    assert _memory(client).write_records([rec, rec]) == 2
    assert client.calls[0]["memoryId"] == "mem-1"
    assert len(client.calls[0]["records"]) == 2


def test_write_records_reports_partial_failure_count():
    assert _memory(_FakeClient(successful=1, failed=1)).write_records([{}, {}]) == 1


def test_write_records_degrades_on_error():
    # Must never break the user's turn (the transcript is still persisted).
    assert _memory(_FakeClient(exc=RuntimeError("boom"))).write_records([{}]) == 0


def test_write_records_noop_on_empty():
    client = _FakeClient()
    assert _memory(client).write_records([]) == 0
    assert client.calls == []


# =============================================================================
# retrieve(): indexed keys filter server-side, others in-process
# =============================================================================
class _RetrieveClient:
    """Fake that honors ``metadataFilters`` the way the service does.

    The real service pre-filters on indexed keys BEFORE the vector search, so a
    filtered request must come back already narrowed — this fake reproduces that
    so the test would fail if we silently stopped pushing filters down.
    """

    _RECORDS = [
        {
            "content": {"text": "north bed: basil"},
            "metadata": {
                "section": {"stringValue": "north_bed"},
                "plants": {"stringListValue": ["basil", "tomato"]},
                "photo_count": {"numberValue": 2.0},
            },
        },
        {
            "content": {"text": "south bed: roses"},
            "metadata": {
                "section": {"stringValue": "south_bed"},
                "plants": {"stringListValue": ["rose"]},
                "photo_count": {"numberValue": 5.0},
            },
        },
    ]

    def retrieve_memory_records(self, **kwargs):
        self.last_kwargs = kwargs
        out = []
        for rec in self._RECORDS:
            keep = True
            for f in kwargs["searchCriteria"].get("metadataFilters", []):
                key = f["left"]["metadataKey"]
                want = f["right"]["metadataValue"]["stringValue"]
                wrapped = rec["metadata"].get(key, {})
                actual = wrapped.get("stringValue", wrapped.get("stringListValue"))
                if f["operator"] == "EQUALS_TO" and actual != want:
                    keep = False
                elif f["operator"] == "CONTAINS" and want not in (actual or []):
                    keep = False
            if keep:
                out.append(rec)
        return {"memoryRecordSummaries": out}


def test_retrieve_pushes_indexed_keys_down_as_server_side_filters():
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    assert len(mem.retrieve("12345", "beds")) == 2
    # No filters requested -> no metadataFilters in the request at all.
    assert "metadataFilters" not in client.last_kwargs["searchCriteria"]

    filtered = mem.retrieve("12345", "beds", metadata_filters={"section": "north_bed"})
    assert [r.content for r in filtered] == ["north bed: basil"]

    sent = client.last_kwargs["searchCriteria"]["metadataFilters"]
    assert sent == [
        {
            "left": {"metadataKey": "section"},
            "operator": "EQUALS_TO",
            "right": {"metadataValue": {"stringValue": "north_bed"}},
        }
    ]


def test_retrieve_uses_contains_for_stringlist_plants():
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    filtered = mem.retrieve("12345", "beds", metadata_filters={"plants": "basil"})

    assert [r.content for r in filtered] == ["north bed: basil"]
    assert client.last_kwargs["searchCriteria"]["metadataFilters"][0]["operator"] == "CONTAINS"


def test_retrieve_filters_non_indexed_key_in_process():
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    # photo_count is NOT an indexed key: it must never be sent to the service
    # (that would raise ValidationException) and is applied in-process instead.
    filtered = mem.retrieve("12345", "beds", metadata_filters={"photo_count": 5.0})

    assert [r.content for r in filtered] == ["south bed: roses"]
    assert "metadataFilters" not in client.last_kwargs["searchCriteria"]


def test_build_metadata_filters_splits_indexed_from_residual():
    pushed, residual = server.build_metadata_filters(
        {"section": "north_bed", "photo_count": 2}
    )

    assert [f["left"]["metadataKey"] for f in pushed] == ["section"]
    assert residual == {"photo_count": 2}


def test_build_metadata_filters_expands_list_values():
    pushed, residual = server.build_metadata_filters({"plants": ["basil", "mint"]})

    assert [f["right"]["metadataValue"]["stringValue"] for f in pushed] == ["basil", "mint"]
    assert all(f["operator"] == "CONTAINS" for f in pushed)
    assert residual == {}


def test_build_metadata_filters_empty_is_noop():
    assert server.build_metadata_filters({}) == ([], {})


# =============================================================================
# Album -> section registration through handle_invocation
# =============================================================================
class _FakeS3:
    def __init__(self):
        self.store = {}

    def put_object(self, Bucket, Key, Body=b"", IfNoneMatch=None, ContentType=None):
        if IfNoneMatch == "*" and Key in self.store:
            err = Exception("exists")
            err.response = {"Error": {"Code": "PreconditionFailed"}}
            raise err
        self.store[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        import io

        return {"Body": io.BytesIO(self.store[Key])}

    def get_paginator(self, _op):
        store = self.store

        class _P:
            def paginate(self, Bucket=None, Prefix=""):
                return [
                    {"Contents": [{"Key": k} for k in sorted(store) if k.startswith(Prefix)]}
                ]

        return _P()

    def delete_objects(self, Bucket, Delete):
        for obj in Delete["Objects"]:
            self.store.pop(obj["Key"], None)
        return {}


class _RecordingMemory:
    def __init__(self):
        self.written = []

    def retrieve(self, chat_id, query, *, metadata_filters=None):
        return []

    def persist(self, chat_id, session_id, messages, metadata=None):
        return True

    def write_records(self, records):
        self.written.extend(records)
        return len(records)


class _StubAgent:
    def process(self, *, system_prompt, message, images=None, workspace_dir=None):
        return "Lovely bed! I see basil and tomato."


class _StubWorkspace:
    def download(self, chat_id, dest):
        return True

    def upload(self, chat_id, src):
        return True


def test_captioned_album_registers_a_section_record():
    memory = _RecordingMemory()
    buf = server.AlbumBuffer("bucket", client=_FakeS3(), debounce_seconds=0)
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_StubAgent(),
        base_persona="You are Sprout.",
        model_id="model",
        album_buffer=buf,
    )

    result = runtime.handle_invocation(
        {
            "user_id": "12345",
            "message": "North bed",
            "images": [{"data": "aaa", "media_type": "image/png"}],
            "invocation_type": "webhook",
            "media_group_id": "gsec1",
            "message_id": "1",
        }
    )

    assert result["ok"] is True
    assert result["metadata"]["sections_written"] == 1
    record = memory.written[0]
    assert record["metadata"]["section"] == {"stringValue": "north_bed"}
    assert record["metadata"]["type"] == {"stringValue": "section"}
    assert "North bed" in record["content"]["text"]


def test_uncaptioned_album_writes_no_section_record():
    memory = _RecordingMemory()
    buf = server.AlbumBuffer("bucket", client=_FakeS3(), debounce_seconds=0)
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_StubAgent(),
        base_persona="You are Sprout.",
        model_id="model",
        album_buffer=buf,
    )

    result = runtime.handle_invocation(
        {
            "user_id": "12345",
            "message": "",
            "images": [{"data": "aaa", "media_type": "image/png"}],
            "invocation_type": "webhook",
            "media_group_id": "gsec2",
            "message_id": "1",
        }
    )

    # No caption -> no section name to register (the bot asks the gardener).
    assert result["metadata"]["sections_written"] == 0
    assert memory.written == []


def test_normal_turn_writes_no_section_record():
    memory = _RecordingMemory()
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_StubAgent(),
        base_persona="You are Sprout.",
        model_id="model",
    )

    result = runtime.handle_invocation({"user_id": "12345", "message": "hello"})

    assert result["metadata"]["sections_written"] == 0
    assert memory.written == []


# =============================================================================
# Plant-name extraction -> section record ``plants`` metadata
# =============================================================================
from server import parse_extracted_plants  # noqa: E402


def test_parse_extracted_plants_basic():
    assert parse_extracted_plants('{"plants": ["basil", "tomato"]}') == ["basil", "tomato"]


def test_parse_extracted_plants_tolerates_fences_and_prose():
    raw = 'Sure:\n```json\n{"plants": ["Basil", "Mint"]}\n```\nDone.'
    assert parse_extracted_plants(raw) == ["basil", "mint"]


def test_parse_extracted_plants_normalizes_and_dedupes():
    raw = '{"plants": ["Basil", " basil ", "TOMATO", "", 5, null]}'
    assert parse_extracted_plants(raw) == ["basil", "tomato"]


def test_parse_extracted_plants_empty_and_invalid():
    assert parse_extracted_plants('{"plants": []}') == []
    assert parse_extracted_plants("not json") == []
    assert parse_extracted_plants("") == []
    assert parse_extracted_plants('{"plants": "basil"}') == []


def test_parse_extracted_plants_caps_length():
    many = ", ".join(f'"p{i}"' for i in range(60))
    assert len(parse_extracted_plants('{"plants": [%s]}' % many)) == server.MAX_SECTION_PLANTS


class _PlantAgent(_StubAgent):
    def __init__(self, plants):
        self._plants = plants
        self.calls = []

    def extract_plant_names(self, *, description):
        self.calls.append(description)
        return list(self._plants)


def _runtime_with_agent(agent, memory):
    return server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=agent,
        base_persona="You are Sprout.",
        model_id="model",
        album_buffer=server.AlbumBuffer("bucket", client=_FakeS3(), debounce_seconds=0),
    )


def _album_payload(group):
    return {
        "user_id": "12345",
        "message": "North bed",
        "images": [{"data": "aaa", "media_type": "image/png"}],
        "invocation_type": "webhook",
        "media_group_id": group,
        "message_id": "1",
    }


def test_section_record_includes_extracted_plants():
    memory = _RecordingMemory()
    agent = _PlantAgent(["basil", "tomato"])
    runtime = _runtime_with_agent(agent, memory)

    result = runtime.handle_invocation(_album_payload("gp1"))

    assert result["metadata"]["sections_written"] == 1
    assert memory.written[0]["metadata"]["plants"] == {
        "stringListValue": ["basil", "tomato"]
    }
    # The extractor saw the assistant's description of the bed.
    assert "basil" in agent.calls[0] or agent.calls[0]


def test_section_record_omits_plants_when_none_extracted():
    memory = _RecordingMemory()
    runtime = _runtime_with_agent(_PlantAgent([]), memory)

    runtime.handle_invocation(_album_payload("gp2"))

    # build_metadata_map omits empty lists, so no plants key is written.
    assert "plants" not in memory.written[0]["metadata"]


def test_section_still_written_when_plant_extraction_raises():
    class _BoomAgent(_StubAgent):
        def extract_plant_names(self, *, description):
            raise RuntimeError("bedrock down")

    memory = _RecordingMemory()
    runtime = _runtime_with_agent(_BoomAgent(), memory)

    result = runtime.handle_invocation(_album_payload("gp3"))

    # Enrichment failure must not lose the section registration.
    assert result["metadata"]["sections_written"] == 1
    assert "plants" not in memory.written[0]["metadata"]


def test_plants_metadata_is_filterable_client_side():
    # End-to-end intent: "which beds have basil?" works via list membership.
    records = [
        _rec("north", section="north_bed", plants=["basil", "tomato"]),
        _rec("south", section="south_bed", plants=["rose"]),
    ]
    out = filter_records_by_metadata(records, {"plants": "Basil"})
    assert [r.content for r in out] == ["north"]


# =============================================================================
# CreateEvent metadata: dimensions known at event time travel the event path too
# =============================================================================
from server import build_event_metadata  # noqa: E402


def test_build_event_metadata_stringvalue_only():
    # CreateEvent metadata is stringValue-only; numbers are coerced, and
    # list-valued dimensions (plants) cannot travel this path at all.
    out = build_event_metadata({"section": "north_bed", "photo_count": 3, "plants": ["basil"]})
    assert out == {
        "section": {"stringValue": "north_bed"},
        "photo_count": {"stringValue": "3"},
    }


def test_build_event_metadata_drops_empty():
    assert build_event_metadata({"a": None, "b": "", "c": "  ", "d": "keep"}) == {
        "d": {"stringValue": "keep"}
    }


class _EventClient:
    def __init__(self):
        self.kwargs = None

    def create_event(self, **kwargs):
        self.kwargs = kwargs
        return {}


def test_persist_sends_metadata_on_create_event():
    client = _EventClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    ok = mem.persist(
        "12345",
        "sess-1",
        [{"role": "USER", "content": "hi"}],
        metadata={"section": {"stringValue": "north_bed"}},
    )

    assert ok is True
    assert client.kwargs["metadata"] == {"section": {"stringValue": "north_bed"}}


def test_persist_omits_metadata_key_when_none():
    client = _EventClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    mem.persist("12345", "sess-1", [{"role": "USER", "content": "hi"}])

    # Never send an empty metadata map — omit the parameter entirely.
    assert "metadata" not in client.kwargs


class _MetadataCapturingMemory(_RecordingMemory):
    def __init__(self):
        super().__init__()
        self.persist_metadata = "unset"

    def persist(self, chat_id, session_id, messages, metadata=None):
        self.persist_metadata = metadata
        return True


def test_captioned_album_attaches_section_metadata_to_the_event():
    memory = _MetadataCapturingMemory()
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_StubAgent(),
        base_persona="You are Sprout.",
        model_id="model",
        album_buffer=server.AlbumBuffer("bucket", client=_FakeS3(), debounce_seconds=0),
    )

    runtime.handle_invocation(_album_payload("gev1"))

    # The section is known at event time, so it rides the CreateEvent path and
    # extraction propagates it onto the records it derives.
    assert memory.persist_metadata == {
        "type": {"stringValue": "section"},
        "section": {"stringValue": "north_bed"},
    }


def test_normal_turn_sends_no_event_metadata():
    memory = _MetadataCapturingMemory()
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_StubAgent(),
        base_persona="You are Sprout.",
        model_id="model",
    )

    runtime.handle_invocation({"user_id": "12345", "message": "hello"})

    assert memory.persist_metadata is None


# =============================================================================
# Temporal filtering on the reserved createdAt field
# =============================================================================
def test_build_created_after_filter_uses_reserved_key():
    moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    f = server.build_created_after_filter(moment)

    assert f == {
        "left": {"metadataKey": "x-amz-agentcore-memory-createdAt"},
        "operator": "AFTER",
        "right": {"metadataValue": {"dateTimeValue": moment}},
    }


def test_retrieve_pushes_created_after_down():
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)
    moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    mem.retrieve("12345", "beds", created_after=moment)

    sent = client.last_kwargs["searchCriteria"]["metadataFilters"]
    assert sent == [server.build_created_after_filter(moment)]


def test_retrieve_combines_indexed_and_temporal_filters():
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)
    moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    mem.retrieve(
        "12345", "beds", metadata_filters={"section": "north_bed"}, created_after=moment
    )

    keys = [f["left"]["metadataKey"] for f in client.last_kwargs["searchCriteria"]["metadataFilters"]]
    assert keys == ["section", "x-amz-agentcore-memory-createdAt"]


# =============================================================================
# build_section_summary: terse content (several records cover the same fact)
# =============================================================================
def test_section_summary_is_terse_and_factual():
    s = server.build_section_summary(
        section_name="Herb bed", plants=["basil", "mint"], photo_count=2
    )
    assert s == "Backyard section 'Herb bed'; contains basil, mint; registered from 2 photo(s)."


def test_section_summary_omits_missing_parts():
    assert server.build_section_summary(section_name="North bed") == "Backyard section 'North bed'."


def test_section_summary_handles_blank_name():
    assert server.build_section_summary(section_name="  ").startswith("Backyard section 'unnamed section'")


def test_section_record_content_excludes_the_agent_reply():
    memory = _RecordingMemory()
    runtime = server.SproutRuntime(
        memory=memory,
        workspace=_StubWorkspace(),
        agent=_PlantAgent(["basil", "mint"]),
        base_persona="You are Sprout.",
        model_id="model",
        album_buffer=server.AlbumBuffer("bucket", client=_FakeS3(), debounce_seconds=0),
    )

    runtime.handle_invocation(_album_payload("gsum1"))

    text = memory.written[0]["content"]["text"]
    # The conversational reply must not be embedded — extraction already stores
    # its own (tighter) version of the same fact.
    assert "Lovely bed!" not in text
    assert text == "Backyard section 'North bed'; contains basil, mint; registered from 1 photo(s)."


# =============================================================================
# Namespace vs namespacePath: summaries live one level deeper
# =============================================================================
def test_retrieve_uses_namespacepath_not_exact_namespace():
    """Regression guard for a bug we shipped and had to correct.

    ``namespace`` matches ONLY the exact namespace given (verified against the
    service), so querying it omits session summaries stored at
    ``sprout/{chat_id}/long_term/{sessionId}`` — leaving the summarization
    strategy write-only, the very problem moving it was meant to fix.
    ``namespacePath`` retrieves the whole subtree.
    """
    client = _RetrieveClient()
    mem = SproutMemory(memory_id="mem-1", client=client)

    mem.retrieve("12345", "beds")

    assert client.last_kwargs["namespacePath"] == "sprout/12345/long_term"
    assert "namespace" not in client.last_kwargs


def test_long_term_namespace_path_matches_the_strategy_namespace():
    # Both extraction strategies write to exactly this namespace; the path form is
    # what retrieval uses so a nested strategy would also be covered.
    assert server.derive_long_term_namespace_path("12345") == "sprout/12345/long_term"
    assert server.derive_long_term_namespace_path("12345") == server.derive_long_term_namespace(
        "12345"
    )
