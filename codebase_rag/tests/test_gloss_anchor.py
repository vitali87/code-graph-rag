"""The text-quote anchor of a Gloss (issue #1808, stage five).

The quote is a digest of the definition's own text with its NAME masked and
whitespace normalised, so a pure rename or a reformat leaves it unchanged
while any edit to the body flips it. The prefix and suffix digest the
non-blank lines around the definition and only break ties between
definitions whose bodies are identical.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.gloss_anchor import text_anchor

SRC = """import os


def helper(x):
    return x + 1


def run(v):
    y = helper(v)
    return y * 2


def tail():
    pass
"""


def _anchor(source: str, name: str | None, start: int, end: int):
    anchor = text_anchor(source, name, start, end)
    assert anchor is not None
    return anchor


def test_a_pure_rename_keeps_the_quote() -> None:
    before = _anchor(SRC, "run", 8, 10)
    renamed = SRC.replace("def run(v):", "def execute(v):")
    after = _anchor(renamed, "execute", 8, 10)
    assert after.quote == before.quote
    assert before.quote.startswith(cs.ANCHOR_QUOTE_VERSION)


def test_a_body_edit_flips_the_quote() -> None:
    before = _anchor(SRC, "run", 8, 10)
    edited = SRC.replace("return y * 2", "return y * 3")
    assert _anchor(edited, "run", 8, 10).quote != before.quote


def test_reformatting_keeps_the_quote() -> None:
    """Whitespace is not part of the quote: spacing, and a blank line inside
    the body (which grows the span by one), leave it alone. A change to a
    token boundary is an edit, not a reformat."""
    before = _anchor(SRC, "run", 8, 10)
    spaced = SRC.replace("y = helper(v)", "y   =   helper(v)")
    assert _anchor(spaced, "run", 8, 10).quote == before.quote
    blank = SRC.replace("    y = helper(v)\n", "    y = helper(v)\n\n")
    assert _anchor(blank, "run", 8, 11).quote == before.quote
    split_token = SRC.replace("helper(v)", "helper( v )")
    assert _anchor(split_token, "run", 8, 10).quote != before.quote


def test_the_name_is_masked_as_a_whole_token_only() -> None:
    # `run` inside `rerun` is not the definition's name.
    src = SRC.replace("y = helper(v)", "y = rerun(v)")
    with_name = _anchor(src, "run", 8, 10)
    renamed = src.replace("def run(v):", "def go(v):")
    assert _anchor(renamed, "go", 8, 10).quote == with_name.quote
    # And masking is what makes them equal: an unmasked digest differs.
    assert _anchor(src, None, 8, 10).quote != _anchor(renamed, None, 8, 10).quote


def test_prefix_and_suffix_take_the_neighbouring_non_blank_lines() -> None:
    anchor = _anchor(SRC, "run", 8, 10)
    # Moving the definition keeps its quote but changes its neighbours.
    moved = "import os\n\n\ndef run(v):\n    y = helper(v)\n    return y * 2\n\n\ndef helper(x):\n    return x + 1\n\n\ndef tail():\n    pass\n"
    other = _anchor(moved, "run", 4, 6)
    assert other.quote == anchor.quote
    assert other.prefix != anchor.prefix
    assert other.suffix != anchor.suffix


def test_a_definition_at_either_end_of_the_file_has_a_context() -> None:
    first = _anchor(SRC, "os", 1, 1)
    last = _anchor(SRC, "tail", 13, 14)
    assert first.prefix.startswith(cs.ANCHOR_QUOTE_VERSION)
    assert last.suffix.startswith(cs.ANCHOR_QUOTE_VERSION)


def test_a_span_outside_the_source_is_no_anchor() -> None:
    assert text_anchor(SRC, "run", 8, 99) is None
    assert text_anchor(SRC, "run", 0, 3) is None
    assert text_anchor("", "run", 1, 1) is None
