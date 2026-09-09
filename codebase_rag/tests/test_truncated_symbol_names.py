"""Issue #1810: a bad byte inside an identifier truncates the indexed name.

tree-sitter treats an invalid byte as a token boundary, so the `name` node
covers only the bytes on one side of it and the extractor decodes that
shortened node without error. `calculate_total` is indexed as `calculate_t`
with nothing raised and nothing logged. The production remedy is a WARNING
keyed on the name's own bytes; these tests pin both directions of that.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture(scope="module")
def parsers_and_queries() -> tuple[dict, dict]:
    return load_parsers()


def _index(
    parsers_and_queries: tuple[dict, dict], filename: str, source: bytes
) -> tuple[set[str], list[str]]:
    """Index one file, returning (symbol names, #1810 warnings)."""
    parsers, queries = parsers_and_queries
    root = Path(tempfile.mkdtemp()) / "proj"
    root.mkdir()
    (root / filename).write_bytes(source)

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="WARNING")
    store = _StatefulIngestor()
    try:
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=True)
    finally:
        logger.remove(sink)

    names = {
        str(uid)
        for (label, uid) in store.nodes
        if label
        in (
            cs.NodeLabel.FUNCTION.value,
            cs.NodeLabel.METHOD.value,
            cs.NodeLabel.CLASS.value,
        )
    }
    return names, [c for c in captured if "#1810" in c]


# One case per language that TRUNCATES rather than drops the symbol. cpp and
# lua are deliberately absent: measured, they remove the definition entirely
# and invent no wrong-named symbol, so there is no truncated name to warn
# about and a missing entry is the honest failure.
TRUNCATING = {
    "python": ("a.py", b"class Greeter:\n    def greet(self):\n        return 1\n"),
    "javascript": ("a.js", b"class Greeter { greet() { return 1; } }\n"),
    "java": (
        "a.java",
        b"public class Greeter { public String greet(String n) { return n; } }\n",
    ),
    "rust": (
        "a.rs",
        b"pub struct Greeter;\nimpl Greeter {\n"
        b"    pub fn greet(&self) -> i32 { 1 }\n}\n",
    ),
}


@pytest.mark.parametrize("language", sorted(TRUNCATING))
def test_a_truncated_name_is_reported(
    parsers_and_queries: tuple[dict, dict], language: str
) -> None:
    filename, clean = TRUNCATING[language]
    assert b"greet" in clean
    dirty = clean.replace(b"greet", b"gr\xffeet", 1)

    _clean_names, clean_warnings = _index(parsers_and_queries, filename, clean)
    dirty_names, dirty_warnings = _index(parsers_and_queries, filename, dirty)

    assert not clean_warnings, f"{language}: warned on clean source"
    assert dirty_warnings, (
        f"{language}: a truncated name reached the graph with no warning; "
        f"indexed {sorted(dirty_names)}"
    )


@pytest.mark.parametrize(
    "identifier",
    ["alpha", "café", "élan", "函数"],
    ids=["ascii", "latin_accent", "leading_accent", "cjk"],
)
def test_valid_non_ascii_identifiers_are_not_reported(
    parsers_and_queries: tuple[dict, dict], identifier: str
) -> None:
    """The control that decides whether this is usable on real codebases.

    A naive "is the name ASCII" check passes every test one would think to
    write and then fires on every non-English codebase.
    """
    source = f"def {identifier}():\n    return 1\n".encode()
    names, warnings = _index(parsers_and_queries, "a.py", source)

    assert not warnings, f"false alarm on the valid identifier {identifier!r}"
    assert f"proj.a.{identifier}" in names


@pytest.mark.parametrize(
    ("position", "source"),
    [
        (
            "module_header",
            b"# Copyright \xa9 2003 Sun Microsystems\ndef alpha():\n    return 1\n",
        ),
        ("body_comment", b"def alpha():\n    # \xa9 Sun\n    return 1\n"),
    ],
)
def test_a_bad_byte_that_damages_no_name_is_not_reported(
    parsers_and_queries: tuple[dict, dict], position: str, source: bytes
) -> None:
    """Positions a bad byte can occupy without truncating a name.

    Only `module_header` was covered originally, and it is the ONE position no
    definition node spans -- so the test passed while the check fired on a bad
    byte in a body, which is exactly the real-world shape. Across 105,352 real
    source files, all 165 invalid ones are latin-1 punctuation in a copyright
    header or an author's name, i.e. these positions and not the defect.

    A bad byte inside a STRING LITERAL or DOCSTRING is deliberately absent:
    it makes the call-processing pass raise `UnicodeDecodeError` and abandon
    the file, which is the separate open defect #1797 (fix in flight as PR
    #1812) and is caught by conftest's per-file-pass guard. Add those two
    positions here once #1812 lands; this check already handles them (verified
    by driving `warn_if_name_truncated` directly), but the file never survives
    ingest long enough to prove it end to end.
    """
    names, warnings = _index(parsers_and_queries, "a.py", source)

    assert not warnings, f"false alarm on a bad byte in a {position}"
    assert "proj.a.alpha" in names, "the symbol indexed under its real name"


