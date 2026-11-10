# `cgr graph definition` on a decorated definition (issue #2428).
#
# Python hangs decorators on the parent `decorated_definition`, so the node a
# function, method or class is ingested from starts at its `def`/`class` line,
# and `definition` answered with a span and source that left the decorators
# out, and without the `decorators` list the node already stores. A consumer
# saw an uncached, non-retrying `fetch`, and an edit by that span would orphan
# the decorators.
#
# The node's stored `start_line` must NOT move: call attribution, rename, dead
# code and the incremental deltas key on it, and a decorator's own call runs
# at definition time in the enclosing scope. The decorated span is recorded
# beside it and only `definition` reads it, so a graph indexed before that
# property existed still answers, with the span it always had.
#
# The graph here is the one the indexer records into the mock ingestor; the
# definition query is answered with exactly the columns its RETURN names, the
# way Memgraph answers it (an absent property reads as null).
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.types_defs import PropertyDict, ResultRow

SOURCE = """\
import functools
from dataclasses import dataclass


def retry(times):
    def deco(fn):
        return fn

    return deco


def route(path):
    return retry(times=1)


@retry(times=3)
@functools.lru_cache(maxsize=None)
def fetch(url):
    return url


@route(
    "/x",
)
def handler():
    return 1


class Service:
    @staticmethod
    def build():
        return Service()

    @property
    def name(self):
        return "svc"


@dataclass
class Point:
    x: int = 0


def plain():
    return 2
"""

_COLUMN = re.compile(r"\bn\.(\w+) AS (\w+)")


def _line(text: str, source: str = SOURCE) -> int:
    """1-based line of the one line of `source` that starts with `text`."""
    hits = [
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if line.strip().startswith(text)
    ]
    assert len(hits) == 1, (text, hits)
    return hits[0]


class RecordedGraph:
    """The definition query answered from what the indexer emitted."""

    def __init__(self, mock: MagicMock, project: str, root: Path) -> None:
        self.nodes: dict[str, tuple[str, PropertyDict]] = {}
        for c in mock.ensure_node_batch.call_args_list:
            label, props = str(c.args[0]), dict(c.args[1])
            qn = props.get(cs.KEY_QUALIFIED_NAME)
            if isinstance(qn, str):
                self.nodes[qn] = (label, props)
        self.project = project
        self.root = root

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        p = params or {}
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: self.project}]
        if query == cq.CYPHER_PROJECT_IS_INCOMPLETE:
            return []
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            return [{cs.KEY_ROOT_PATH: str(self.root)}]
        assert query == cq.CYPHER_GRAPH_DEFINITION, query[:60]
        hit = self.nodes.get(str(p.get(cs.KEY_QN, "")))
        if hit is None:
            return []
        label, props = hit
        row: ResultRow = {
            alias: props.get(prop) for prop, alias in _COLUMN.findall(query)
        }
        row[cs.KEY_LABEL] = label
        return [row]


