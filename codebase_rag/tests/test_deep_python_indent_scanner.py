"""A Python file nested past 510 levels indexes without crashing (issue #3831).

tree-sitter-python 0.25.0's external scanner saves its state into the
1,024-byte buffer the tree-sitter runtime lends it: a flag, a count, one byte
per open string, then two bytes per indentation level. It checks for room
before an indent's two bytes rather than for both, so with one string open
and 511 levels of indentation it writes a 1,025th byte, over the low byte of
the stack pointer the parser keeps right after the buffer. The parse then
pushes onto a stack at the wrong address and the process segfaults, taking
the whole index with it. Upstream's master has the same check.

The parser loader hands py-tree-sitter a copy of the grammar whose scanner
saves into a larger buffer, of which the runtime gets the first 1,024 bytes
(`bounded_scanner` in rust/cgr_graph_rs/src/grammars). The runtime cannot
keep more levels than that, so past them the grammar loses the nesting with
or without the fix (CPython refuses more than 100 levels): the over-deep
function's own body is not compared, but every other function in the file
must index exactly as it does when the nesting is shallow.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag import crate_grammars
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.parser_loader import load_parsers

# Well past the 510 nested `if`s (511 indentation levels with the innermost
# line) at which one open string makes the scanner write the extra byte; the
# issue's depth.
DEEP = 1200
SHALLOW = 2
_ROOT = str(Path(__file__).resolve().parents[2])

_INDEX = """
import json, sys
from pathlib import Path
from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _CapturingIngestor
repo = Path(sys.argv[1])
parsers, queries = load_parsers()
ingestor = _CapturingIngestor()
GraphUpdater(ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries,
             capture=resolve_capture([])).run(force=True)
prefix = repo.name + "."
print(json.dumps(sorted(
    (str(src).removeprefix(prefix), str(dst).removeprefix(prefix))
    for _, src, rel, _, dst in ingestor.rels
    if rel == cs.RelationshipType.CALLS.value
)))
"""

_PARSE = """
import sys
from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
parser = load_parsers()[0][cs.SupportedLanguage.PYTHON]
source = sys.stdin.buffer.read()
for _ in range(3):
    parser.parse(source)
print("parsed")
"""


def _nested(depth: int, innermost: str) -> str:
    # One space per level, so the nesting costs the scanner one saved indent
    # per `if`; the innermost line holds a string, which the scanner saves
    # its state inside.
    return (
        "def deep(x):\n"
        + "".join(" " * (level + 1) + f"if x == {level}:\n" for level in range(depth))
        + " " * (depth + 1)
        + innermost
        + "\n return 0\n"
    )


def _module(depth: int) -> str:
    return (
        "def helper():\n    return 1\n\n\n"
        "def other():\n    return 2\n\n\n"
        "def before():\n    return helper()\n\n\n"
        + _nested(depth, 'y = "deep"')
        + "\n\ndef after():\n    return other()\n"
    )


def _run(
    script: str, *args: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    # A child process, so a segfault fails the test instead of killing pytest.
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, *args],
        input=stdin,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": _ROOT},
        check=False,
        timeout=600,
    )


def _calls(tmp_path: Path, depth: int) -> list[list[str]]:
    repo = tmp_path / f"depth{depth}"
    repo.mkdir()
    (repo / "a.py").write_text(_module(depth), encoding=cs.ENCODING_UTF8)
    result = _run(_INDEX, str(repo))
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-3000:]
    calls: list[list[str]] = json.loads(result.stdout.decode().splitlines()[-1])
    return calls


def test_indexing_python_nested_past_the_scanner_buffer_completes(
    tmp_path: Path,
) -> None:
    shallow = _calls(tmp_path, SHALLOW)
    assert shallow == [["a.after", "a.other"], ["a.before", "a.helper"]]

    assert _calls(tmp_path, DEEP) == shallow


def test_the_loaders_python_parser_survives_the_deep_source() -> None:
    # The editing tools parse with `load_parsers()`'s parser directly.
    result = _run(_PARSE, stdin=_nested(DEEP, 'print("deep")').encode())

    assert result.returncode == 0, result.stderr.decode(errors="replace")[-3000:]
    assert result.stdout.decode().split() == ["parsed"]


def _package_language() -> Language:
    import tree_sitter_python

    return Language(tree_sitter_python.language())


@pytest.mark.parametrize(
    "source",
    [
        _module(SHALLOW),
        # Just short of the levels the runtime can keep with a string open:
        # nothing to cut, so nothing may change.
        _nested(509, 'print("deep")'),
        'x = f"{y!r:>{w}}" + """a\n  b""" + rb"\\d"\n',
        "class A:\n\tdef f(self):\n\t\treturn [\n  1]\n",
    ],
    ids=["module", "509-levels", "strings", "tabs-and-brackets"],
)
def test_the_bounded_grammar_parses_as_the_package_does(source: str) -> None:
    # Negative: below the buffer's end the copy is the grammar itself.
    parser = load_parsers()[0][cs.SupportedLanguage.PYTHON]
    bounded = parser.parse(source.encode()).root_node
    package = Parser(_package_language()).parse(source.encode()).root_node

    assert str(bounded) == str(package)
    assert bounded.has_error == package.has_error


def test_the_loader_parses_python_with_the_bounded_copy() -> None:
    language = load_parsers()[0][cs.SupportedLanguage.PYTHON].language

    assert language is not None
    assert language != _package_language()
    assert crate_grammars.grammar_fingerprint(language) == (
        crate_grammars.grammar_fingerprint(_package_language())
    )


def test_an_extension_without_the_bound_keeps_the_package_grammar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tree_sitter_python

    class _OldExtension:
        pass

    monkeypatch.setattr(crate_grammars, "rust_extension", _OldExtension)
    loader = crate_grammars.bounded_scanner_loader(tree_sitter_python.language)

    assert loader is not None
    assert Language(loader()) == _package_language()


def test_a_grammar_the_extension_cannot_bound_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: the extension declines (another ABI, no scanner), so the
    # package's grammar is used as it is.
    import tree_sitter_python

    class _DecliningExtension:
        @staticmethod
        def bounded_scanner(_language: Language) -> None:
            return None

    monkeypatch.setattr(crate_grammars, "rust_extension", _DecliningExtension)
    loader = crate_grammars.bounded_scanner_loader(tree_sitter_python.language)

    assert loader is not None
    assert Language(loader()) == _package_language()


def test_the_extension_declines_a_language_handed_out_as_an_int() -> None:
    # Old bindings returned the language's address; there is no capsule to
    # read a grammar from, so the loader must keep what it was given.
    bound = getattr(rust_extension(), cs.EG_RUST_BOUNDED_SCANNER)

    assert bound(42) is None


def test_no_loader_stays_no_loader() -> None:
    assert crate_grammars.bounded_scanner_loader(None) is None
