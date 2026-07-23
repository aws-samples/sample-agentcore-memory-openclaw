"""Property-based tests for memory-context assembly.

Feature: openclaw-telegram-agentcore, Property 1: Memory context assembly
ordering and capping.

Exercises the pure :func:`server.assemble_memory_context` pipeline that the
``server.py`` invocation lifecycle relies on (design "Memory Retrieval and
Context Assembly"). The properties below pin down the four guarantees the design
makes about the assembled output:

1. Explicit-confidence records always precede Inferred-confidence records.
2. When a topic has both an Explicit and a conflicting Inferred record, the
   Explicit record wins and the Inferred record is discarded.
3. The output never exceeds the cap (50 by default) regardless of input size.
4. Relative ordering within each confidence class is stable (preserves the
   original retrieval order).

Validates: Requirements 5.2, 5.3, 5.4
"""

from __future__ import annotations

import os
import sys

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (  # noqa: E402  (path setup must precede the import)
    MAX_MEMORIES_IN_CONTEXT,
    Confidence,
    MemoryContextRecord,
    assemble_memory_context,
)

# Minimum 100 iterations per the design's PBT configuration.
PBT_SETTINGS = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)

_CONFIDENCES = st.sampled_from([Confidence.EXPLICIT, Confidence.INFERRED])
# A small topic alphabet so conflicts (same topic, different confidence) occur
# frequently rather than vanishingly rarely across generated inputs. The empty
# string models a record with "no topic" (never conflicts).
_TOPICS = st.sampled_from(["", "watering", "zone", "fertilizer", "soil", "light"])


@st.composite
def _records(draw, *, min_size: int = 0, max_size: int = 120):
    """Generate a list of MemoryContextRecords with unique, traceable ids.

    Unique ``record_id`` values let assertions track identity through the
    pipeline (for ordering/stability checks). Confidence and topic are drawn
    from constrained domains so Explicit/Inferred conflicts on shared topics are
    well represented in the generated space.
    """
    size = draw(st.integers(min_value=min_size, max_value=max_size))
    records = []
    for index in range(size):
        records.append(
            MemoryContextRecord(
                content=f"content-{index}",
                confidence_class=draw(_CONFIDENCES),
                topic=draw(_TOPICS),
                record_id=str(index),
            )
        )
    return records


def _is_explicit(record: MemoryContextRecord) -> bool:
    return record.confidence_class is Confidence.EXPLICIT


@given(records=_records())
@PBT_SETTINGS
def test_explicit_records_precede_inferred_records(records):
    """Property 1a: every Explicit record appears before every Inferred record.

    Validates: Requirement 5.2
    """
    result = assemble_memory_context(records)

    classes = [_is_explicit(r) for r in result]
    # Once we have seen an Inferred record, no Explicit record may follow.
    seen_inferred = False
    for is_explicit in classes:
        if not is_explicit:
            seen_inferred = True
        else:
            assert not seen_inferred, "an Explicit record followed an Inferred record"


@given(records=_records())
@PBT_SETTINGS
def test_conflicting_topics_resolve_to_explicit(records):
    """Property 1b: a topic with an Explicit record drops conflicting Inferred.

    For every non-empty topic that has at least one Explicit record in the
    input, the output must contain no Inferred record on that topic.

    Validates: Requirement 5.3
    """
    result = assemble_memory_context(records)

    explicit_topics = {
        r.topic for r in records if _is_explicit(r) and r.topic
    }
    for record in result:
        if not _is_explicit(record) and record.topic:
            assert record.topic not in explicit_topics, (
                f"Inferred record survived on topic {record.topic!r} that has an "
                "Explicit record"
            )


@given(records=_records(max_size=200), cap=st.integers(min_value=1, max_value=80))
@PBT_SETTINGS
def test_output_never_exceeds_cap(records, cap):
    """Property 1c: output size never exceeds the cap, for any input size.

    Checked against both the default 50-record cap (Req 5.4) and arbitrary
    explicit caps.

    Validates: Requirement 5.4
    """
    default_result = assemble_memory_context(records)
    assert len(default_result) <= MAX_MEMORIES_IN_CONTEXT

    capped_result = assemble_memory_context(records, max_memories=cap)
    assert len(capped_result) <= cap


@given(records=_records())
@PBT_SETTINGS
def test_stable_relative_order_within_each_class(records):
    """Property 1d: relative order within each confidence class is preserved.

    The surviving records of each class must appear in the same relative order
    as in the input (a stable partition), so assembly never reshuffles records
    beyond moving Explicit ahead of Inferred.

    Validates: Requirement 5.2
    """
    result = assemble_memory_context(records)
    surviving = {r.record_id for r in result}

    # Order of each class as it appears in the result.
    explicit_in_result = [r.record_id for r in result if _is_explicit(r)]
    inferred_in_result = [r.record_id for r in result if not _is_explicit(r)]

    # Expected order: each class's surviving records in original input order.
    explicit_expected = [
        r.record_id for r in records if _is_explicit(r) and r.record_id in surviving
    ]
    inferred_expected = [
        r.record_id for r in records if not _is_explicit(r) and r.record_id in surviving
    ]

    assert explicit_in_result == explicit_expected
    assert inferred_in_result == inferred_expected


@given(records=_records(min_size=1))
@PBT_SETTINGS
def test_assembly_does_not_mutate_input(records):
    """Sanity property: assembly is pure and never mutates its input list."""
    snapshot = list(records)

    assemble_memory_context(records)

    assert records == snapshot
