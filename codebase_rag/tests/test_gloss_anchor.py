"""The text-quote anchor of a Gloss (issue #1808, stage five).

The quote is a digest of the definition's own text with its NAME masked and
whitespace normalised, so a pure rename or a reformat leaves it unchanged
while any edit to the body flips it. The prefix and suffix digest the
non-blank lines around the definition and only break ties between
definitions whose bodies are identical.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.gloss_anchor import ParsedSource, parse_source, text_anchor
from codebase_rag.parser_loader import load_parsers

_PARSERS, _ = load_parsers()


def _parsed(source: str) -> ParsedSource:
    return ParsedSource(source, parse_source(_PARSERS, Path("mod.py"), source))


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
    anchor = text_anchor(_parsed(source), name, start, end)
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
    """Whitespace between tokens is not part of the quote, wherever it is:
    spacing, a blank line inside the body (which grows the span by one), a
    space inside a call. The tokens are what the grammar sees."""
    before = _anchor(SRC, "run", 8, 10)
    spaced = SRC.replace("y = helper(v)", "y   =   helper( v )")
    assert _anchor(spaced, "run", 8, 10).quote == before.quote
    blank = SRC.replace("    y = helper(v)\n", "    y = helper(v)\n\n")
    assert _anchor(blank, "run", 8, 11).quote == before.quote


def test_a_string_literal_is_kept_exactly_as_written() -> None:
    """The bot review on PR #1966: a text-only reading masked the name and
    collapsed whitespace inside literals too, so a rename plus a literal
    edit was still followed. A literal is a token whose text is the body."""
    src = SRC.replace("return y * 2", 'return "run  x"')
    before = _anchor(src, "run", 8, 10)
    # The name inside the literal is not masked; the literal is the literal.
    renamed = src.replace("def run(v):", "def execute(v):")
    assert _anchor(renamed, "execute", 8, 10).quote == before.quote
    # A literal edit -- to the new name, or just its spacing -- flips it.
    literal_renamed = renamed.replace('"run  x"', '"execute  x"')
    assert _anchor(literal_renamed, "execute", 8, 10).quote != before.quote
    respaced = src.replace('"run  x"', '"run x"')
    assert _anchor(respaced, "run", 8, 10).quote != before.quote


def test_a_comment_is_not_part_of_the_quote() -> None:
    """The same reading as `anchor_hash`: formatters move comments."""
    before = _anchor(SRC, "run", 8, 10)
    commented = SRC.replace("    y = helper(v)", "    y = helper(v)  # run helper")
    assert _anchor(commented, "run", 8, 10).quote == before.quote


def test_without_a_parse_there_is_no_quote() -> None:
    assert text_anchor(ParsedSource(SRC, None), "run", 8, 10) is None


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
    assert text_anchor(_parsed(SRC), "run", 8, 99) is None
    assert text_anchor(_parsed(SRC), "run", 0, 3) is None
    assert text_anchor(_parsed(""), "run", 1, 1) is None


def test_an_identifier_inside_an_interpolation_is_code() -> None:
    """`f"{run()}"` names the definition in code, not in text: the rename
    keeps the quote (bot review on PR #1966), while the literal parts around
    the interpolation stay literal."""
    src = SRC.replace("return y * 2", 'return f"{run()} run"')
    before = _anchor(src, "run", 8, 10)
    renamed = src.replace("def run(v):", "def execute(v):").replace(
        'f"{run()} run"', 'f"{execute()} run"'
    )
    assert _anchor(renamed, "execute", 8, 10).quote == before.quote
    literal_too = renamed.replace('f"{execute()} run"', 'f"{execute()} execute"')
    assert _anchor(literal_too, "execute", 8, 10).quote != before.quote


def test_a_bare_dart_substitution_is_literal_text() -> None:
    """`"value: $run"` has no braces: the name is the string's own text, so a
    rename that also changes it is a body change; `"${run()}"` is code
    (bot review on PR #1966, second round)."""
    from codebase_rag.parser_loader import load_parsers

    parsers, _ = load_parsers()

    def dart(source: str, name: str) -> str:
        parsed = ParsedSource(source, parse_source(parsers, Path("mod.dart"), source))
        anchor = text_anchor(parsed, name, 1, 3)
        assert anchor is not None
        return anchor.quote

    bare = 'String run() {\n  return "value: $run";\n}\n'
    bare_renamed = bare.replace("run()", "execute()").replace("$run", "$execute")
    assert dart(bare, "run") != dart(bare_renamed, "execute")
    braced = 'String run() {\n  return "value: ${run()}";\n}\n'
    assert dart(braced, "run") == dart(braced.replace("run()", "execute()"), "execute")


_CPP_TEMPLATE = """template <typename T>
T run(T a) {
    return a;
}
"""


def _cpp_anchor(source: str, name: str | None, start: int, end: int):
    parsed = ParsedSource(source, parse_source(_PARSERS, Path("mod.cpp"), source))
    anchor = text_anchor(parsed, name, start, end)
    assert anchor is not None
    return anchor


def test_a_templated_cpp_rename_keeps_the_quote() -> None:
    """`template` matched C++'s `template_declaration` as a literal kind.

    Every leaf of a templated definition was then classified literal, so
    the definition's own name was never masked and a pure rename changed
    the quote -- the anchor could not follow it. The JS literal this entry
    exists for is `template_string` (Copilot, #1966).
    """
    before = _cpp_anchor(_CPP_TEMPLATE, "run", 1, 4)
    renamed = _CPP_TEMPLATE.replace("T run(T a)", "T execute(T a)")

    assert _cpp_anchor(renamed, "execute", 1, 4).quote == before.quote


def test_a_templated_cpp_body_edit_still_flips_the_quote() -> None:
    """The control: masking the name must not mask the body as well."""
    before = _cpp_anchor(_CPP_TEMPLATE, "run", 1, 4)
    edited = _CPP_TEMPLATE.replace("return a;", "return a + 1;")

    assert _cpp_anchor(edited, "run", 1, 4).quote != before.quote
