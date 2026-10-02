"""A `#match?` over bytes that are not UTF-8 must not take the process down.

py-tree-sitter evaluates `#match?` by handing the captured node's bytes to
`re.search` through a strict UTF-8 conversion. On an undecodable byte that
conversion fails, the error is left set and evaluation carries on, so the call
either raises a SystemError or corrupts the interpreter and segfaults.
tree-sitter-sql's highlights query tests `(literal)` that way and modifier
extraction runs it over every function header, so one bad byte in a SQL string
literal could abort the file's ingest or kill the indexer outright.

Every probe that feeds such bytes runs in a subprocess, so a regression fails
here with the child's exit status instead of killing the test run.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.query_predicates import (
    compile_query,
    query_captures,
    query_predicate,
    rewrite_match_predicates,
)

tree_sitter = pytest.importorskip("tree_sitter")

_REPO = Path(__file__).resolve().parents[2]

_CLEAN = b"SELECT 'a?', 1, 2.5 FROM t;\n"
_DIRTY = b"SELECT 'a\xad', 1, 2.5 FROM t;\n"
_FUNCTIONS = (
    b"CREATE FUNCTION good() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\n\n"
    b"CREATE FUNCTION bad(a text DEFAULT 'x\xad') RETURNS int AS $$ SELECT 1; $$"
    b" LANGUAGE sql;\n\n"
    b"CREATE FUNCTION after() RETURNS int AS $$ SELECT 2; $$ LANGUAGE sql;\n"
)

_PROBE = """
import json, sys
from pathlib import Path
from unittest.mock import MagicMock

from loguru import logger

logger.remove()
errors = []
logger.add(lambda message: errors.append(message.record["message"]), level="ERROR")

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import get_query_cursor, sorted_captures

repo, clean, dirty, functions = Path(sys.argv[1]), *map(bytes.fromhex, sys.argv[2:])
parsers, queries = load_parsers()
parser = parsers[cs.SupportedLanguage.SQL]
query = queries[cs.SupportedLanguage.SQL][cs.QUERY_HIGHLIGHTS]


def report(stage, value):
    print(json.dumps([stage, value]), flush=True)


for stage, source in (("clean", clean), ("dirty", dirty)):
    found = sorted_captures(get_query_cursor(query), parser.parse(source).root_node)
    report(
        stage,
        sorted(
            [name, node.start_byte, node.end_byte]
            for name, nodes in found.items()
            for node in nodes
        ),
    )

(repo / "q.sql").write_bytes(functions)
ingestor = MagicMock()
GraphUpdater(ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries).run()
report(
    "functions",
    sorted(
        call.args[1]["qualified_name"]
        for call in ingestor.ensure_node_batch.call_args_list
        if call.args[0] == "Function"
    ),
)
report("errors", errors)
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    pytest.importorskip("tree_sitter_sql")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(tmp_path_factory.mktemp("sql_repo")),
            _CLEAN.hex(),
            _DIRTY.hex(),
            _FUNCTIONS.hex(),
        ],
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=600,
        check=False,
    )
    stages = dict(json.loads(line) for line in result.stdout.splitlines())
    return {
        "returncode": result.returncode,
        "stages": stages,
        "stderr": result.stderr[-2000:],
    }


def test_an_undecodable_literal_does_not_crash_the_interpreter(
    probe: dict[str, Any],
) -> None:
    assert probe["returncode"] == 0, probe["stderr"]
    assert set(probe["stages"]) == {"clean", "dirty", "functions", "errors"}


def test_an_undecodable_literal_keeps_every_capture(probe: dict[str, Any]) -> None:
    """Same-length sources, so a lost or moved capture shows as a span change."""
    stages = probe["stages"]
    assert "dirty" in stages, probe["stderr"]
    assert stages["clean"], "the clean source produced no captures to compare"
    assert stages["dirty"] == stages["clean"]


def test_every_sql_function_survives_an_undecodable_literal_in_a_header(
    probe: dict[str, Any],
) -> None:
    stages = probe["stages"]
    assert "functions" in stages, probe["stderr"]
    names = {qn.rsplit(".", 1)[-1] for qn in stages["functions"]}
    assert {"good", "bad", "after"} <= names, names
    assert stages["errors"] == []


def _spans(captures: dict[str, list[Any]]) -> set[tuple[str, int, int]]:
    return {
        (name, node.start_byte, node.end_byte)
        for name, nodes in captures.items()
        for node in nodes
    }


def _evaluations(language: Any, source: str, tree: Any) -> tuple[set, set, set]:
    """Captures natively, through the rewrite, and with the predicate ignored.

    The third is the control: py-tree-sitter skips a generic predicate when no
    callback is passed, so a rewritten query run bare shows what the source
    would capture if the predicate were never evaluated.
    """
    root = tree.root_node
    native = tree_sitter.QueryCursor(tree_sitter.Query(language, source))
    rewritten = tree_sitter.QueryCursor(compile_query(language, source))
    ignored = tree_sitter.QueryCursor(
        tree_sitter.Query(language, rewrite_match_predicates(source))
    )
    return (
        _spans(native.captures(root)),
        _spans(query_captures(rewritten, root)),
        _spans(ignored.captures(root)),
    )


