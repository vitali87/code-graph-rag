"""`LocationRehydrator`: the location indexes a sync reads back from the graph.

An incremental sync re-parses only what changed, so Pass 2 fills the C# and
Go type locations and the function locations for those files alone, while
the semantic joins resolve against every file (issues #1229, #1240). These
tests construct the rehydrator directly over a scripted graph to pin how
each read-back row is admitted, keyed and merged with what Pass 2 recorded,
and round-trip one Python repository through the embedded engine to pin
that what the graph returns restores exactly what Pass 2 recorded.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.factory import ProcessorFactory
from codebase_rag.services import QueryProtocol
from codebase_rag.services.graph_service import GraphConnection, GraphIngestor
from codebase_rag.types_defs import FunctionLocation, PropertyParams, ResultRow
from codebase_rag.updater.location_rehydration import LocationRehydrator
from codebase_rag.updater.rehydration import RehydrationRun


@dataclass
class _Run:
    is_full_build: bool = False
    pending_inbound_edges: list[ResultRow] = field(default_factory=list)


class _Graph:
    """Answers each location query from scripted rows; `failing` raises."""

    def __init__(
        self,
        rows: dict[str, list[ResultRow]] | None = None,
        failing: set[str] = frozenset(),
    ) -> None:
        self.rows = rows or {}
        self.failing = set(failing)
        self.reads: list[tuple[str, PropertyParams | None]] = []

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        self.reads.append((query, params))
        if query in self.failing:
            raise RuntimeError("graph unreachable")
        return list(self.rows.get(query, []))

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        return None


def _factory(repo: Path) -> ProcessorFactory:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=_Graph(),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).factory


def _owned_by_proj(rows: list[ResultRow], key: str) -> list[ResultRow]:
    # The updater's rule in miniature: a nested project `proj.v2` shares the
    # `proj.` prefix the queries select on, and owns its own rows.
    return [
        row
        for row in rows
        if isinstance(qn := row.get(key), str) and not qn.startswith("proj.v2.")
    ]


def _module_of_path(path: str, run: RehydrationRun) -> str:
    return "proj." + Path(path).with_suffix("").as_posix().replace("/", ".")


def _rehydrator(
    graph: QueryProtocol | None, factory: ProcessorFactory
) -> LocationRehydrator:
    return LocationRehydrator(
        graph=graph,
        factory=factory,
        project_name="proj",
        owned_rows=_owned_by_proj,
        recorded_module_qn=_module_of_path,
    )


@pytest.fixture
def factory(temp_repo: Path) -> ProcessorFactory:
    return _factory(temp_repo)


def _csharp_type(qn: str | None, path: str | None, line: int | None) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: line,
    }


def _go_type(
    qn: str, label: str | None, path: str, line: int, col: int | None
) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_LABEL: label,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: line,
        cs.KEY_START_COL: col,
    }


class TestCSharpTypeLocations:
    def test_unchanged_files_types_are_restored_beside_fresh_ones(
        self, factory: ProcessorFactory
    ) -> None:
        locations = factory.definition_processor.csharp.type_locations
        locations[("Core/A.cs", 3)] = "proj.Core.A.Fresh"
        graph = _Graph(
            {
                cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS: [
                    _csharp_type("proj.Core.B.Part", "Core/B.cs", 5),
                    # Same key as the fresh Pass-2 entry: the fresh one wins.
                    _csharp_type("proj.Core.A.Stale", "Core/A.cs", 3),
                    _csharp_type("proj.v2.Other", "Other.cs", 1),
                ]
            }
        )

        _rehydrator(graph, factory).rehydrate_csharp_type_locations(_Run())

        assert locations == {
            ("Core/A.cs", 3): "proj.Core.A.Fresh",
            ("Core/B.cs", 5): "proj.Core.B.Part",
        }
        assert graph.reads == [
            (cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS, {cs.KEY_PROJECT_PREFIX: "proj."})
        ]

    @pytest.mark.parametrize(
        "row",
        [
            # bool is an int: True would key as line 1 and shadow a real type.
            _csharp_type("proj.T", "T.cs", True),
            _csharp_type("proj.T", "T.cs", None),
            _csharp_type("proj.T", None, 1),
            _csharp_type(None, "T.cs", 1),
        ],
        ids=["bool-line", "no-line", "no-path", "no-qn"],
    )
    def test_a_row_without_a_usable_key_is_skipped(
        self, factory: ProcessorFactory, row: ResultRow
    ) -> None:
        graph = _Graph({cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS: [row]})

        _rehydrator(graph, factory).rehydrate_csharp_type_locations(_Run())

        assert factory.definition_processor.csharp.type_locations == {}


class TestGoTypeLocations:
    def test_go_types_are_restored_by_line_and_column(
        self, factory: ProcessorFactory
    ) -> None:
        locations = factory.definition_processor.go.type_locations
        locations[("pkg/a.go", 3, 5)] = ("proj.pkg.a.Fresh", "Class")
        graph = _Graph(
            {
                cs.CYPHER_ALL_GO_TYPE_LOCATIONS: [
                    _go_type("proj.pkg.b.Store", "Interface", "pkg/b.go", 4, 5),
                    _go_type("proj.pkg.a.Stale", "Class", "pkg/a.go", 3, 5),
                    # Two types on one line keep apart by column.
                    _go_type("proj.pkg.b.Left", "Class", "pkg/b.go", 9, 5),
                    _go_type("proj.pkg.b.Right", "Class", "pkg/b.go", 9, 30),
                ]
            }
        )

        _rehydrator(graph, factory).rehydrate_go_type_locations(_Run())

        assert locations == {
            ("pkg/a.go", 3, 5): ("proj.pkg.a.Fresh", "Class"),
            ("pkg/b.go", 4, 5): ("proj.pkg.b.Store", "Interface"),
            ("pkg/b.go", 9, 5): ("proj.pkg.b.Left", "Class"),
            ("pkg/b.go", 9, 30): ("proj.pkg.b.Right", "Class"),
        }

    @pytest.mark.parametrize(
        "row",
        [
            # A pre-#1240 graph persisted no column.
            _go_type("proj.T", "Class", "t.go", 1, None),
            _go_type("proj.T", "Class", "t.go", 1, False),
            # The query reads every language's types; only Go's are keyed.
            _go_type("proj.T", "Class", "t.rs", 1, 0),
            _go_type("proj.T", None, "t.go", 1, 0),
            _go_type("proj.v2.T", "Class", "t.go", 1, 0),
        ],
        ids=["no-col", "bool-col", "not-go", "no-label", "nested-project"],
    )
    def test_a_row_the_join_cannot_key_is_skipped(
        self, factory: ProcessorFactory, row: ResultRow
    ) -> None:
        graph = _Graph({cs.CYPHER_ALL_GO_TYPE_LOCATIONS: [row]})

        _rehydrator(graph, factory).rehydrate_go_type_locations(_Run())

        assert factory.definition_processor.go.type_locations == {}


def _definition(
    qn: str,
    label: str,
    module_qn: str,
    start: tuple[int, int],
    name: tuple[int | None, int | None] = (None, None),
    **extra: str,
) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_LABEL: label,
        cs.KEY_MODULE_QN: module_qn,
        cs.KEY_START_LINE: start[0],
        cs.KEY_START_COL: start[1],
        cs.KEY_NAME_START_LINE: name[0],
        cs.KEY_NAME_START_COL: name[1],
        **extra,
    }


class TestFunctionLocations:
    def test_a_definition_is_keyed_at_its_start_and_at_its_name(
        self, factory: ProcessorFactory
    ) -> None:
        graph = _Graph(
            {
                cs.CYPHER_ALL_FUNCTION_LOCATIONS: [
                    _definition("proj.a.f", "Function", "proj.a", (1, 0), (1, 4)),
                    # No persisted name token: the start key alone.
                    _definition("proj.a.g", "Function", "proj.a", (7, 0)),
                ],
                cs.CYPHER_ALL_METHOD_LOCATIONS: [
                    _definition(
                        "proj.a.C.m",
                        "Method",
                        "proj.a",
                        (5, 4),
                        (5, 8),
                        container_qn="proj.a.C",
                    )
                ],
            }
        )

        _rehydrator(graph, factory).rehydrate_function_locations(_Run())

        f = FunctionLocation("Function", "proj.a.f", None)
        m = FunctionLocation("Method", "proj.a.C.m", "proj.a.C")
        assert dict(factory.facts.function_locations) == {
            ("proj.a", 1, 0): f,
            ("proj.a", 1, 4): f,
            ("proj.a", 7, 0): FunctionLocation("Function", "proj.a.g", None),
            ("proj.a", 5, 4): m,
            ("proj.a", 5, 8): m,
        }

    def test_a_name_on_a_later_line_is_keyed_on_its_own_line(
        self, factory: ProcessorFactory
    ) -> None:
        # A multiline Go receiver: `func (s *Store)\n\tGet()`.
        graph = _Graph(
            {
                cs.CYPHER_ALL_METHOD_LOCATIONS: [
                    _definition(
                        "proj.pkg.s.Store.Get",
                        "Method",
                        "proj.pkg.s",
                        (10, 0),
                        (11, 1),
                        container_qn="proj.pkg.s.Store",
                        path="pkg/s.go",
                    )
                ]
            }
        )

        _rehydrator(graph, factory).rehydrate_function_locations(_Run())

        assert set(factory.facts.function_locations) == {
            ("proj.pkg.s", 10, 0),
            ("proj.pkg.s", 11, 1),
        }

    def test_a_go_method_is_keyed_by_its_own_files_module(
        self, factory: ProcessorFactory
    ) -> None:
        # Both methods hang off the receiver type's module, `proj.pkg.types`,
        # at the same line and column of two different files (issue #2483).
        graph = _Graph(
            {
                cs.CYPHER_ALL_METHOD_LOCATIONS: [
                    _definition(
                        f"proj.pkg.types.T.{name}",
                        "Method",
                        "proj.pkg.types",
                        (3, 0),
                        container_qn="proj.pkg.types.T",
                        path=f"pkg/{name.lower()}.go",
                    )
                    for name in ("Load", "Save")
                ]
            }
        )

        _rehydrator(graph, factory).rehydrate_function_locations(_Run())

        restored = factory.facts.function_locations
        assert restored[("proj.pkg.load", 3, 0)].qualified_name == (
            "proj.pkg.types.T.Load"
        )
        assert restored[("proj.pkg.save", 3, 0)].qualified_name == (
            "proj.pkg.types.T.Save"
        )
        assert ("proj.pkg.types", 3, 0) not in restored

    def test_fresh_records_win_and_unusable_rows_are_skipped(
        self, factory: ProcessorFactory
    ) -> None:
        fresh = FunctionLocation("Function", "proj.a.renamed", None)
        factory.facts.function_locations[("proj.a", 1, 0)] = fresh
        graph = _Graph(
            {
                cs.CYPHER_ALL_FUNCTION_LOCATIONS: [
                    _definition("proj.a.old", "Function", "proj.a", (1, 0), (1, 4)),
                    {
                        **_definition("proj.a.bad", "Function", "proj.a", (2, 0)),
                        cs.KEY_START_LINE: True,
                    },
                    _definition("proj.v2.x.f", "Function", "proj.v2.x", (1, 0)),
                ]
            }
        )

        _rehydrator(graph, factory).rehydrate_function_locations(_Run())

        assert dict(factory.facts.function_locations) == {
            ("proj.a", 1, 0): fresh,
            # The name key was free, so the read-back record takes it.
            ("proj.a", 1, 4): FunctionLocation("Function", "proj.a.old", None),
        }


_REHYDRATIONS = {
    "csharp": (
        LocationRehydrator.rehydrate_csharp_type_locations,
        cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS,
    ),
    "go": (
        LocationRehydrator.rehydrate_go_type_locations,
        cs.CYPHER_ALL_GO_TYPE_LOCATIONS,
    ),
    "functions": (
        LocationRehydrator.rehydrate_function_locations,
        cs.CYPHER_ALL_FUNCTION_LOCATIONS,
    ),
    "methods": (
        LocationRehydrator.rehydrate_function_locations,
        cs.CYPHER_ALL_METHOD_LOCATIONS,
    ),
}


class TestFailedReads:
    @pytest.mark.parametrize("which", sorted(_REHYDRATIONS))
    def test_an_incremental_run_aborts_on_a_failed_read(
        self, factory: ProcessorFactory, which: str
    ) -> None:
        # Its joins would otherwise resolve against a partial index.
        rehydrate, query = _REHYDRATIONS[which]
        graph = _Graph(failing={query})

        with pytest.raises(RuntimeError, match="graph unreachable"):
            rehydrate(_rehydrator(graph, factory), _Run(is_full_build=False))

    @pytest.mark.parametrize("which", sorted(_REHYDRATIONS))
    def test_a_full_build_carries_on_past_a_failed_read(
        self, factory: ProcessorFactory, which: str
    ) -> None:
        # A full build re-parses every file, so Pass 2 fills the indexes.
        rehydrate, query = _REHYDRATIONS[which]
        graph = _Graph(failing={query})

        rehydrate(_rehydrator(graph, factory), _Run(is_full_build=True))

        assert graph.reads[-1][0] == query
        assert factory.facts.function_locations == {}

    def test_a_failed_method_read_keeps_the_functions_already_restored(
        self, factory: ProcessorFactory
    ) -> None:
        graph = _Graph(
            {
                cs.CYPHER_ALL_FUNCTION_LOCATIONS: [
                    _definition("proj.a.f", "Function", "proj.a", (1, 0))
                ]
            },
            failing={cs.CYPHER_ALL_METHOD_LOCATIONS},
        )

        _rehydrator(graph, factory).rehydrate_function_locations(
            _Run(is_full_build=True)
        )

        assert set(factory.facts.function_locations) == {("proj.a", 1, 0)}

    def test_a_write_only_sink_restores_nothing(
        self, factory: ProcessorFactory
    ) -> None:
        rehydrator = _rehydrator(None, factory)
        for rehydrate, _query in _REHYDRATIONS.values():
            rehydrate(rehydrator, _Run())

        assert factory.facts.function_locations == {}
        assert factory.definition_processor.go.type_locations == {}
        assert factory.definition_processor.csharp.type_locations == {}


@pytest.fixture
def embedded_graph(tmp_path: Path) -> Iterator[GraphIngestor]:
    with GraphIngestor(
        connection=GraphConnection(
            dialect=get_dialect(DIALECT_SQLITE),
            sqlite_path=str(tmp_path / "graph.sqlite"),
        )
    ) as ingestor:
        ingestor.ensure_constraints()
        yield ingestor


def test_what_the_graph_returns_restores_what_pass_2_recorded(
    temp_repo: Path, embedded_graph: GraphIngestor
) -> None:
    (temp_repo / "a.py").write_text(
        "def f(x):\n    return x\n\n\nclass C:\n    def m(self):\n        return 1\n",
        encoding="utf-8",
    )
    (temp_repo / "b.py").write_text(
        "from a import f\n\n\ndef g():\n    return f(1)\n", encoding="utf-8"
    )
    parsers, queries = load_parsers()
    indexed = GraphUpdater(
        ingestor=embedded_graph,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        skip_embeddings=True,
    )
    indexed.run(force=True)
    recorded = dict(indexed.factory.facts.function_locations)
    assert {loc.qualified_name for loc in recorded.values()} == {
        "proj.a.f",
        "proj.a.C.m",
        "proj.b.g",
    }, "fixture guard: Pass 2 must record every definition"

    fresh = _factory(temp_repo)
    _rehydrator(embedded_graph, fresh).rehydrate_function_locations(_Run())

    restored = fresh.facts.function_locations
    for key, location in recorded.items():
        assert restored.get(key) == location, key
    # Every other key is a name-token alias of a restored definition.
    aliases = set(restored) - set(recorded)
    assert {restored[key] for key in aliases} <= set(recorded.values())
