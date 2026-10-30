"""`ContainerFlips`: the Package or Folder each directory a re-ingest touches is.

A directory with a package indicator is a Package, without one a Folder, and
every containment edge under it hangs off whichever node it is (issues #1570,
#1798, #1872). Each test indexes a real repository into the embedded engine,
changes the disk the way an agent edit would, and asks a `ContainerFlips`
built over that graph what the scoped re-ingest must re-parse, re-derive
and prune. The end-to-end re-ingest runs live in test_reingest_package_flip.py.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryProtocol
from codebase_rag.services.graph_service import GraphConnection, GraphIngestor
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.updater.container_flips import ContainerFlips

_FILES = (
    "a.py",
    "pkg/__init__.py",
    "pkg/util.py",
    "pkg/sub/mod.py",
    "plain/x.py",
)

PACKAGE = cs.NodeLabel.PACKAGE.value
FOLDER = cs.NodeLabel.FOLDER.value


class _Graph:
    """A store that holds nothing; `failing` makes every read raise."""

    def __init__(self, failing: bool = False) -> None:
        self.failing = failing
        self.writes: list[tuple[str, PropertyParams | None]] = []

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        if self.failing:
            raise RuntimeError("graph unreachable")
        return []

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        self.writes.append((query, params))


@dataclass
class _Indexed:
    repo: Path
    graph: GraphIngestor
    updater: GraphUpdater

    def flips(self, graph: QueryProtocol | None = None) -> ContainerFlips:
        return ContainerFlips(
            graph=self.graph if graph is None else graph,
            factory=self.updater.factory,
            repo_path=self.repo,
            tree_scan=self.updater.tree_scan,
        )

    def path(self, key: str) -> Path:
        return self.repo / key

    def touch(self, key: str) -> Path:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("X = 1\n", encoding="utf-8")
        return path

    def kinds(self, rel: str) -> set[str]:
        return self.flips().recorded_kinds(self.path(rel))

    def rederive(self, scope: set[str]) -> None:
        # What `reingest` does past its refusal point: derive the new kind,
        # which writes it beside the old one until the prune.
        self.updater.factory.structure_processor.identify_structure(only=scope)
        self.graph.flush_all()


@pytest.fixture
def indexed(temp_repo: Path, tmp_path: Path) -> Iterator[_Indexed]:
    for key in _FILES:
        path = temp_repo / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("X = 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    with GraphIngestor(
        connection=GraphConnection(
            dialect=get_dialect(DIALECT_SQLITE),
            sqlite_path=str(tmp_path / "graph.sqlite"),
        )
    ) as graph:
        graph.ensure_constraints()
        updater = GraphUpdater(
            ingestor=graph,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
            project_name="proj",
            skip_embeddings=True,
        )
        updater.run(force=True)
        yield _Indexed(temp_repo, graph, updater)


class TestRecordedKinds:
    def test_the_graph_names_each_directorys_container(self, indexed: _Indexed) -> None:
        assert indexed.kinds("pkg") == {PACKAGE}
        assert indexed.kinds("plain") == {FOLDER}
        assert indexed.kinds("pkg/sub") == {FOLDER}
        assert indexed.kinds("never/indexed") == set()

    @pytest.mark.parametrize("graph", [None, "failing"])
    def test_cannot_tell_reads_as_empty(
        self, indexed: _Indexed, graph: str | None
    ) -> None:
        flips = ContainerFlips(
            graph=None if graph is None else _Graph(failing=True),
            factory=indexed.updater.factory,
            repo_path=indexed.repo,
            tree_scan=indexed.updater.tree_scan,
        )

        assert flips.recorded_kinds(indexed.path("pkg")) == set()


class TestPackageFlip:
    def test_a_deleted_initializer_demotes_its_directory(
        self, indexed: _Indexed
    ) -> None:
        init = indexed.path("pkg/__init__.py")
        init.unlink()

        flipped, siblings = indexed.flips().package_flip({}, {"pkg/__init__.py": init})

        assert flipped == {"pkg"}
        # Files directly under it re-parse; the subdirectory's do not.
        assert siblings == {"pkg/util.py": indexed.path("pkg/util.py")}

    def test_an_added_initializer_promotes_its_directory(
        self, indexed: _Indexed
    ) -> None:
        init = indexed.touch("plain/__init__.py")

        flipped, siblings = indexed.flips().package_flip(
            {"plain/__init__.py": init}, {}
        )

        assert flipped == {"plain"}
        # The initializer is named already, so it is no sibling to add.
        assert siblings == {"plain/x.py": indexed.path("plain/x.py")}

    def test_an_edited_initializer_flips_nothing(self, indexed: _Indexed) -> None:
        init = indexed.touch("pkg/__init__.py")

        assert indexed.flips().package_flip({"pkg/__init__.py": init}, {}) == (
            set(),
            {},
        )

    def test_only_directories_named_by_an_indicator_are_checked(
        self, indexed: _Indexed
    ) -> None:
        # `pkg` lost its initializer behind this call's back; a scoped call
        # naming another file makes no claim about it.
        indexed.path("pkg/__init__.py").unlink()
        util = indexed.touch("pkg/util.py")

        assert indexed.flips().package_flip({"pkg/util.py": util}, {}) == (
            set(),
            {},
        )

    def test_a_package_deleted_whole_flips_nothing(self, indexed: _Indexed) -> None:
        # Nothing is left to hang files off; the orphan prune removes it.
        shutil.rmtree(indexed.path("pkg"))

        flipped, siblings = indexed.flips().package_flip(
            {}, {"pkg/__init__.py": indexed.path("pkg/__init__.py")}
        )

        assert (flipped, siblings) == (set(), {})

    def test_a_link_or_an_ignored_file_is_no_sibling(self, indexed: _Indexed) -> None:
        indexed.touch("pkg/bundle.min.js")
        indexed.path("pkg/alias.py").symlink_to(indexed.path("pkg/util.py"))
        assert indexed.updater.tree_scan.ignores(indexed.path("pkg/bundle.min.js")), (
            "fixture guard: the walk must ignore the minified bundle"
        )
        init = indexed.path("pkg/__init__.py")
        init.unlink()

        _flipped, siblings = indexed.flips().package_flip({}, {"pkg/__init__.py": init})

        assert set(siblings) == {"pkg/util.py"}

    def test_a_fresh_updater_still_sees_the_flip(self, indexed: _Indexed) -> None:
        # A fresh updater's map is derived from the disk as it is now, so
        # only the graph remembers what the directory was (PR #1835).
        init = indexed.path("pkg/__init__.py")
        init.unlink()
        parsers, queries = load_parsers()
        fresh = GraphUpdater(
            ingestor=indexed.graph,
            repo_path=indexed.repo,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )
        fresh.factory.structure_processor.identify_structure(emit=False)
        flips = ContainerFlips(
            graph=indexed.graph,
            factory=fresh.factory,
            repo_path=indexed.repo,
            tree_scan=fresh.tree_scan,
        )

        flipped, _siblings = flips.package_flip({}, {"pkg/__init__.py": init})

        assert flipped == {"pkg"}


class TestUncontainedDirs:
    # The checkout root holds the Project node, never a container, so every
    # climb that reaches it reports it; the tests look below the root.
    @staticmethod
    def _below_root(dirs: set[str]) -> set[str]:
        return dirs - {"."}

    def test_a_new_directory_and_its_new_parents_need_deriving(
        self, indexed: _Indexed
    ) -> None:
        added = indexed.touch("pkg/new/deeper/f.py")

        new, diverged = indexed.flips().uncontained_dirs({"pkg/new/deeper/f.py": added})

        # The climb stops at `pkg`, which the graph already records right.
        assert new == {"pkg/new/deeper", "pkg/new"}
        assert diverged == set()

    def test_a_directory_recorded_right_needs_nothing(self, indexed: _Indexed) -> None:
        new, diverged = indexed.flips().uncontained_dirs(
            {"pkg/util.py": indexed.path("pkg/util.py")}
        )

        assert (new, diverged) == (set(), set())

    def test_an_ancestor_whose_kind_diverged_is_reported_apart(
        self, indexed: _Indexed
    ) -> None:
        indexed.path("pkg/__init__.py").unlink()

        new, diverged = indexed.flips().uncontained_dirs(
            {"pkg/util.py": indexed.path("pkg/util.py")}
        )

        assert diverged == {"pkg"}
        assert self._below_root(new) == set()

    def test_an_unreadable_store_aborts_the_reingest(self, indexed: _Indexed) -> None:
        with pytest.raises(ex.ReingestAborted):
            indexed.flips(_Graph(failing=True)).uncontained_dirs(
                {"pkg/util.py": indexed.path("pkg/util.py")}
            )


class TestDerivationScope:
    def test_a_flipped_directory_brings_its_children_never_its_parents(
        self, indexed: _Indexed
    ) -> None:
        indexed.touch("pkg/sub/inner/leaf.py")

        assert indexed.flips().derivation_scope({"pkg/sub"}) == {
            "pkg/sub",
            "pkg/sub/inner",
        }

    def test_a_flipped_directory_gone_from_disk_is_scoped_alone(
        self, indexed: _Indexed
    ) -> None:
        assert indexed.flips().derivation_scope({"gone"}) == {"gone"}


class TestPruneFlipped:
    @pytest.mark.parametrize(
        ("rel", "change", "before", "after"),
        [
            ("pkg", "delete", PACKAGE, FOLDER),
            ("plain", "add", FOLDER, PACKAGE),
        ],
        ids=["demote", "promote"],
    )
    def test_the_node_of_the_old_kind_goes_after_the_rederivation(
        self, indexed: _Indexed, rel: str, change: str, before: str, after: str
    ) -> None:
        init = indexed.path(f"{rel}/__init__.py")
        if change == "delete":
            init.unlink()
        else:
            indexed.touch(f"{rel}/__init__.py")
        flips = indexed.flips()
        flipped, _siblings = flips.package_flip(
            {f"{rel}/__init__.py": init} if change == "add" else {},
            {f"{rel}/__init__.py": init} if change == "delete" else {},
        )
        indexed.rederive(flips.derivation_scope(flipped))
        assert indexed.kinds(rel) == {before, after}, (
            "fixture guard: the derivation writes the new kind beside the old"
        )

        flips.prune_flipped(flipped)

        assert indexed.kinds(rel) == {after}

    def test_an_empty_flip_writes_nothing(self, indexed: _Indexed) -> None:
        graph = _Graph()

        indexed.flips(graph).prune_flipped(set())

        assert graph.writes == []