_PY_SOURCE = b"""import os

CONSTANT = 1
lower = 2

# TODO first
# second
def Build():
    # plain
    # also plain
    return Widget(CONSTANT, lower, "x")

# TODO only
def run():
    print()
    print("y")
"""

_VARIANT_QUERIES = {
    "match": '((identifier) @x (#match? @x "^[A-Z]"))',
    "not-match": '((identifier) @x (#not-match? @x "^[A-Z]"))',
    "match-every-node": '((comment)+ @c (#match? @c "TODO"))',
    "any-match": '((comment)+ @c (#any-match? @c "TODO"))',
    "not-match-every-node": '((comment)+ @c (#not-match? @c "TODO"))',
    "any-not-match": '((comment)+ @c (#any-not-match? @c "TODO"))',
    "empty-capture": (
        "(call function: (identifier) @f arguments: (argument_list (string)? @s)"
        ' (#any-match? @s "x"))'
    ),
}


@pytest.mark.parametrize("variant", sorted(_VARIANT_QUERIES))
def test_a_rewritten_match_predicate_agrees_with_native_evaluation(
    variant: str,
) -> None:
    tree_sitter_python = pytest.importorskip("tree_sitter_python")
    language = tree_sitter.Language(tree_sitter_python.language())
    tree = tree_sitter.Parser(language).parse(_PY_SOURCE)
    native, rewritten, ignored = _evaluations(language, _VARIANT_QUERIES[variant], tree)
    assert native != ignored, "the source does not exercise this predicate"
    assert rewritten == native


def _corpus_sources() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location(
        "_build_corpus", _REPO / "fuzz" / "build_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SOURCES


# Appended where the corpus program never reaches the grammar's `#match?`: the
# SQL program has no literal, the Go one calls no builtin.
_PREDICATE_REACHING = {
    cs.SupportedLanguage.SQL: "\nSELECT 'a', 1, 2.5;\n",
    cs.SupportedLanguage.GO: '\nfunc extra() int { return len("x") + other() }\n',
}


def test_every_highlights_query_agrees_with_native_evaluation() -> None:
    from codebase_rag.parser_loader import _highlights_source, load_parsers

    parsers, queries = load_parsers()
    sources = _corpus_sources()
    with_match: set[str] = set()
    exercised: set[str] = set()
    mismatched: list[str] = []
    for lang_name in sorted(parsers):
        source = _highlights_source(lang_name)
        if rewrite_match_predicates(source) == source:
            continue
        with_match.add(lang_name)
        program = sources[lang_name] + _PREDICATE_REACHING.get(lang_name, "")
        tree = parsers[lang_name].parse(program.encode())
        language = queries[lang_name][cs.QUERY_LANGUAGE]
        native, rewritten, ignored = _evaluations(language, source, tree)
        if rewritten != native:
            mismatched.append(lang_name)
        if ignored != native:
            exercised.add(lang_name)
    assert not mismatched
    assert {cs.SupportedLanguage.PYTHON, cs.SupportedLanguage.SQL} <= with_match
    assert exercised == with_match, "a grammar's predicate decided nothing here"


_TEXT_X = SimpleNamespace(text=b"x")


@pytest.mark.parametrize(
    ("args", "captures"),
    [
        ([("c", "capture")], {"c": [_TEXT_X]}),
        ([("c", "capture"), ("x", "string"), ("x", "string")], {"c": [_TEXT_X]}),
        ([("x", "string"), ("c", "capture")], {"c": [_TEXT_X]}),
        ([("c", "capture"), ("(", "string")], {"c": [_TEXT_X]}),
        ([("c", "capture"), ("x", "string")], {"c": [object()]}),
    ],
    ids=["one-argument", "three-arguments", "swapped", "bad-regex", "no-text"],
)
def test_a_malformed_match_predicate_is_never_satisfied(
    args: list[tuple[str, str]], captures: dict[str, list[Any]]
) -> None:
    """The callback runs inside the binding, so it answers rather than raises.

    An exception escaping it would leave the same pending error behind that
    the rewrite exists to avoid.
    """
    predicate = f"{cs.QUERY_PREDICATE_REWRITE_PREFIX}match?"
    assert query_predicate(predicate, args, 0, captures) is False


@pytest.mark.parametrize(
    ("name", "regex", "expected"),
    [
        ("match?", "^a", True),
        ("match?", "^[a-z]+$", False),
        ("not-match?", "^[a-z]+$", True),
        ("any-not-match?", "^[a-z]+$", True),
    ],
)
def test_undecodable_text_is_matched_with_its_bad_bytes_replaced(
    name: str, regex: str, expected: bool
) -> None:
    """`a\\xad` is tested as `a\\ufffd`: its decodable prefix still matches, and
    the replaced byte is a character no ASCII class accepts."""
    predicate = f"{cs.QUERY_PREDICATE_REWRITE_PREFIX}{name}"
    args = [("c", "capture"), (regex, "string")]
    dirty = SimpleNamespace(text=b"a\xad")
    assert query_predicate(predicate, args, 0, {"c": [dirty]}) is expected


@pytest.mark.parametrize("predicate", ["lua-match?", "has-ancestor?", "match?"])
def test_a_predicate_it_does_not_own_is_left_alone(predicate: str) -> None:
    """Without a callback py-tree-sitter skipped every generic predicate."""
    args = [("c", "capture"), ("^y", "string")]
    assert query_predicate(predicate, args, 0, {"c": [_TEXT_X]}) is True


@pytest.mark.parametrize(
    "name", ["match?", "not-match?", "any-match?", "any-not-match?"]
)
@pytest.mark.parametrize("opening", ["(", "(\n  "])
def test_every_match_predicate_spelling_is_rewritten(name: str, opening: str) -> None:
    source = f'((identifier) @x {opening}#{name} @x "^A"))'
    prefix = cs.QUERY_PREDICATE_REWRITE_PREFIX
    expected = f'((identifier) @x {opening}#{prefix}{name} @x "^A"))'
    assert rewrite_match_predicates(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        '((identifier) @x (#lua-match? @x "^A"))',
        '((identifier) @x (#vim-match? @x "^A"))',
        '((identifier) @x (#matches? @x "^A"))',
        '((identifier) @x (#eq? @x "match?"))',
        '((identifier) @x (#any-of? @x "#match? " "y"))',
    ],
    ids=["lua-match", "vim-match", "matches", "eq", "any-of"],
)
def test_a_lookalike_predicate_is_not_rewritten(source: str) -> None:
    assert rewrite_match_predicates(source) == source


