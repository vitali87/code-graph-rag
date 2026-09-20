"""`safe_decode_text` must provide both guarantees its name asserts (#1811).

The name reads as "this is the decode that handles bad input". Until #1797
that was half true: "safe" covered only the None handling, and the decode
underneath was strict. A bad byte in a Python decorator raised
`UnicodeDecodeError`, the per-file handler in `graph_updater` caught it and
abandoned the file, and every definition in that file vanished from the graph
-- in the repo's primary language, through a function whose name said the
case was handled.

#1797 made the decode total. #1811 is about keeping the name honest: with
~550 call sites, a name asserting a guarantee should be checkable against the
implementation. These tests are that check, so a future strict decode fails
here rather than silently deleting files' worth of definitions.
"""

from __future__ import annotations

import pytest

from codebase_rag.parsers.utils import safe_decode_text


class _Node:
    """Minimal stand-in: `safe_decode_text` reads only `.text`."""

    def __init__(self, text: object) -> None:
        self.text = text


# A bad byte in a decorator -- the exact shape that motivated #1797.
_BAD_DECORATOR = b"@dec\xffrator"


def test_a_strict_decode_would_raise_on_this_input() -> None:
    """Calibrate the instrument first.

    Every "does not raise" assertion below is evidence only if the input can
    actually break a strict decode. Without this, a weakened fixture would
    make the suite pass for the wrong reason.
    """
    with pytest.raises(UnicodeDecodeError):
        _BAD_DECORATOR.decode("utf-8")


def test_undecodable_bytes_are_replaced_not_raised() -> None:
    """The guarantee the name implied but did not provide before #1797."""
    result = safe_decode_text(_Node(_BAD_DECORATOR))
    assert result is not None
    assert result.startswith("@dec")
    assert result.endswith("rator")
    # U+FFFD in place of the bad byte: the identifier is visibly mangled and
    # the rest of the file stays indexable.
    assert "�" in result


def test_wholly_invalid_bytes_still_decode() -> None:
    assert safe_decode_text(_Node(b"\xff\xfe\x00invalid")) is not None


def test_a_null_node_yields_none() -> None:
    """The guarantee "safe" originally named."""
    assert safe_decode_text(None) is None


def test_a_node_with_null_text_yields_none() -> None:
    assert safe_decode_text(_Node(None)) is None


def test_valid_utf8_is_unchanged() -> None:
    """The replacement decode must not disturb ordinary input."""
    assert safe_decode_text(_Node(b"ordinary_name")) == "ordinary_name"
    assert safe_decode_text(_Node("café".encode())) == "café"


def test_non_bytes_text_is_stringified() -> None:
    assert safe_decode_text(_Node(123)) == "123"


def test_the_decode_routes_through_the_shared_helper() -> None:
    """Pin the mechanism, not just the outcome.

    The assertions above would also pass if this function grew its own
    `errors="replace"` call. Several passes decode node text independently
    and each abandons the file on its own, so the requirement is that they
    all route through one helper -- fixing one and leaving the rest losing
    data on the same input is what #1797 was.
    """
    from codebase_rag.parsers import utils

    called: list[bytes] = []
    real = utils.decode_node_text

    def spy(raw: bytes) -> str:
        called.append(raw)
        return real(raw)

    # The decode is memoised, so use bytes no earlier test has decoded.
    unique = b"unique_for_the_spy_\xff"
    utils._cached_decode_bytes.cache_clear()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(utils, "decode_node_text", spy)
    try:
        assert safe_decode_text(_Node(unique)) is not None
    finally:
        monkey.undo()
        utils._cached_decode_bytes.cache_clear()

    assert called == [unique], "decode did not route through decode_node_text"
