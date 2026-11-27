"""The Rust engine's Go skeleton against the Python it ports.

Phase 3's step 3 (docs/reports/PHASE3_GO.md, section 10) starts the Go
engine in `rust/cgr_graph_rs/src/engine/go`: Go files parsed on the engine's
pool, the build constraints of `parsers/go/builds.py` (each file's, and
whether files build together), and the package clause `editing/rename.py`
reads with `read_package_clause`. Each must answer exactly as the Python
does, on one thread, two and every core. A corpus of file names and heads
(`go_engine_corpus.py`) is asked of both, and so is each limit the reader
stops at, on both sides of it: Python's answer changes there, and Rust's
must change at the same input.
"""

from __future__ import annotations

import ast
import importlib
import random
from collections.abc import Iterable, Iterator
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag import engine_frame
from codebase_rag.engine_frame import RawGoConstraint
from codebase_rag.parsers.go.builds import (
    ALWAYS,
    UNKNOWN,
    Expr,
    GoBuilds,
)
from codebase_rag.parsers.go.module_paths import read_package_clause
from codebase_rag.parsers.native_trees import describe
from codebase_rag.tests.go_engine_corpus import (
    EXTRA_SOURCES,
    HAND_CLAUSE_HEADS,
    HAND_HEADS,
    HAND_NAMES,
    generated_clause_heads,
    generated_heads,
    generated_names,
    whitespace_heads,
)
from codebase_rag.types_defs import DescribedNode

# Python's Go grammar, which only the treesitter-full extra installs.
GO_GRAMMAR_PACKAGE = "tree_sitter_go"


def _go_parser() -> Parser | None:
    """A parser on Python's Go grammar, or None on the base install."""
    try:
        grammar = importlib.import_module(GO_GRAMMAR_PACKAGE)
    except ImportError:
        return None
    return Parser(Language(grammar.language()))


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS = Path(__file__).resolve().parent
GO_PARSERS = REPO_ROOT / "codebase_rag" / "parsers" / "go"
AST_GO = REPO_ROOT / "codebase_rag" / "constants" / "ast_go.py"
SEED = 3629
SHOWN = 15
GENERATED_NAMES = 300
GENERATED_HEADS = 1500
GENERATED_CLAUSE_HEADS = 600
GROUPS = 1500
HEAD_BYTES = 64 * 1024
MAX_DEPTH = 100
MAX_TOKENS = 1000
MAX_EXPR_SIZE = 4096
MAX_FREE_TAGS = 10
GO_FILE = "a.go"

type Answer = object


def _as_raw(expr: Expr) -> RawGoConstraint:
    return (
        expr.op,
        expr.name,
        tuple(_as_raw(part) for part in expr.parts),
        expr.size,
        expr.depth,
    )


def _go_build(expression: str) -> str:
    return f"//go:build {expression}\n\npackage p\n"


def _chain(links: int) -> str:
    # `links` links hold `links + 1` tags, the two ends asked for or ruled
    # out alone: `links - 1` tags are both, and enumerated.
    return " && ".join(f"(t{k} || !t{k + 1})" for k in range(links))