@pytest.fixture
def indexed(tmp_path: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    repo = tmp_path / "decor"
    repo.mkdir()
    # LF pinned: `source` is the file's text as written, and Windows'
    # write_text would turn every expected `\n` into `\r\n`.
    (repo / "svc.py").write_text(SOURCE, encoding="utf-8", newline="\n")
    updater = create_and_run_updater(repo, mock_ingestor)
    return RecordedGraph(mock_ingestor, updater.project_name, repo)


def _definition(graph: RecordedGraph, name: str) -> graph_query.DefinitionRow:
    row = graph_query.definition(
        graph.fetch_all, graph.project, f"{graph.project}.svc.{name}", graph.root
    )
    assert row["found"] is True, name
    return row


# --- the decorated span -----------------------------------------------------


def test_decorated_function_definition_starts_at_its_first_decorator(
    indexed: RecordedGraph,
) -> None:
    row = _definition(indexed, "fetch")
    assert row["start_line"] == _line("@retry(times=3)")
    assert row["name_line"] == _line("def fetch(url):")
    assert row["end_line"] == _line("return url")
    assert row["source"] == (
        "@retry(times=3)\n"
        "@functools.lru_cache(maxsize=None)\n"
        "def fetch(url):\n"
        "    return url"
    )
    assert row["decorators"] == [
        "@retry(times=3)",
        "@functools.lru_cache(maxsize=None)",
    ]


def test_a_multi_line_decorator_is_covered_whole(indexed: RecordedGraph) -> None:
    row = _definition(indexed, "handler")
    assert row["start_line"] == _line("@route(")
    assert row["name_line"] == _line("def handler():")
    assert row["source"] == '@route(\n    "/x",\n)\ndef handler():\n    return 1'
    assert row["decorators"] == ['@route(\n    "/x",\n)']


@pytest.mark.parametrize(
    ("method", "decorator", "header"),
    [
        ("build", "@staticmethod", "def build():"),
        ("name", "@property", "def name(self):"),
    ],
)
def test_a_decorated_method_definition_covers_its_decorator(
    indexed: RecordedGraph, method: str, decorator: str, header: str
) -> None:
    row = _definition(indexed, f"Service.{method}")
    assert row["label"] == cs.NodeLabel.METHOD.value
    assert row["start_line"] == _line(decorator)
    assert row["name_line"] == _line(header)
    assert row["source"] is not None
    assert row["source"].startswith(f"{decorator}\n    {header}")
    assert row["decorators"] == [decorator]


def test_a_decorated_class_definition_covers_its_decorator(
    indexed: RecordedGraph,
) -> None:
    row = _definition(indexed, "Point")
    assert row["label"] == cs.NodeLabel.CLASS.value
    assert row["start_line"] == _line("@dataclass")
    assert row["name_line"] == _line("class Point:")
    assert row["source"] == "@dataclass\nclass Point:\n    x: int = 0"
    assert row["decorators"] == ["@dataclass"]


def test_cli_definition_prints_the_decorated_span_and_the_decorators(
    indexed: RecordedGraph,
) -> None:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=indexed.fetch_all)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    args = [
        "definition",
        f"{indexed.project}.svc.fetch",
        "--project",
        indexed.project,
        "--repo-path",
        str(indexed.root),
    ]
    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        result = CliRunner().invoke(graph_cli, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["start_line"] == _line("@retry(times=3)")
    assert payload["name_line"] == _line("def fetch(url):")
    assert payload["source"].startswith("@retry(times=3)\n")
    assert payload["decorators"] == [
        "@retry(times=3)",
        "@functools.lru_cache(maxsize=None)",
    ]


# --- what must not change ---------------------------------------------------


def test_an_undecorated_definition_is_unchanged(indexed: RecordedGraph) -> None:
    row = _definition(indexed, "plain")
    assert row["start_line"] == row["name_line"] == _line("def plain():")
    assert row["source"] == "def plain():\n    return 2"
    assert row["decorators"] == []
    # A class is not decorated by its decorated members.
    owner = _definition(indexed, "Service")
    assert owner["start_line"] == owner["name_line"] == _line("class Service:")
    assert owner["decorators"] == []


def test_the_stored_start_line_stays_on_the_def_line(indexed: RecordedGraph) -> None:
    # Everything line-based (call attribution, rename, the deltas) reads the
    # stored start; only the separate property carries the decorated one.
    _label, fetch = indexed.nodes[f"{indexed.project}.svc.fetch"]
    assert fetch[cs.KEY_START_LINE] == _line("def fetch(url):")
    assert fetch[cs.KEY_DECORATED_START_LINE] == _line("@retry(times=3)")
    _label, point = indexed.nodes[f"{indexed.project}.svc.Point"]
    assert point[cs.KEY_START_LINE] == _line("class Point:")
    assert point[cs.KEY_DECORATED_START_LINE] == _line("@dataclass")
    for name in ("plain", "Service", "route"):
        _label, props = indexed.nodes[f"{indexed.project}.svc.{name}"]
        assert cs.KEY_DECORATED_START_LINE not in props, name


def test_a_decorators_call_still_belongs_to_the_module(
    indexed: RecordedGraph, mock_ingestor: MagicMock
) -> None:
    # `retry(times=3)` runs when the module executes the `def`, not when
    # `fetch` is called: its CALLS edge must stay on the module.
    module_qn = f"{indexed.project}.svc"
    retry_qn = f"{module_qn}.retry"
    decorator_line = _line("@retry(times=3)")
    sources = {
        c.args[0][2]
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if c.args[2][2] == retry_qn
        and (c.args[3] if len(c.args) > 3 else c.kwargs.get("properties") or {}).get(
            cs.KEY_LINE
        )
        == decorator_line
    }
    assert sources == {module_qn}
    fetch_calls = [
        c
        for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value)
        if c.args[0][2] == f"{module_qn}.fetch"
    ]
    assert fetch_calls == []


@pytest.mark.parametrize("stored", ["absent", None, "not-a-line"])
def test_a_graph_without_the_decorated_start_keeps_the_old_span(
    indexed: RecordedGraph, stored: str | None
) -> None:
    # A graph indexed before the property existed (or a value that is not a
    # line) answers with the def-line span, and still names the decorators.
    _label, props = indexed.nodes[f"{indexed.project}.svc.fetch"]
    if stored == "absent":
        del props[cs.KEY_DECORATED_START_LINE]
    else:
        props[cs.KEY_DECORATED_START_LINE] = stored
    row = _definition(indexed, "fetch")
    assert row["start_line"] == row["name_line"] == _line("def fetch(url):")
    assert row["source"] == "def fetch(url):\n    return url"
    assert row["decorators"] == [
        "@retry(times=3)",
        "@functools.lru_cache(maxsize=None)",
    ]


