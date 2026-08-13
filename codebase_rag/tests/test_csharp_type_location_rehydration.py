# Issue #1229 (Phase 1): on an incremental run, _join_csharp_partials resolves
# each Roslyn partial-declaration location against csharp_type_locations, which
# Pass 2 fills only for RE-PARSED files. Without rehydration, a partial part in
# an UNCHANGED .cs file has no entry, so its group silently drops below the
# 2-member threshold and the parts stop spanning to each other. These tests
# drive the rehydration + join directly (no C# toolchain, no real DB) via a
# tiny QueryProtocol fake that answers the type-location query from seeded rows.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, ResultRow


class _TypeLocGraph:
    """Answers only CYPHER_ALL_CSHARP_TYPE_LOCATIONS (as the persisted graph
    would for unchanged .cs types); every other query returns []."""

    def __init__(self, rows: list[ResultRow]) -> None:
        self._rows = rows

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(self, *args: object, **kwargs: object) -> None:
        return None

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if query != cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS:
            return []
        prefix = str((params or {}).get(cs.KEY_PROJECT_PREFIX, ""))
        return [
            r
            for r in self._rows
            if str(r.get(cs.KEY_QUALIFIED_NAME, "")).startswith(prefix)
        ]

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _updater(repo: Path, rows: list[ResultRow]) -> gu.GraphUpdater:
    parsers, queries = load_parsers()
    return gu.GraphUpdater(
        ingestor=_TypeLocGraph(rows),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
    )


def _persisted_type(prefix: str, path: str, line: int, name: str) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: f"{prefix}{name}",
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: line,
    }


def test_rehydration_lets_partial_group_span_an_unchanged_file(tmp_path: Path) -> None:
    # Two parts of one partial class in different dirs get distinct tree-sitter
    # qns; Roslyn proves they are one symbol. On incremental, dirA is re-parsed
    # (fresh Pass-2 entry) and dirB is unchanged (rehydrated). The group must
    # form across both.
    repo = tmp_path / "proj"
    repo.mkdir()
    updater = _updater(repo, [_persisted_type("proj.", "dirB/W.cs", 3, "dirB.W")])
    dp = updater.factory.definition_processor

    # Pass-2 result for the re-parsed file only.
    dp.csharp_type_locations[("dirA/W.cs", 5)] = "proj.dirA.W"
    # Roslyn says these two locations are the same partial symbol.
    updater._csharp_partial_decls = [[("dirA/W.cs", 5), ("dirB/W.cs", 3)]]

    updater._rehydrate_csharp_type_locations()
    updater._join_csharp_partials()

    group = dp.csharp_partial_groups.get("proj.dirA.W")
    assert group is not None, dp.csharp_partial_groups
    assert set(group) == {"proj.dirA.W", "proj.dirB.W"}, group


def test_without_rehydration_the_unchanged_part_is_dropped(tmp_path: Path) -> None:
    # Negative control: same setup, but the unchanged part is never rehydrated
    # (empty graph), so the group falls below two members and is not formed --
    # the exact staleness Phase 1 fixes.
    repo = tmp_path / "proj"
    repo.mkdir()
    updater = _updater(repo, [])
    dp = updater.factory.definition_processor

    dp.csharp_type_locations[("dirA/W.cs", 5)] = "proj.dirA.W"
    updater._csharp_partial_decls = [[("dirA/W.cs", 5), ("dirB/W.cs", 3)]]

    updater._rehydrate_csharp_type_locations()
    updater._join_csharp_partials()

    assert dp.csharp_partial_groups == {}


def test_rehydration_keeps_the_fresh_pass2_entry(tmp_path: Path) -> None:
    # A re-parsed file whose type moved lines must keep its FRESH Pass-2 entry;
    # a stale persisted position for the same path must not overwrite it.
    repo = tmp_path / "proj"
    repo.mkdir()
    updater = _updater(
        repo,
        [_persisted_type("proj.", "dirA/W.cs", 5, "dirA.W")],  # stale line
    )
    dp = updater.factory.definition_processor
    dp.csharp_type_locations[("dirA/W.cs", 9)] = "proj.dirA.W"  # fresh Pass-2 line

    updater._rehydrate_csharp_type_locations()

    # The fresh Pass-2 line survives; the join keys off the current fact
    # position, so the stale rehydrated (path, 5) entry is simply never queried.
    assert dp.csharp_type_locations[("dirA/W.cs", 9)] == "proj.dirA.W"


class _NonQueryGraph:
    """An ingestor that does NOT satisfy QueryProtocol (no fetch_all /
    execute_write), so rehydration must no-op rather than crash."""

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(self, *args: object, **kwargs: object) -> None:
        return None

    def flush_all(self) -> None:
        return None


class _RaisingGraph(_TypeLocGraph):
    def __init__(self) -> None:
        super().__init__([])

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        raise RuntimeError("graph read failed")


def test_rehydration_noops_without_query_protocol(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=_NonQueryGraph(), repo_path=repo, parsers=parsers, queries=queries
    )
    dp = updater.factory.definition_processor

    updater._rehydrate_csharp_type_locations()  # must not raise

    assert dp.csharp_type_locations == {}


def test_rehydration_reraises_query_error_on_incremental(tmp_path: Path) -> None:
    # A genuine incremental run (not a full build) cannot silently proceed on a
    # degraded read -- it would drop real partial groups -- so the error raises.
    repo = tmp_path / "proj"
    repo.mkdir()
    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=_RaisingGraph(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater._is_full_build = False

    with pytest.raises(RuntimeError):
        updater._rehydrate_csharp_type_locations()


def test_rehydration_tolerates_query_error_on_full_build(tmp_path: Path) -> None:
    # A full build has every location from Pass 2 already, so a failed read is
    # only a warning -- rehydration returns without touching the map.
    repo = tmp_path / "proj"
    repo.mkdir()
    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=_RaisingGraph(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater._is_full_build = True

    updater._rehydrate_csharp_type_locations()  # must not raise

    assert updater.factory.definition_processor.csharp_type_locations == {}


def test_rehydration_skips_malformed_rows(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    updater = _updater(
        repo,
        [
            {cs.KEY_QUALIFIED_NAME: "proj.Bad", cs.KEY_PATH: "b.cs"},  # no start_line
            {
                cs.KEY_QUALIFIED_NAME: "proj.NotInt",
                cs.KEY_PATH: "c.cs",
                cs.KEY_START_LINE: "x",  # non-int line
            },
            {
                cs.KEY_QUALIFIED_NAME: "proj.BoolLine",
                cs.KEY_PATH: "d.cs",
                cs.KEY_START_LINE: True,  # bool is an int subclass -> reject
            },
            _persisted_type("proj.", "good.cs", 4, "Good"),
        ],
    )
    dp = updater.factory.definition_processor

    updater._rehydrate_csharp_type_locations()

    assert dp.csharp_type_locations == {("good.cs", 4): "proj.Good"}
