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


def test_a_bad_byte_outside_a_name_is_not_reported(
    parsers_and_queries: tuple[dict, dict],
) -> None:
    """The measured real-world case, and why this is not a per-file check.

    Across 105,352 real source files (Rust crates, Go modules, the macOS SDK,
    Homebrew, site-packages), 165 are not valid UTF-8 and NONE corrupts a
    symbol: every one is latin-1 punctuation in a copyright header or an
    author's name. A file-level signal would have fired on all 165 and been
    wrong every time; this one fires on none of them.
    """
    source = b"# Copyright \xa9 2003 Sun Microsystems\ndef alpha():\n    return 1\n"

    names, warnings = _index(parsers_and_queries, "a.py", source)

    assert not warnings, "warned on a bad byte that damages no name"
    assert "proj.a.alpha" in names, "the symbol indexed under its real name"


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