def _tokens(negations: int) -> str:
    # `!t0 && t1 && ... && t499` is `MAX_TOKENS` tokens with one negation.
    tags = " && ".join(f"t{k}" for k in range(MAX_TOKENS // 2))
    return "!" * negations + tags


_LINUX = "//go:build linux\n"


def _blank_head(size: int) -> str:
    # `size` bytes: a constraint, then blank lines and no code.
    return _LINUX + "\n" * (size - len(_LINUX))


def _code_at(offset: int) -> str:
    # A constraint, blank lines, and the package clause at byte `offset`.
    return _blank_head(offset) + "package p\n"


# Each limit as the head at it and the head one step past it: Python reads
# the first and not the second.
HEAD_LIMITS: dict[str, tuple[str, str]] = {
    "nesting of groups": (
        _go_build("(" * MAX_DEPTH + "linux" + ")" * MAX_DEPTH),
        _go_build("(" * (MAX_DEPTH + 1) + "linux" + ")" * (MAX_DEPTH + 1)),
    ),
    "nesting of negations": (
        _go_build("!" * MAX_DEPTH + "linux"),
        _go_build("!" * (MAX_DEPTH + 1) + "linux"),
    ),
    "nesting of both": (
        _go_build("!(" * (MAX_DEPTH // 2) + "linux" + ")" * (MAX_DEPTH // 2)),
        _go_build("!(" * (MAX_DEPTH // 2) + "(linux)" + ")" * (MAX_DEPTH // 2)),
    ),
    "tokens": (_go_build(_tokens(1)), _go_build(_tokens(2))),
    "head without code": (_blank_head(HEAD_BYTES - 1), _blank_head(HEAD_BYTES)),
    "code at the head's end": (_code_at(HEAD_BYTES - 1), _code_at(HEAD_BYTES)),
}


def _or_of(prefix: str, count: int) -> str:
    return _go_build(" || ".join(f"{prefix}{k}" for k in range(count)))


# A group's constraints joined past `MAX_EXPR_SIZE` nodes: ten any-ofs of
# 408 tags each (409 nodes) and tags alone, under one all-of, which leaves
# room for `_ROOM` tags.
_OR_TAGS = 408
_SIZE_FILES = [_or_of(f"s{file}_", _OR_TAGS) for file in range(10)]
_ROOM = MAX_EXPR_SIZE - 1 - len(_SIZE_FILES) * (_OR_TAGS + 1)
_SIZE_TAGS = [_go_build(f"z{k}") for k in range(_ROOM + 1)]
GROUP_LIMITS: dict[str, tuple[list[str], list[str]]] = {
    "constraint size": (
        [*_SIZE_FILES, *_SIZE_TAGS[:_ROOM]],
        [*_SIZE_FILES, *_SIZE_TAGS],
    ),
    "free tags": (
        [_go_build(_chain(MAX_FREE_TAGS + 1))],
        [_go_build(_chain(MAX_FREE_TAGS + 2))],
    ),
}


class Corpus(NamedTuple):
    """The corpus on disk: the files whose constraints are asked, groups of
    them, the files whose package clauses are asked and the Go sources
    parsed, each limit's two files or groups, all under `root`."""

    root: Path
    rel_paths: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    clause_paths: tuple[Path, ...]
    sources: tuple[Path, ...]
    head_limits: dict[str, tuple[str, str]]
    group_limits: dict[str, tuple[tuple[str, ...], tuple[str, ...]]]


def _write(root: Path, rel_path: str, content: str | bytes) -> str:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode(cs.ENCODING_UTF8) if isinstance(content, str) else content
    path.write_bytes(data)
    return rel_path


@cache
def _go_snippets() -> tuple[bytes, ...]:
    """Every Go source the Go tests write, as their string literals hold
    them, with the repository's own Go files."""
    found: set[str] = set()
    for path in sorted(TESTS.glob("test_go_*.py")):
        tree = ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))
        found.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "package " in node.value
        )
    repository = (
        REPO_ROOT / "codebase_rag/parsers/go_frontend/gotypes/main.go",
        REPO_ROOT / "evals/flows_corpus/go/app.go",
        REPO_ROOT / "evals/oracles/go_ast.go",
    )
    return (
        *(text.encode(cs.ENCODING_UTF8) for text in sorted(found)),
        *(path.read_bytes() for path in repository),
        *EXTRA_SOURCES,
    )


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Corpus:
    root = tmp_path_factory.mktemp("go_corpus")
    rng = random.Random(SEED)  # noqa: S311 - a reproducible corpus, not a secret
    names = [*HAND_NAMES, *generated_names(rng, GENERATED_NAMES)]
    heads: list[str | bytes] = [
        *HAND_HEADS,
        *whitespace_heads(),
        *generated_heads(rng, GENERATED_HEADS),
    ]
    rel_paths = [
        _write(root, f"n{index:04d}/{name}", "package p\n")
        for index, name in enumerate(names)
    ]
    rel_paths += [
        _write(root, f"h{index:05d}/{names[index % len(names)]}", head)
        for index, head in enumerate(heads)
    ]
    head_limits = {
        limit: (
            _write(root, f"limit{index}/below/{GO_FILE}", below),
            _write(root, f"limit{index}/above/{GO_FILE}", above),
        )
        for index, (limit, (below, above)) in enumerate(HEAD_LIMITS.items())
    }
    group_limits = {
        limit: tuple(
            tuple(
                _write(root, f"group{index}/{side}/{k}/{GO_FILE}", head)
                for k, head in enumerate(files)
            )
            for side, files in (("below", below), ("above", above))
        )
        for index, (limit, (below, above)) in enumerate(GROUP_LIMITS.items())
    }
    limit_paths = [path for pair in head_limits.values() for path in pair]
    limit_paths += [
        path for pair in group_limits.values() for group in pair for path in group
    ]
    # None, a missing file, a folder, and a file named with a trailing slash,
    # which pathlib drops.
    unread = ["", "missing/x_linux.go", "n0001", f"{rel_paths[-1]}/"]
    all_paths = [*rel_paths, *limit_paths, *unread]
    groups = [tuple(rng.sample(all_paths, rng.randint(1, 4))) for _ in range(GROUPS)]
    groups += [group for pair in group_limits.values() for group in pair]
    groups += [(), ("n0001/i_linux.go", "n0002/i_windows.go")]
    clause_heads = [
        *HAND_CLAUSE_HEADS,
        *generated_clause_heads(rng, GENERATED_CLAUSE_HEADS),
    ]
    clause_paths = [root / path for path in all_paths if path]
    clause_paths += [
        root / _write(root, f"c{index:04d}.go", head)
        for index, head in enumerate(clause_heads)
    ]
    # The clause ending on the head's last byte, cut off inside its name,
    # starting on the last byte, and past the head.
    clause_paths += [
        root / _write(root, f"edge/{name}{offset}.go", _clause_at_edge(name, offset))
        for name, offset in (("pkg", 0), ("pkgname", 0), ("pkg", 11), ("pkg", 12))
    ]
    sources = [
        root / _write(root, f"s{index:04d}.go", source)
        for index, source in enumerate(_go_snippets())
    ]
    sources += [root / "missing.go", root / "n0001"]
    return Corpus(
        root,
        tuple(all_paths),
        tuple(groups),
        tuple(clause_paths),
        tuple(sources),
        head_limits,
        group_limits,
    )


def _clause_at_edge(name: str, offset: int) -> str:
    # `package <name>`, starting `offset` bytes after the place where
    # `package pkg\n` ends on the head's last byte.
    return "\n" * (HEAD_BYTES - len("package pkg\n") + offset) + f"package {name}\n"


class PythonAnswers(NamedTuple):
    constraints: list[RawGoConstraint]
    decisions: list[bool | None]
    clauses: list[str | None]
    parses: list[list[DescribedNode] | None]


def _decision(builds: GoBuilds, group: Iterable[str]) -> bool | None:
    paths = tuple(group)
    if builds.together(paths):
        return True
    return False if builds.apart(paths) else None


def _python_parse(parser: Parser, path: Path) -> list[DescribedNode] | None:
    try:
        source = path.read_bytes()
    except OSError:
        return None
    return describe(parser.parse(source).root_node)


@pytest.fixture(scope="module")
def python(corpus: Corpus) -> PythonAnswers:
    builds = GoBuilds(corpus.root)
    # The base install has no Go grammar package: only the parse comparison
    # needs one, and it skips there; the rest compare Python's text readers.
    parser = _go_parser()
    return PythonAnswers(
        [_as_raw(builds.constraint(path)) for path in corpus.rel_paths],
        [_decision(builds, group) for group in corpus.groups],
        [read_package_clause(path) for path in corpus.clause_paths],
        [_python_parse(parser, path) for path in corpus.sources] if parser else [],
    )


@pytest.fixture(params=[1, 2, 0], ids=["one-thread", "two-threads", "every-core"])
def engine(request: pytest.FixtureRequest) -> engine_frame.Engine:
    made = engine_frame.new_engine(request.param)
    # The checkout builds its own crate, so the engine is always there.
    assert made is not None, "the extension lacks the engine; rebuild the crate"
    return made


def _assert_same(
    cases: Iterable[object],
    python: Iterable[Answer],
    rust: Iterable[Answer],
    at_least: int,
) -> None:
    count = 0
    differing = []
    for case, expected, actual in zip(cases, python, rust, strict=True):
        count += 1
        if expected != actual:
            differing.append(f"{case!r}: Python {expected!r}, Rust {actual!r}")
    assert not differing, f"{len(differing)} of {count} answers differ:\n" + "\n".join(
        differing[:SHOWN]
    )
    # The corpus must not shrink unnoticed.
    assert count >= at_least, count


def test_each_files_constraint_is_pythons(
    corpus: Corpus, python: PythonAnswers, engine: engine_frame.Engine
) -> None:
    rust = engine.go_build_constraints(corpus.root, corpus.rel_paths)

    _assert_same(corpus.rel_paths, python.constraints, rust, at_least=2200)
    # The corpus reaches every kind of answer.
    ops = {constraint[0] for constraint in python.constraints}
    assert ops == {"tag", "not", "and", "or", "unknown"}


def test_each_groups_decision_is_pythons(
    corpus: Corpus, python: PythonAnswers, engine: engine_frame.Engine
) -> None:
    rust = engine.go_build_decisions(corpus.root, corpus.groups)

    _assert_same(corpus.groups, python.decisions, rust, at_least=GROUPS)
    assert set(python.decisions) == {True, False, None}


def test_each_package_clause_is_pythons(
    corpus: Corpus, python: PythonAnswers, engine: engine_frame.Engine
) -> None:
    rust = engine.go_package_clauses(corpus.clause_paths)

    _assert_same(corpus.clause_paths, python.clauses, rust, at_least=2800)
    assert None in python.clauses
    assert len(set(python.clauses)) > 15


def test_each_go_file_parses_as_py_tree_sitter_parses_it(
    corpus: Corpus, python: PythonAnswers, engine: engine_frame.Engine
) -> None:
    pytest.importorskip(GO_GRAMMAR_PACKAGE)
    rust = engine.go_parse(corpus.sources)

    _assert_same(corpus.sources, python.parses, rust, at_least=300)
    assert sum(len(tree) for tree in python.parses if tree) > 20_000


@pytest.mark.parametrize("limit", sorted(HEAD_LIMITS))
def test_the_reader_stops_where_pythons_does(
    corpus: Corpus, python: PythonAnswers, limit: str
) -> None:
    engine = engine_frame.engine()
    assert engine is not None
    paths = corpus.head_limits[limit]
    expected = [python.constraints[corpus.rel_paths.index(path)] for path in paths]

    below, above = expected
    assert below != _as_raw(UNKNOWN)
    assert above == _as_raw(UNKNOWN)
    assert engine.go_build_constraints(corpus.root, paths) == expected


@pytest.mark.parametrize("limit", sorted(GROUP_LIMITS))
def test_a_decision_stops_where_pythons_does(
    corpus: Corpus, python: PythonAnswers, limit: str
) -> None:
    engine = engine_frame.engine()
    assert engine is not None
    groups = corpus.group_limits[limit]
    expected = [python.decisions[corpus.groups.index(group)] for group in groups]

    assert expected == [True, None]
    assert engine.go_build_decisions(corpus.root, groups) == expected


def test_a_file_that_cannot_be_read_is_read_as_python_reads_it(
    corpus: Corpus, python: PythonAnswers
) -> None:
    # Nothing, a missing file and a folder ask nothing beyond their names; a
    # trailing slash pathlib drops still names the file.
    unread = corpus.rel_paths[-4:]
    answers = [python.constraints[corpus.rel_paths.index(path)] for path in unread]

    assert answers[:3] == [_as_raw(ALWAYS), ("tag", "linux", (), 1, 1), _as_raw(ALWAYS)]
    assert answers[3] == python.constraints[corpus.rel_paths.index(unread[3][:-1])]


def _module_names(path: Path) -> Iterator[str]:
    for node in ast.parse(path.read_text(encoding=cs.ENCODING_UTF8)).body:
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign)
            else node.target
            if isinstance(node, ast.AnnAssign)
            else None
        )
        if isinstance(target, ast.Name):
            yield target.id


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, frozenset | set | tuple | list):
        return [item for item in value if isinstance(item, str)]
    return []