_OWNER = Path("codebase_rag") / "query_predicates.py"
_SCANNED_DIRS = ("codebase_rag", "fuzz", "evals", "optimize", "scripts")


def _bypasses(source: str) -> list[int]:
    """Lines that build a tree-sitter Query or run one without the callback.

    Only a file that touches tree-sitter is in scope, so an unrelated
    `.matches()` elsewhere is not mistaken for a query run. A file touches it by
    importing from `tree_sitter` or importing `get_query_cursor`.
    """
    tree = ast.parse(source)
    query_names: set[str] = set()
    module_names: set[str] = set()
    in_scope = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if node.module == "tree_sitter" or "get_query_cursor" in names:
                in_scope = True
            if node.module == "tree_sitter":
                query_names |= {
                    a.asname or a.name for a in node.names if a.name == "Query"
                }
        elif isinstance(node, ast.Import):
            module_names |= {
                a.asname or a.name for a in node.names if a.name == "tree_sitter"
            }
    if not (in_scope or module_names):
        return []
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"captures", "matches"}:
            lines.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in query_names:
            lines.append(node.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "Query"
            and isinstance(func.value, ast.Name)
            and func.value.id in module_names
        ):
            lines.append(node.lineno)
    return sorted(lines)


def test_the_scan_sees_every_way_to_build_or_run_a_query() -> None:
    planted = textwrap.dedent(
        """
        import tree_sitter as ts
        from tree_sitter import Query, Query as Q
        a = Query(lang, src)
        b = Q(lang, src)
        c = ts.Query(lang, src)
        d = cursor.captures(node)
        e = cursor.matches(node)
        f = query_captures(cursor, node)
        g = compile_query(lang, src)
        """
    )
    assert _bypasses(planted) == [4, 5, 6, 7, 8]
    via_helper = (
        "from x.utils import get_query_cursor\nc = get_query_cursor(q)\nc.captures(n)\n"
    )
    assert _bypasses(via_helper) == [3]


def test_the_scan_leaves_files_that_never_touch_tree_sitter() -> None:
    unrelated = "import re\nfound = component.matches(name)\n"
    assert _bypasses(unrelated) == []


def test_no_query_is_built_or_run_outside_the_predicate_module() -> None:
    """A query built or run elsewhere would silently drop its `#match?` tests."""
    paths = [p for d in _SCANNED_DIRS for p in (_REPO / d).rglob("*.py")]
    paths += list(_REPO.glob("*.py"))
    offenders = {}
    for path in paths:
        rel = path.relative_to(_REPO)
        if rel == _OWNER or "tests" in rel.parts or "node_modules" in rel.parts:
            continue
        if lines := _bypasses(path.read_text(encoding="utf-8")):
            offenders[str(rel)] = lines
    assert len(paths) > 100, "the scan found too few files to mean anything"
    assert not offenders
