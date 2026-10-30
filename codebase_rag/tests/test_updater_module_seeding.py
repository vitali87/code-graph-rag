"""`ModuleQnSeeding`: the module-qn claims an incremental run starts from.

Pinned directly, over a scripted graph and a real definition processor's
module map: which graph rows seed the map, which stems an added or deleted
file puts in flux and what happens to their survivors' claims (issues
#1569, #2022, #2232, #2445), and when the seeded map may be pruned (issues
#1712, #2404). One test seeds from a graph the embedded engine built, so the
row shape the seeding reads is the one a real index writes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryProtocol
from codebase_rag.services.graph_service import GraphConnection, GraphIngestor
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.updater.module_seeding import (
    ModuleQnSeeding,
    flux_stems_of,
    stem_key,
)


class _Graph:
    """Answers each query from scripted rows; `failing` raises."""

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


class _Remover:
    """Records each file the seeding asks the updater to forget."""

    def __init__(self) -> None:
        self.removed: list[tuple[Path, bool]] = []

    def remove_file_from_state(
        self, file_path: Path, frontend_current: bool = False
    ) -> None:
        self.removed.append((file_path, frontend_current))


def _owns(qn: str) -> bool:
    # `proj.v2` is a nested project: its rows share the `proj.` prefix.
    return (qn == "proj" or qn.startswith("proj.")) and not qn.startswith("proj.v2")


def _owned_rows(rows: list[ResultRow], key: str) -> list[ResultRow]:
    return [row for row in rows if isinstance(qn := row.get(key), str) and _owns(qn)]


def _module(qn: str, path: str) -> ResultRow:
    return {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: path}


class _Harness:
    def __init__(self, repo: Path, graph: QueryProtocol | None) -> None:
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=_Graph(),
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )
        self.repo = repo
        self.module_map = updater.factory.definition_processor.module_qn_to_file_path
        self.frontend_runner = updater.frontend_runner
        self.parsed_files: list[tuple[Path, cs.SupportedLanguage]] = []
        self.rehydrated: set[str] = set()
        self.remover = _Remover()
        self.seeding = ModuleQnSeeding(
            graph=graph,
            factory=updater.factory,
            repo_path=repo,
            project_name="proj",
            owns=_owns,
            owned_rows=_owned_rows,
            parsed_files=self.parsed_files,
            rehydrated_module_qns=self.rehydrated,
            frontend_runner=self.frontend_runner,
            remover=self.remover,
        )

    def write(self, *keys: str) -> None:
        for key in keys:
            path = self.repo / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")


class TestStems:
    @pytest.mark.parametrize(
        ("key", "stem"),
        [
            ("pkg/util.py", "pkg/util"),
            ("util.h", "util"),
            ("types/lib.d.ts", "types/lib.d"),
            ("Makefile", "Makefile"),
        ],
    )
    def test_a_stem_is_the_path_without_its_last_suffix(
        self, key: str, stem: str
    ) -> None:
        assert stem_key(key) == stem

    def test_a_file_puts_its_own_stem_in_flux(self) -> None:
        assert flux_stems_of("pkg/util.rs") == {"pkg/util"}

    @pytest.mark.parametrize("init", ["__init__.py", "__init__.pyi"])
    def test_a_package_initializer_also_puts_its_modules_stem_in_flux(
        self, init: str
    ) -> None:
        # `x/__init__.py` names `proj.x`, as `x.py` and `x.pyi` do (#2445).
        assert flux_stems_of(f"pkg/x/{init}") == {
            stem_key(f"pkg/x/{init}"),
            "pkg/x",
        }

    def test_the_root_initializer_names_no_sibling_module(self) -> None:
        assert flux_stems_of("__init__.py") == {"__init__"}


class TestSeedFromGraph:
    def test_only_owned_surviving_file_modules_are_seeded(
        self, temp_repo: Path
    ) -> None:
        graph = _Graph(
            {
                cs.CYPHER_ALL_MODULE_PATHS_INTERNAL: [
                    _module("proj.shapes", "shapes.rs"),
                    _module("proj.gone", "gone.py"),
                    _module("proj.v2.api", "api.py"),
                    _module("proj.lib.inner", "inline_module_inner"),
                    _module("proj.blank", ""),
                    {cs.KEY_QUALIFIED_NAME: None, cs.KEY_PATH: "x.py"},
                ]
            }
        )
        h = _Harness(temp_repo, graph)

        h.seeding.seed_module_qns_from_graph(
            {"shapes.rs", "api.py", "inline_module_inner", "x.py"}
        )

        assert dict(h.module_map) == {"proj.shapes": temp_repo / "shapes.rs"}
        assert graph.reads == [(cs.CYPHER_ALL_MODULE_PATHS_INTERNAL, None)]

    def test_a_claim_already_in_memory_is_kept(self, temp_repo: Path) -> None:
        graph = _Graph(
            {cs.CYPHER_ALL_MODULE_PATHS_INTERNAL: [_module("proj.util", "util.h")]}
        )
        h = _Harness(temp_repo, graph)
        h.module_map["proj.util"] = temp_repo / "util.c"

        h.seeding.seed_module_qns_from_graph({"util.h", "util.c"})

        assert dict(h.module_map) == {"proj.util": temp_repo / "util.c"}

    def test_a_readable_flux_survivor_reparses_unseeded(self, temp_repo: Path) -> None:
        h = _Harness(
            temp_repo,
            _Graph(
                {cs.CYPHER_ALL_MODULE_PATHS_INTERNAL: [_module("proj.util", "util.h")]}
            ),
        )
        h.write("util.h")
        h.rehydrated.add("proj.util")

        h.seeding.seed_module_qns_from_graph({"util.h", "util.c"}, {"util"})

        assert "proj.util" not in h.module_map
        # Not offered to import verification as a live module either (#2586).
        assert "proj.util" not in h.rehydrated
        # The claim comes back should the survivor's read fail (#2232).
        h.seeding.restore_unreadable_flux_claims({"util.h"})
        assert h.module_map["proj.util"] == temp_repo / "util.h"

    def test_an_unopenable_flux_survivor_keeps_its_claim(self, temp_repo: Path) -> None:
        # Not on disk: it cannot be re-parsed, so its subtree stays and so
        # must its qn, or the added sibling would take it over that subtree.
        h = _Harness(
            temp_repo,
            _Graph(
                {cs.CYPHER_ALL_MODULE_PATHS_INTERNAL: [_module("proj.util", "util.h")]}
            ),
        )

        h.seeding.seed_module_qns_from_graph({"util.h", "util.c"}, {"util"})

        assert h.module_map["proj.util"] == temp_repo / "util.h"

    def test_a_failed_read_aborts_the_run(self, temp_repo: Path) -> None:
        # A missing seed lets an added file overwrite a sibling's module qn.
        h = _Harness(temp_repo, _Graph(failing={cs.CYPHER_ALL_MODULE_PATHS_INTERNAL}))

        with pytest.raises(RuntimeError, match="graph unreachable"):
            h.seeding.seed_module_qns_from_graph({"a.py"})

    def test_a_write_only_sink_seeds_nothing(self, temp_repo: Path) -> None:
        h = _Harness(temp_repo, None)

        h.seeding.seed_module_qns_from_graph({"a.py"})

        assert dict(h.module_map) == {}


class TestPrepareFluxStems:
    def test_an_added_sibling_puts_its_stem_in_flux_and_frees_the_claim(
        self, temp_repo: Path
    ) -> None:
        graph = _Graph(
            {
                cs.CYPHER_ALL_MODULE_PATHS_INTERNAL: [
                    _module("proj.util", "util.h"),
                    _module("proj.main", "main.c"),
                ]
            }
        )
        h = _Harness(temp_repo, graph)
        h.write("util.h", "util.c", "main.c")
        # A reused updater still holds the claim its last run wrote (#2022).
        h.module_map["proj.util"] = temp_repo / "util.h"
        eligible = [(temp_repo / key, key) for key in ("main.c", "util.c", "util.h")]

        flux = h.seeding.prepare_flux_stems(
            eligible, {"util.h": "h1", "main.c": "h2"}, is_full_build=False
        )

        assert flux == {"util"}
        assert h.remover.removed == [(temp_repo / "util.h", False)]
        assert dict(h.module_map) == {"proj.main": temp_repo / "main.c"}
        h.seeding.restore_unreadable_flux_claims({"util.h", "main.c"})
        assert h.module_map["proj.util"] == temp_repo / "util.h"

    def test_a_deleted_package_initializer_frees_the_stub_module(
        self, temp_repo: Path
    ) -> None:
        h = _Harness(temp_repo, _Graph())
        h.write("pkg/x.pyi")
        h.module_map["proj.pkg.x"] = temp_repo / "pkg" / "x.pyi"

        flux = h.seeding.prepare_flux_stems(
            [(temp_repo / "pkg" / "x.pyi", "pkg/x.pyi")],
            {"pkg/x.pyi": "h1", "pkg/x/__init__.py": "h2"},
            is_full_build=False,
        )

        assert "pkg/x" in flux
        assert h.remover.removed == [(temp_repo / "pkg" / "x.pyi", False)]
        assert "proj.pkg.x" not in h.module_map

    def test_a_libclang_covered_survivor_keeps_its_frontend_registrations(
        self, temp_repo: Path
    ) -> None:
        h = _Harness(temp_repo, _Graph())
        h.write("util.h", "util.c")
        h.module_map["proj.util"] = temp_repo / "util.h"
        h.frontend_runner.cpp_covered = frozenset({"util.h"})

        h.seeding.prepare_flux_stems(
            [(temp_repo / "util.h", "util.h"), (temp_repo / "util.c", "util.c")],
            {"util.h": "h1"},
            is_full_build=False,
        )

        assert h.remover.removed == [(temp_repo / "util.h", True)]

    def test_a_module_outside_the_checkout_is_on_no_stem(
        self, temp_repo: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "vendor" / "util.h"
        outside.parent.mkdir()
        outside.write_text("", encoding="utf-8")
        h = _Harness(temp_repo, _Graph())
        h.write("util.c")
        h.module_map["proj.vendor.util"] = outside

        h.seeding.prepare_flux_stems(
            [(temp_repo / "util.c", "util.c")], {}, is_full_build=False
        )

        assert h.remover.removed == []
        assert h.module_map["proj.vendor.util"] == outside

    def test_a_full_build_has_no_flux_and_reads_nothing(self, temp_repo: Path) -> None:
        graph = _Graph()
        h = _Harness(temp_repo, graph)
        h.write("util.h", "util.c")
        h.module_map["proj.util"] = temp_repo / "util.h"

        flux = h.seeding.prepare_flux_stems(
            [(temp_repo / "util.c", "util.c"), (temp_repo / "util.h", "util.h")],
            {"util.h": "h1"},
            is_full_build=True,
        )

        assert flux == set()
        assert graph.reads == []
        assert h.remover.removed == []
        assert h.module_map["proj.util"] == temp_repo / "util.h"

    def test_a_new_run_does_not_restore_an_earlier_runs_claim(
        self, temp_repo: Path
    ) -> None:
        h = _Harness(temp_repo, _Graph())
        h.write("util.h", "util.c")
        h.module_map["proj.util"] = temp_repo / "util.h"
        both = [(temp_repo / "util.c", "util.c"), (temp_repo / "util.h", "util.h")]
        h.seeding.prepare_flux_stems(both, {"util.h": "h1"}, is_full_build=False)

        h.seeding.prepare_flux_stems(
            both, dict.fromkeys(("util.h", "util.c"), "h"), is_full_build=False
        )
        h.seeding.restore_unreadable_flux_claims({"util.h"})

        assert "proj.util" not in h.module_map


class TestPruneStaleSeeds:
    @staticmethod
    def _graph(*qns: str, failing: bool = False) -> _Graph:
        return _Graph(
            {cs.CYPHER_PROJECT_MODULE_PATHS: [_module(qn, "") for qn in qns]},
            failing={cs.CYPHER_PROJECT_MODULE_PATHS} if failing else set(),
        )

    def test_a_module_the_graph_lost_is_dropped(self, temp_repo: Path) -> None:
        graph = self._graph("proj.kept")
        h = _Harness(temp_repo, graph)
        h.module_map["proj.kept"] = temp_repo / "kept.py"
        h.module_map["proj.deleted_elsewhere"] = temp_repo / "deleted_elsewhere.py"

        h.seeding.prune_stale_seeded_module_qns()

        assert dict(h.module_map) == {"proj.kept": temp_repo / "kept.py"}
        assert graph.reads == [
            (
                cs.CYPHER_PROJECT_MODULE_PATHS,
                {cs.KEY_PROJECT_PREFIX: "proj.", cs.KEY_PROJECT_NAME: "proj"},
            )
        ]

    def test_a_module_parsed_this_run_survives_an_unflushed_graph(
        self, temp_repo: Path
    ) -> None:
        h = _Harness(temp_repo, self._graph("proj.kept"))
        h.module_map["proj.kept"] = temp_repo / "kept.py"
        h.module_map["proj.fresh"] = temp_repo / "fresh.py"
        h.parsed_files.append((temp_repo / "fresh.py", cs.SupportedLanguage.PYTHON))

        h.seeding.prune_stale_seeded_module_qns()

        assert set(h.module_map) == {"proj.kept", "proj.fresh"}

    def test_exempt_paths_replace_the_runs_parsed_files(self, temp_repo: Path) -> None:
        # `reingest` names what IT re-parsed: `_parsed_files` on a retained
        # updater holds every file since the process started (#1712).
        h = _Harness(temp_repo, self._graph("proj.kept"))
        h.module_map["proj.kept"] = temp_repo / "kept.py"
        h.module_map["proj.old"] = temp_repo / "old.py"
        h.module_map["proj.reparsed"] = temp_repo / "reparsed.py"
        h.parsed_files.append((temp_repo / "old.py", cs.SupportedLanguage.PYTHON))

        h.seeding.prune_stale_seeded_module_qns({temp_repo / "reparsed.py"})

        assert set(h.module_map) == {"proj.kept", "proj.reparsed"}

    def test_a_first_sync_reads_nothing(self, temp_repo: Path) -> None:
        # Every entry was parsed by this run: nothing to decide (#2404).
        graph = self._graph()
        h = _Harness(temp_repo, graph)
        h.module_map["proj.a"] = temp_repo / "a.py"
        h.parsed_files.append((temp_repo / "a.py", cs.SupportedLanguage.PYTHON))

        h.seeding.prune_stale_seeded_module_qns()

        assert graph.reads == []
        assert set(h.module_map) == {"proj.a"}

    @pytest.mark.parametrize("failing", [False, True], ids=["empty", "unreadable"])
    def test_no_rows_is_no_verdict(self, temp_repo: Path, failing: bool) -> None:
        h = _Harness(temp_repo, self._graph(failing=failing))
        h.module_map["proj.a"] = temp_repo / "a.py"
        h.module_map["proj.b"] = temp_repo / "b.py"

        h.seeding.prune_stale_seeded_module_qns()

        assert set(h.module_map) == {"proj.a", "proj.b"}

    def test_a_nested_projects_modules_are_no_evidence(self, temp_repo: Path) -> None:
        # Its rows pass the prefix filter but are filtered out as not owned,
        # leaving no verdict rather than a verdict that drops everything.
        h = _Harness(temp_repo, self._graph("proj.v2.a"))
        h.module_map["proj.a"] = temp_repo / "a.py"

        h.seeding.prune_stale_seeded_module_qns()

        assert set(h.module_map) == {"proj.a"}


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


def test_seeding_from_a_real_index_claims_each_surviving_files_module(
    temp_repo: Path, embedded_graph: GraphIngestor
) -> None:
    (temp_repo / "pkg").mkdir()
    for key in ("pkg/__init__.py", "pkg/a.py", "b.py", "c.py"):
        (temp_repo / key).write_text("X = 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=embedded_graph,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        skip_embeddings=True,
    ).run(force=True)
    h = _Harness(temp_repo, embedded_graph)

    # `c.py` was deleted since: its module is not claimed.
    h.seeding.seed_module_qns_from_graph({"pkg/__init__.py", "pkg/a.py", "b.py"})

    assert dict(h.module_map) == {
        "proj.pkg": temp_repo / "pkg" / "__init__.py",
        "proj.pkg.a": temp_repo / "pkg" / "a.py",
        "proj.b": temp_repo / "b.py",
    }