def test_every_go_node_kind_and_field_python_reads_is_the_engines() -> None:
    # Each `cs` name Go's analysis reads (`parsers/go/`), Go's own constants
    # and its language spec's node types: every value the Go grammar has as
    # a named node kind or a field, and every `TS_GO_` one it has as a
    # token, is one of the engine's constants.
    names = set(_module_names(AST_GO))
    names.update(name for name in dir(cs) if name.startswith("SPEC_GO_"))
    for path in sorted(GO_PARSERS.glob("*.py")):
        names.update(
            node.attr
            for node in ast.walk(ast.parse(path.read_text(encoding=cs.ENCODING_UTF8)))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "cs"
        )
    go = Language(pytest.importorskip(GO_GRAMMAR_PACKAGE).language())
    kinds, tokens, fields = set(), set(), set()
    for name in names:
        for value in _strings(getattr(cs, name, None)):
            if go.id_for_node_kind(value, True):
                kinds.add(value)
            elif name.startswith("TS_GO_") and go.id_for_node_kind(value, False):
                tokens.add(value)
            if go.field_id_for_name(value):
                fields.add(value)
    engine_type = type(engine_frame.new_engine(1))

    assert len(kinds) > 60
    assert tokens
    assert len(fields) > 15
    assert kinds <= set(getattr(engine_type, "GO_NODE_KINDS"))
    assert tokens <= set(getattr(engine_type, "GO_TOKENS"))
    assert fields <= set(getattr(engine_type, "GO_FIELDS"))
