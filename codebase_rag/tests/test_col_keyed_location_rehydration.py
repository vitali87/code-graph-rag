# Issue #1240 (Phase 2 of #1229): the Go IMPLEMENTS and semantic-call joins
# resolve positions against col-keyed indexes (go_type_locations,
# function_locations) that Pass 2 fills only for RE-PARSED files. These tests
# drive the rehydration + joins directly (no Go toolchain, no real DB) via a
# QueryProtocol fake answering the location queries from seeded rows, exactly
# like the Phase-1 C# harness.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.frontends.protocol import ImplementsPair
from codebase_rag.types_defs import PropertyDict, ResultRow


class _LocGraph:
    """Answers the three location queries from seeded row lists; records
    relationship writes so the IMPLEMENTS join is observable."""

    def __init__(
        self,
        go_types: list[ResultRow] | None = None,
        functions: list[ResultRow] | None = None,
        methods: list[ResultRow] | None = None,
    ) -> None:
        self._by_query = {
            cs.CYPHER_ALL_GO_TYPE_LOCATIONS: go_types or [],
            cs.CYPHER_ALL_FUNCTION_LOCATIONS: functions or [],
            cs.CYPHER_ALL_METHOD_LOCATIONS: methods or [],
        }
        self.edges: list[tuple[str, str, str]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self, from_spec, rel_type, to_spec, properties=None
    ) -> None:
        self.edges.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return self._by_query.get(query, [])

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _updater(repo: Path, graph: _LocGraph) -> gu.GraphUpdater:
    parsers, queries = load_parsers()
    return gu.GraphUpdater(
        ingestor=graph, repo_path=repo, parsers=parsers, queries=queries
    )


def _go_type_row(qn: str, label: str, path: str, line: int, col: int) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_LABEL: label,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: line,
        cs.KEY_START_COL: col,
    }


def test_go_implements_joins_across_an_unchanged_file(tmp_path: Path) -> None:
    # The implementer is in a re-parsed file (fresh Pass-2 entry); the
    # interface sits in an UNCHANGED file and resolves only through the
    # rehydrated persisted location.
    repo = tmp_path / "proj"
    repo.mkdir()
    graph = _LocGraph(
        go_types=[
            _go_type_row("proj.pkg.iface.Store", "Interface", "pkg/iface.go", 3, 5)
        ]
    )
    updater = _updater(repo, graph)
    dp = updater.factory.definition_processor
    dp.go_type_locations[("pkg/impl.go", 8, 5)] = ("proj.pkg.impl.DiskStore", "Class")
    dp.go_implements = [
        ImplementsPair(
            impl_file="pkg/impl.go",
            impl_line=8,
            impl_col=5,
            iface_file="pkg/iface.go",
            iface_line=3,
            iface_col=5,
        )
    ]

    updater._rehydrate_go_type_locations()
    updater._join_go_implements()

    assert (
        "proj.pkg.impl.DiskStore",
        cs.RelationshipType.IMPLEMENTS.value,
        "proj.pkg.iface.Store",
    ) in [(a, r, b) for a, r, b in graph.edges]