def test_a_decorated_start_below_the_def_line_is_not_trusted(
    indexed: RecordedGraph,
) -> None:
    # The decorated span only ever widens the definition upwards.
    _label, props = indexed.nodes[f"{indexed.project}.svc.fetch"]
    props[cs.KEY_DECORATED_START_LINE] = _line("return url")
    row = _definition(indexed, "fetch")
    assert row["start_line"] == _line("def fetch(url):")


def test_an_unknown_definition_reports_empty_decorators(
    indexed: RecordedGraph,
) -> None:
    row = graph_query.definition(
        indexed.fetch_all, indexed.project, f"{indexed.project}.svc.nope", None
    )
    assert row["found"] is False
    assert row["name_line"] is None
    assert row["decorators"] == []


# --- annotations that precede the node as siblings --------------------------
#
# Rust attribute items, TypeScript method decorators and Dart metadata are not
# children of the definition either: they are the siblings the `decorators`
# list is already read from, and the span covers them the same way.

RUST_SOURCE = """\
/// Doc.
#[inline]
#[must_use]
pub fn attributed() -> u8 {
    2
}

pub fn bare() -> u8 {
    3
}

#[derive(Debug)]
pub struct Point {
    x: u8,
}
"""

TS_SOURCE = """\
function log(target: object, key: string) {}

export class Svc {
  @log
  run(): number {
    return 1;
  }

  plain(): number {
    return 2;
  }
}
"""


def _index(tmp_path: Path, mock: MagicMock, name: str, text: str) -> RecordedGraph:
    repo = tmp_path / "annotated"
    repo.mkdir()
    (repo / name).write_text(text, encoding="utf-8", newline="\n")
    updater = create_and_run_updater(repo, mock)
    return RecordedGraph(mock, updater.project_name, repo)


@pytest.mark.parametrize(
    ("name", "first", "header", "decorators"),
    [
        ("attributed", "#[inline]", "pub fn attributed", ["#[inline]", "#[must_use]"]),
        ("Point", "#[derive(Debug)]", "pub struct Point", ["#[derive(Debug)]"]),
        ("bare", "pub fn bare", "pub fn bare", []),
    ],
)
def test_rust_attribute_items_are_covered(
    tmp_path: Path,
    mock_ingestor: MagicMock,
    name: str,
    first: str,
    header: str,
    decorators: list[str],
) -> None:
    graph = _index(tmp_path, mock_ingestor, "lib.rs", RUST_SOURCE)
    row = graph_query.definition(
        graph.fetch_all, graph.project, f"{graph.project}.lib.{name}", graph.root
    )
    assert row["found"] is True
    # The doc comment above the attributes is the docstring, not the span.
    assert row["start_line"] == _line(first, RUST_SOURCE)
    assert row["name_line"] == _line(header, RUST_SOURCE)
    assert row["source"] is not None
    assert row["source"].startswith(first)
    assert row["decorators"] == decorators


@pytest.mark.parametrize(
    ("name", "first", "header", "decorators"),
    [
        ("run", "@log", "run(): number {", ["@log"]),
        ("plain", "plain(): number {", "plain(): number {", []),
    ],
)
def test_a_typescript_method_decorator_is_covered(
    tmp_path: Path,
    mock_ingestor: MagicMock,
    name: str,
    first: str,
    header: str,
    decorators: list[str],
) -> None:
    graph = _index(tmp_path, mock_ingestor, "svc.ts", TS_SOURCE)
    row = graph_query.definition(
        graph.fetch_all, graph.project, f"{graph.project}.svc.Svc.{name}", graph.root
    )
    assert row["found"] is True
    assert row["start_line"] == _line(first, TS_SOURCE)
    assert row["name_line"] == _line(header, TS_SOURCE)
    assert row["source"] is not None
    assert row["source"].startswith(first)
    assert row["decorators"] == decorators


JAVA_SOURCE = """\
class Greeter {
    @Override
    public String toString() {
        return "hi";
    }
}
"""


def test_an_annotation_inside_the_node_keeps_its_span_and_names_the_line(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    # Java annotations are modifiers inside the method node, so its stored
    # start is already the annotation line and nothing is recorded beside
    # it; `name_line` still points at the signature, not the annotation.
    graph = _index(tmp_path, mock_ingestor, "Greeter.java", JAVA_SOURCE)
    qn, (_label, props) = next(
        (qn, hit)
        for qn, hit in graph.nodes.items()
        if hit[0] == cs.NodeLabel.METHOD.value and hit[1].get(cs.KEY_NAME) == "toString"
    )
    assert cs.KEY_DECORATED_START_LINE not in props
    row = graph_query.definition(graph.fetch_all, graph.project, qn, graph.root)
    assert row["start_line"] == _line("@Override", JAVA_SOURCE)
    assert row["name_line"] == _line("public String toString()", JAVA_SOURCE)
    assert row["source"] is not None
    assert row["source"].startswith("@Override\n")
    assert "@Override" in row["decorators"]
