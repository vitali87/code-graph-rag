# Real-database check of issue #2665: one directory indexed under two project
# names shares its Folder and File nodes (they are keyed by absolute_path), so
# the containment walk from either Project reached the other's packages.
# `delete-project OLD` then wiped `shop` as well, and `stats -n` counted both.
# Only a real database proves the ownership Cypher parses and filters, and it
# runs on Memgraph and on the embedded engine, the default backend: written
# with path variables, the walk ran on Memgraph alone.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import ResultRow

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


@pytest.fixture(params=["memgraph_ingestor", "sqlite_ingestor"])
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    return request.getfixturevalue(request.param)


CART = "def price(x):\n    return x * 2\n\n\ndef total(xs):\n    return sum(price(x) for x in xs)\n"
LEGACY = "def old_price(x):\n    return x * 3\n"
README = "# Shop\n\nA tiny shop.\n\n## Usage\n\nCall `total`.\n"

_KEY = "coalesce(n.qualified_name, n.absolute_path, n.name, '')"
_NODES = (
    f"MATCH (n) WHERE NOT n:IncompleteRun RETURN labels(n) AS labels, {_KEY} AS key"
)
_RELS = (
    "MATCH (a)-[r]->(b) RETURN type(r) AS type, "
    "coalesce(a.qualified_name, a.absolute_path, a.name, '') AS src, "
    "coalesce(b.qualified_name, b.absolute_path, b.name, '') AS dst"
)

Snapshot = tuple[list[tuple[str, ...]], list[tuple[str, ...]]]


def _repo(root: Path, legacy: bool = False) -> Path:
    repo = root / "shop-demo"
    pkg = repo / "src" / "shop"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cart.py").write_text(CART, encoding="utf-8")
    (repo / "README.md").write_text(README, encoding="utf-8")
    if legacy:
        (pkg / "legacy.py").write_text(LEGACY, encoding="utf-8")
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path, project: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)
    ingestor.flush_all()


def _node_row(row: ResultRow) -> tuple[str, ...]:
    labels = row["labels"]
    names = [str(label) for label in labels] if isinstance(labels, list) else []
    return (*sorted(names), str(row["key"]))


def _snapshot(ingestor: MemgraphIngestor) -> Snapshot:
    nodes = sorted(_node_row(row) for row in ingestor.fetch_all(_NODES))
    rels = sorted(
        (str(row["type"]), str(row["src"]), str(row["dst"]))
        for row in ingestor.fetch_all(_RELS)
    )
    return nodes, rels


def _wipe(ingestor: MemgraphIngestor) -> None:
    ingestor._execute_query("MATCH (n) DETACH DELETE n")


def _counts(rows: list[ResultRow], key: str) -> dict[str, int]:
    counted: dict[str, int] = {}
    for row in rows:
        raw = row[key]
        label = str(raw[0]) if isinstance(raw, list) else str(raw)
        counted[label] = counted.get(label, 0) + int(row["count"])
    return counted


def _scoped(
    ingestor: MemgraphIngestor, project: str
) -> tuple[dict[str, int], dict[str, int]]:
    params = {"project_names": [project]}
    return (
        _counts(
            ingestor.fetch_all(cq.CYPHER_STATS_PROJECT_NODE_COUNTS, params), "labels"
        ),
        _counts(
            ingestor.fetch_all(cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS, params),
            "type",
        ),
    )


def _alone(ingestor: MemgraphIngestor, repo: Path, project: str) -> Snapshot:
    _index(ingestor, repo, project)
    snapshot = _snapshot(ingestor)
    _wipe(ingestor)
    return snapshot


@pytest.mark.parametrize(("keep", "drop"), [("shop", "old"), ("old", "shop")])
def test_deleting_one_name_leaves_the_other_exactly_as_if_alone(
    ingestor: MemgraphIngestor, tmp_path: Path, keep: str, drop: str
) -> None:
    repo = _repo(tmp_path)
    expected = _alone(ingestor, repo, keep)
    _index(ingestor, repo, "old")
    _index(ingestor, repo, "shop")

    ingestor.delete_project(drop)

    assert _snapshot(ingestor) == expected