def test_a_name_bound_by_an_enclosing_field_is_reported(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """A TS class-field arrow is named by its ENCLOSING field definition.

    `_js_ts_field_member_name` reads the binding from the parent, so the
    identifier sits ABOVE the node handed to the check. Searching only
    downwards missed it: `Greeter.eet` reached the graph with no warning while
    every other shape was covered, which is the shape a downward-only search
    cannot see by construction.

    JS is deliberately not covered here: it does not index class-field arrows
    as methods at all (verified -- the clean control indexes only `Greeter`),
    so there is no truncated name to report.
    """
    clean = b"class Greeter {\n  greet = (n: string) => n;\n}\n"
    dirty = b"class Greeter {\n  gr\xffeet = (n: string) => n;\n}\n"

    clean_names, clean_warnings = _index(parsers_and_queries, "a.ts", clean)
    dirty_names, dirty_warnings = _index(parsers_and_queries, "a.ts", dirty)

    assert "proj.a.Greeter.greet" in clean_names, "control: the arrow is indexed"
    assert not clean_warnings, "false alarm on a clean field arrow"
    assert dirty_warnings, (
        f"a field-bound name truncated with no warning; indexed {sorted(dirty_names)}"
    )


def test_one_bad_byte_does_not_warn_once_per_enclosing_definition(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """Nesting must not multiply the message.

    Keyed on the enclosing definition's span, one bad byte in a deeply nested
    body produced a warning per level (4 here) while every symbol was indexed
    correctly.
    """
    source = (
        b"class Outer:\n"
        b"    class Mid:\n"
        b"        class Inner:\n"
        b'            def deep(self):\n                return "\xa9"\n'
    )

    names, warnings = _index(parsers_and_queries, "a.py", source)

    assert not warnings, f"{len(warnings)} warning(s) for one harmless bad byte"
    assert "proj.a.Outer.Mid.Inner" in names


def test_a_replacement_character_inside_a_name_is_reported(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """The second damage shape, which adjacency alone cannot see.

    Lua keeps the bad byte in an ERROR node BETWEEN two identifiers of a
    dotted name, and #1797's fix decodes the whole expression with
    errors="replace", so the name survives as `Greeter.gr\ufffdeet` rather
    than being truncated. Nothing is adjacent to a shortened span, so the
    adjacency test finds nothing; the replacement character in the name is
    itself the signal.

    Safe because no legitimate identifier contains U+FFFD in any language --
    it is only ever there because a decode put it there.
    """
    clean = b"function Greeter.greet(n) return n end\n"
    dirty = b"function Greeter.gr\xffeet(n) return n end\n"

    clean_names, clean_warnings = _index(parsers_and_queries, "a.lua", clean)
    _dirty_names, dirty_warnings = _index(parsers_and_queries, "a.lua", dirty)

    assert "proj.a.Greeter.greet" in clean_names, "control: the clean name indexes"
    assert not clean_warnings, "false alarm on clean Lua"
    assert dirty_warnings, "a name carrying U+FFFD reached the graph unreported"


def test_a_valid_multibyte_character_beside_a_name_is_not_reported(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """A one-byte adjacency probe calls valid source corrupt.

    Under error recovery `def alpha\u00a9():` puts the two bytes c2 a9 next to the
    name. Together they are a valid `\u00a9`; NEITHER decodes alone, so probing a
    single byte either side reports a truncated name for well-formed source.
    The probe is a window the width of the longest UTF-8 sequence, with the
    partial character at the cut edge trimmed before decoding.
    """
    source = "def alpha\u00a9():\n    return 1\n".encode()

    _names, warnings = _index(parsers_and_queries, "a.py", source)

    assert not warnings, "false alarm on a valid multi-byte character"


def test_a_synthesized_name_still_resolves_to_its_source_span(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """C# destructors are ingested as `~Greeter` while the source leaf is bare.

    The span lookup matches the extracted name against the tree, so a name the
    ingestor SYNTHESIZED finds nothing and the check returns before inspecting
    anything. A leading sigil is the general shape of that, so it is stripped
    before the lookup.
    """
    from codebase_rag.parsers.utils import _name_span

    parsers, _queries = parsers_and_queries
    source = b"class Greeter {\n  ~Gr\xffeeter() { }\n}\n"
    tree = parsers[cs.SupportedLanguage.CSHARP].parse(source)

    stack = [tree.root_node]
    destructor = None
    while stack:
        node = stack.pop()
        if "destructor" in node.type:
            destructor = node
            break
        stack.extend(node.children)
    assert destructor is not None, "fixture did not produce a destructor node"

    # The grammar keeps the LEADING fragment here, so the ingested name is `~Gr`.
    assert _name_span(destructor, "~Gr") is not None, (
        "a synthesized name must still resolve to the span it came from"
    )


def test_the_name_nodes_own_bytes_cannot_detect_this() -> None:
    """Why the check probes ADJACENT bytes rather than the name's own.

    tree-sitter excludes the bad byte from the name node, so its bytes decode
    cleanly in the corrupt and the clean case alike. A check on them detects
    nothing -- the opposite failure to the definition-span version, and just
    as invisible.
    """
    from codebase_rag.parser_loader import load_parsers

    parsers, _ = load_parsers()
    parser = parsers[cs.SupportedLanguage.PYTHON]

    corrupt = parser.parse(b"def al\xffpha():\n    return 1\n")
    definition = next(
        n for n in corrupt.root_node.children if n.type == "function_definition"
    )
    name_node = definition.child_by_field_name("name")

    assert name_node.text == b"pha", "the name node excludes the bad byte"
    name_node.text.decode(cs.ENCODING_UTF8)  # decodes cleanly: detects nothing


def test_the_containment_oracle_cannot_detect_this() -> None:
    """Why the check asks the SOURCE to round-trip, pinned as a test.

    The obvious oracle -- does the extracted name appear in the bytes it came
    from -- is satisfied BY this corruption, because the truncated name is a
    substring of the corrupt bytes. It returns True on the exact defect it
    would be written for and can never fire. First thing a future reader will
    try to "simplify" this into.
    """
    raw = b"al\xffpha"
    truncated = b"pha"

    assert truncated in raw, "containment passes on the defect itself"

    with pytest.raises(UnicodeDecodeError):
        raw.decode(cs.ENCODING_UTF8)