def test_go_type_rehydration_guards_old_graphs_and_fresh_entries(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    graph = _LocGraph(
        go_types=[
            # pre-#1240 row: no start_col persisted -> skipped cleanly.
            {
                cs.KEY_QUALIFIED_NAME: "proj.old.T",
                cs.KEY_LABEL: "Class",
                cs.KEY_PATH: "old/t.go",
                cs.KEY_START_LINE: 2,
                cs.KEY_START_COL: None,
            },
            # bool masquerading as an int is rejected, not keyed as col 1.
            _go_type_row("proj.b.T", "Class", "b/t.go", 2, True),
            # a non-Go path never enters the Go index.
            _go_type_row("proj.cs.T", "Class", "cs/t.cs", 2, 4),
            # a fresh Pass-2 entry is kept, not overwritten.
            _go_type_row("proj.pkg.stale.Dup", "Class", "pkg/dup.go", 5, 0),
        ]
    )
    updater = _updater(repo, graph)
    dp = updater.factory.definition_processor
    dp.go_type_locations[("pkg/dup.go", 5, 0)] = ("proj.pkg.fresh.Dup", "Class")

    updater._rehydrate_go_type_locations()

    assert dp.go_type_locations[("pkg/dup.go", 5, 0)] == (
        "proj.pkg.fresh.Dup",
        "Class",
    )
    assert ("old/t.go", 2, None) not in dp.go_type_locations
    assert all("cs/t.cs" != key[0] for key in dp.go_type_locations)
    assert len(dp.go_type_locations) == 1


def test_function_rehydration_keys_span_and_name_alias(tmp_path: Path) -> None:
    # Go keys semantic call targets at the NAME token while span keys sit at
    # the `func` keyword; both persisted columns must key the same record.
    repo = tmp_path / "proj"
    repo.mkdir()
    graph = _LocGraph(
        functions=[
            {
                cs.KEY_QUALIFIED_NAME: "proj.pkg.util.Do",
                cs.KEY_LABEL: "Function",
                "module_qn": "proj.pkg.util",
                cs.KEY_START_LINE: 10,
                cs.KEY_START_COL: 0,
                cs.KEY_NAME_START_COL: 5,
            }
        ],
        methods=[
            {
                cs.KEY_QUALIFIED_NAME: "proj.pkg.store.Disk.Put",
                cs.KEY_LABEL: "Method",
                "container_qn": "proj.pkg.store.Disk",
                "module_qn": "proj.pkg.store",
                cs.KEY_START_LINE: 22,
                cs.KEY_START_COL: 0,
                cs.KEY_NAME_START_COL: 18,
            }
        ],
    )
    updater = _updater(repo, graph)
    dp = updater.factory.definition_processor

    updater._rehydrate_function_locations()

    span = dp.function_locations[("proj.pkg.util", 10, 0)]
    alias = dp.function_locations[("proj.pkg.util", 10, 5)]
    assert span == alias
    assert span.qualified_name == "proj.pkg.util.Do"
    method = dp.function_locations[("proj.pkg.store", 22, 18)]
    assert method.container_qn == "proj.pkg.store.Disk"
    assert method.label == "Method"


def test_alias_keys_at_the_name_tokens_own_line(tmp_path: Path) -> None:
    # A multiline Go receiver puts the NAME on a later line than the
    # declaration start; the alias must key at the name's persisted line,
    # never the declaration's (review on #1318).
    repo = tmp_path / "proj"
    repo.mkdir()
    graph = _LocGraph(
        methods=[
            {
                cs.KEY_QUALIFIED_NAME: "proj.pkg.s.Wide.Do",
                cs.KEY_LABEL: "Method",
                "container_qn": "proj.pkg.s.Wide",
                "module_qn": "proj.pkg.s",
                cs.KEY_START_LINE: 5,
                cs.KEY_START_COL: 0,
                cs.KEY_NAME_START_LINE: 7,
                cs.KEY_NAME_START_COL: 2,
            }
        ]
    )
    updater = _updater(repo, graph)
    dp = updater.factory.definition_processor

    updater._rehydrate_function_locations()

    assert ("proj.pkg.s", 5, 0) in dp.function_locations
    assert ("proj.pkg.s", 7, 2) in dp.function_locations
    assert ("proj.pkg.s", 5, 2) not in dp.function_locations


def test_function_rehydration_never_overwrites_fresh_entries(tmp_path: Path) -> None:
    from codebase_rag.types_defs import FunctionLocation

    repo = tmp_path / "proj"
    repo.mkdir()
    graph = _LocGraph(
        functions=[
            {
                cs.KEY_QUALIFIED_NAME: "proj.m.stale",
                cs.KEY_LABEL: "Function",
                "module_qn": "proj.m",
                cs.KEY_START_LINE: 4,
                cs.KEY_START_COL: 0,
                cs.KEY_NAME_START_COL: 5,
            }
        ]
    )
    updater = _updater(repo, graph)
    dp = updater.factory.definition_processor
    fresh = FunctionLocation(
        label="Function", qualified_name="proj.m.fresh", container_qn=None
    )
    dp.function_locations[("proj.m", 4, 0)] = fresh

    updater._rehydrate_function_locations()

    assert dp.function_locations[("proj.m", 4, 0)] == fresh
    # The alias key was still free and rehydrates to the persisted record.
    assert dp.function_locations[("proj.m", 4, 5)].qualified_name == "proj.m.stale"


def test_nodes_persist_start_and_name_columns(tmp_path: Path) -> None:
    # The rehydration above only works if writes carry the columns; pin the
    # additive schema on a real parse (language-agnostic writer paths).
    from unittest.mock import MagicMock

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "app.py").write_text(
        "class Widget:\n    def render(self):\n        return 1\n\n"
        "def main():\n    return 2\n",
        encoding="utf-8",
    )
    parsers, queries = load_parsers()
    mock = MagicMock()
    gu.GraphUpdater(
        ingestor=mock, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    by_label: dict[str, list[PropertyDict]] = {}
    for call in mock.ensure_node_batch.call_args_list:
        by_label.setdefault(str(call.args[0]), []).append(call.args[1])
    classes = [p for p in by_label.get("Class", []) if p.get(cs.KEY_NAME) == "Widget"]
    functions = [
        p for p in by_label.get("Function", []) if p.get(cs.KEY_NAME) == "main"
    ]
    methods = [p for p in by_label.get("Method", []) if p.get(cs.KEY_NAME) == "render"]
    assert classes
    assert cs.KEY_START_COL in classes[0]
    assert functions
    assert cs.KEY_START_COL in functions[0]
    assert functions[0][cs.KEY_NAME_START_COL] == 4
    assert methods
    assert methods[0][cs.KEY_START_COL] == 4