def test_a_file_only_the_deleted_name_indexed_goes_with_it(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # `old` indexed legacy.py, which was removed before `shop` indexed the
    # same directory: that File hangs only off `old`'s package, so it must go
    # with `old` rather than stay behind as an orphan.
    repo = _repo(tmp_path, legacy=True)
    (repo / "src" / "shop" / "legacy.py").unlink()
    expected = _alone(ingestor, repo, "shop")
    (repo / "src" / "shop" / "legacy.py").write_text(LEGACY, encoding="utf-8")
    _index(ingestor, repo, "old")
    (repo / "src" / "shop" / "legacy.py").unlink()
    _index(ingestor, repo, "shop")

    ingestor.delete_project("old")

    assert _snapshot(ingestor) == expected


def test_scoped_stats_count_one_name_as_if_it_were_alone(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _index(ingestor, repo, "shop")
    alone = _scoped(ingestor, "shop")
    _index(ingestor, repo, "old")

    assert _scoped(ingestor, "shop") == alone
    assert _scoped(ingestor, "old") == alone


def test_per_project_breakdown_counts_each_name_once(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = _repo(tmp_path)
    _index(ingestor, repo, "shop")
    (alone,) = ingestor.fetch_all(cq.CYPHER_STATS_PER_PROJECT)
    _index(ingestor, repo, "old")

    rows = {
        str(row["project"]): (row["nodes"], row["relationships"])
        for row in ingestor.fetch_all(cq.CYPHER_STATS_PER_PROJECT)
    }

    expected = (alone["nodes"], alone["relationships"])
    assert rows == {"old": expected, "shop": expected}


def test_a_later_sync_does_not_purge_the_shared_directory_nodes(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # ensure_constraints runs on every sync. Its #897 repair read any
    # Folder/File with two project parents as legacy damage and purged it,
    # which cut `old`'s packages off from `old` and dropped File nodes `shop`
    # still needed. Both projects record the same root_path: not damage.
    repo = _repo(tmp_path)
    _index(ingestor, repo, "old")
    _index(ingestor, repo, "shop")
    before = _snapshot(ingestor)

    ingestor.ensure_constraints()

    assert _snapshot(ingestor) == before


@pytest.mark.parametrize("outer_first", [True, False])
def test_a_later_sync_keeps_what_a_nested_root_shares(
    ingestor: MemgraphIngestor, tmp_path: Path, outer_first: bool
) -> None:
    # One project at the repository root, one at a subdirectory of it: the
    # files under the subdirectory are one File node for both, but the
    # projects record different root_paths. That is not the #897 damage,
    # which shared nodes between projects in DIFFERENT directories.
    repo = _repo(tmp_path)
    order = [(repo, "mono"), (repo / "src", "svc")]
    for path, project in order if outer_first else reversed(order):
        _index(ingestor, path, project)
    before = _snapshot(ingestor)

    ingestor.ensure_constraints()

    assert _snapshot(ingestor) == before


@pytest.mark.parametrize(
    ("first", "second", "node", "damaged"),
    [
        # One layout in two directories: what the old relative key merged.
        ("/w/a", "/w/b", "/w/a/app/lib", True),
        # A repository and its subdirectory, recorded with Windows separators.
        ("C:\\w\\a", "C:\\w\\a\\app", "C:/w/a/app/lib", False),
        # A filesystem root already ends in the separator.
        ("/", "/w/a", "/w/a/lib", False),
        ("C:\\", "C:\\w\\a", "C:/w/a/lib", False),
    ],
)
def test_a_sync_reads_each_root_against_the_shared_node(
    ingestor: MemgraphIngestor,
    first: str,
    second: str,
    node: str,
    damaged: bool,
) -> None:
    ingestor._execute_query(
        "CREATE (a:Project {name: 'a', root_path: $first}), "
        "(b:Project {name: 'b', root_path: $second}), "
        "(n:Folder {absolute_path: $node}), "
        "(a)-[:CONTAINS_FOLDER]->(n), (b)-[:CONTAINS_FOLDER]->(n)",
        {"first": first, "second": second, "node": node},
    )

    ingestor.ensure_constraints()

    (row,) = ingestor.fetch_all("MATCH (n:Folder) RETURN count(n) AS c")
    assert row["c"] == (0 if damaged else 1)


def test_a_tree_already_cut_off_from_its_project_goes_with_it(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A graph the old purge already repaired: `old`'s packages survive but no
    # containment path leads to them from `old` any more.
    repo = _repo(tmp_path)
    expected = _alone(ingestor, repo, "shop")
    _index(ingestor, repo, "old")
    _index(ingestor, repo, "shop")
    ingestor._execute_query("MATCH (:Project {name: 'old'})-[r]->() DELETE r")

    ingestor.delete_project("old")

    assert _snapshot(ingestor) == expected


# Negative: what must not change.


def test_a_lone_project_is_still_deleted_completely(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(ingestor, _repo(tmp_path), "shop")

    ingestor.delete_project("shop")

    nodes, rels = _snapshot(ingestor)
    structural = {"Project", "Folder", "File", "Package", "Module", "Function"}
    assert [n for n in nodes if structural & set(n)] == []
    assert rels == []


def test_projects_in_separate_directories_are_untouched(
    ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    first = _repo(tmp_path / "a")
    second = _repo(tmp_path / "b")
    expected = _alone(ingestor, first, "shop")
    _index(ingestor, first, "shop")
    _index(ingestor, second, "other")

    ingestor.delete_project("other")

    assert _snapshot(ingestor) == expected
