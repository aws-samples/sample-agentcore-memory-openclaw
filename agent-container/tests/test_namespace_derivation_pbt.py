"""Property-based tests for per-user memory namespace derivation.

Feature: openclaw-telegram-agentcore, Property 3: Namespace derivation — unique
mapping from chat_id to namespace.

Exercises the pure :func:`server.derive_long_term_namespace` function that the
``server.py`` memory lifecycle uses to scope every retrieval/persistence call to
the authenticated user (design "Testability"; Req 6.1, 6.2). The properties
below pin down the guarantees the design makes about the derived namespace:

1. Determinism — each unique ``chat_id`` maps to exactly one namespace path
   (the function is a well-defined mapping).
2. Injectivity — no two distinct ``chat_id`` values produce the same namespace,
   so a query scoped to user A's namespace can never collide with user B's.
3. Pattern — the namespace always matches ``sprout/{chat_id}/long_term`` with
   the chat id embedded verbatim as the sole variable segment.

Validates: Requirements 6.1, 6.2
"""

from __future__ import annotations

import os
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

# Make the container module (server.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (  # noqa: E402  (path setup must precede the import)
    LONG_TERM_SEGMENT,
    NAMESPACE_ROOT,
    chat_id_from_namespace,
    derive_long_term_namespace,
)

# Minimum 100 iterations per the design's PBT configuration.
PBT_SETTINGS = settings(max_examples=200)

# Telegram chat ids are stringified integers (positive for private chats,
# negative for groups/supergroups). The namespace convention embeds the chat id
# as a single "/"-delimited path segment, so a well-formed chat id never
# contains "/". We model that input space: realistic numeric ids plus arbitrary
# non-"/" text to stress the derivation beyond the happy path.
_NUMERIC_CHAT_IDS = st.integers(
    min_value=-1_000_000_000_000, max_value=1_000_000_000_000
).map(str)
_TEXT_CHAT_IDS = st.text(
    alphabet=st.characters(blacklist_characters="/"), min_size=1, max_size=40
)
_CHAT_IDS = st.one_of(_NUMERIC_CHAT_IDS, _TEXT_CHAT_IDS)


@given(chat_id=_CHAT_IDS)
@PBT_SETTINGS
def test_namespace_derivation_is_deterministic(chat_id):
    """Property 3a: a chat id always maps to exactly one namespace.

    Calling the derivation twice on the same input yields identical output, so
    the mapping is a well-defined function (Req 6.2 "consistent mapping").

    Validates: Requirement 6.2
    """
    assert derive_long_term_namespace(chat_id) == derive_long_term_namespace(chat_id)


@given(chat_id=_CHAT_IDS)
@PBT_SETTINGS
def test_namespace_matches_pattern(chat_id):
    """Property 3c: the namespace always matches ``sprout/{chat_id}/long_term``.

    The root and trailing segments are constant and the chat id appears verbatim
    as the single variable middle segment.

    Validates: Requirement 6.1
    """
    namespace = derive_long_term_namespace(chat_id)

    assert namespace == f"{NAMESPACE_ROOT}/{chat_id}/{LONG_TERM_SEGMENT}"

    parts = namespace.split("/")
    assert parts[0] == NAMESPACE_ROOT
    assert parts[-1] == LONG_TERM_SEGMENT
    # For a well-formed (no-"/") chat id there are exactly three segments and the
    # middle one is the chat id itself.
    assert parts[1:-1] == [chat_id]
    assert chat_id_from_namespace(namespace) == chat_id


@given(chat_id_a=_CHAT_IDS, chat_id_b=_CHAT_IDS)
@PBT_SETTINGS
def test_distinct_chat_ids_produce_distinct_namespaces(chat_id_a, chat_id_b):
    """Property 3b: distinct chat ids never share a namespace (injectivity).

    This is the isolation guarantee — user A's namespace can never equal user
    B's, so retrieval scoped to one user cannot surface another's records.

    Validates: Requirement 6.2
    """
    namespace_a = derive_long_term_namespace(chat_id_a)
    namespace_b = derive_long_term_namespace(chat_id_b)

    if chat_id_a == chat_id_b:
        assert namespace_a == namespace_b
    else:
        assert namespace_a != namespace_b


@given(chat_ids=st.lists(_CHAT_IDS, min_size=0, max_size=50))
@PBT_SETTINGS
def test_no_collisions_across_a_population_of_chat_ids(chat_ids):
    """Property 3b (set form): the mapping is injective over any population.

    The number of distinct namespaces equals the number of distinct chat ids —
    equivalently, the derivation introduces no collisions.

    Validates: Requirement 6.2
    """
    namespaces = {derive_long_term_namespace(cid) for cid in chat_ids}
    assert len(namespaces) == len(set(chat_ids))
