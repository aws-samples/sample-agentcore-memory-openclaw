"""Property-based tests for Telegram long-message splitting.

Feature: openclaw-telegram-agentcore, Property 2: Message splitting — all chunks
are within the length limit and concatenation preserves content.

Exercises the pure :func:`telegram_api.split_message` function that the Webhook
and Cron Lambdas rely on to break responses that exceed Telegram's
4096-character per-message limit into ordered chunks (design "Telegram
Webhook", Req 2.3). The properties below pin down the four guarantees the
design makes about the split output:

1. No chunk exceeds ``max_length`` characters.
2. Joining the chunks reconstructs the original text modulo the whitespace that
   is trimmed at the split points (non-whitespace content is preserved in
   order).
3. Empty input yields empty output.
4. Input already within the limit yields exactly one chunk equal to the input.

Validates: Requirements 2.3
"""

from __future__ import annotations

import os
import re
import sys

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Make the Lambda module (telegram_api.py) importable without packaging it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram_api import (  # noqa: E402  (path setup must precede the import)
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_message,
)

# Minimum 100 iterations per the design's PBT configuration.
PBT_SETTINGS = settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)

# Text drawn from a mix of ordinary characters plus the whitespace and sentence
# punctuation that drive the split boundaries, so paragraph/sentence/whitespace
# splits are all well represented in the generated space.
_TEXT_ALPHABET = st.characters(
    whitelist_categories=("L", "N", "P", "Zs"),
    whitelist_characters=" \n\t.!?",
)
_TEXT = st.text(alphabet=_TEXT_ALPHABET, min_size=0, max_size=20000)

# Non-whitespace remnant used to compare content across the split. Whitespace is
# intentionally collapsed because split_message trims whitespace at the cut
# points (rstrip/lstrip), which is the documented, allowed modulo.
_WHITESPACE = re.compile(r"\s+")


def _non_whitespace(text: str) -> str:
    """Return ``text`` with all whitespace removed for content comparison."""
    return _WHITESPACE.sub("", text)


@given(text=_TEXT, max_length=st.integers(min_value=1, max_value=4096))
@PBT_SETTINGS
def test_no_chunk_exceeds_max_length(text, max_length):
    """Property 2a: every produced chunk is within ``max_length`` characters.

    Checked against arbitrary limits in ``1..4096`` so the guarantee holds for
    the default 4096 limit and any smaller cap.

    Validates: Requirement 2.3
    """
    chunks = split_message(text, max_length=max_length)

    for chunk in chunks:
        assert len(chunk) <= max_length, (
            f"chunk of length {len(chunk)} exceeds max_length {max_length}"
        )


@given(text=_TEXT, max_length=st.integers(min_value=1, max_value=4096))
@PBT_SETTINGS
def test_concatenation_preserves_content(text, max_length):
    """Property 2b: joining chunks reconstructs the original non-whitespace text.

    ``split_message`` trims whitespace at split points, so equality is asserted
    on the whitespace-stripped content, which must be preserved exactly and in
    order.

    Validates: Requirement 2.3
    """
    chunks = split_message(text, max_length=max_length)

    reconstructed = _non_whitespace("".join(chunks))
    assert reconstructed == _non_whitespace(text)


@given(text=_TEXT, max_length=st.integers(min_value=1, max_value=4096))
@PBT_SETTINGS
def test_chunk_order_is_preserved(text, max_length):
    """Property 2b (order): each chunk's content appears in original order.

    A stronger companion to the concatenation property: the concatenated,
    whitespace-stripped chunks must be a prefix-preserving reconstruction, which
    the equality in :func:`test_concatenation_preserves_content` already implies
    for the full string. This test additionally guards that no chunk is empty
    (empty chunks would signal a splitting bug that stalls delivery).

    Validates: Requirement 2.3
    """
    chunks = split_message(text, max_length=max_length)

    for chunk in chunks:
        assert chunk != "", "split_message must not emit empty chunks"


@given(max_length=st.integers(min_value=1, max_value=4096))
@PBT_SETTINGS
def test_empty_input_produces_empty_output(max_length):
    """Property 2c: empty input yields an empty chunk list.

    Validates: Requirement 2.3
    """
    assert split_message("", max_length=max_length) == []


@given(
    data=st.data(),
    max_length=st.integers(min_value=1, max_value=4096),
)
@PBT_SETTINGS
def test_input_within_limit_produces_single_unchanged_chunk(data, max_length):
    """Property 2d: non-empty input <= ``max_length`` yields exactly one chunk.

    The single chunk must equal the input unchanged (no trimming is applied when
    the whole text already fits).

    Validates: Requirement 2.3
    """
    # Generate non-empty text that is guaranteed to fit within max_length.
    text = data.draw(
        st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=max_length)
    )

    chunks = split_message(text, max_length=max_length)

    assert chunks == [text]


@given(text=_TEXT)
@PBT_SETTINGS
def test_default_limit_matches_telegram_max(text):
    """The default limit is Telegram's 4096-character maximum (Req 2.3)."""
    chunks = split_message(text)

    for chunk in chunks:
        assert len(chunk) <= TELEGRAM_MAX_MESSAGE_LENGTH
