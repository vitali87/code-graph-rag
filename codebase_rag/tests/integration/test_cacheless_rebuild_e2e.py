"""A rebuild without a hash cache must not accumulate stale graph state.

The cache lives in the repo working tree, so a fresh clone (or a deleted
cache) has none while the shared database still holds the project. Every
file then counts as "new", the per-file delete-before-reingest is skipped,
and symbols renamed or removed since the previous index linger alongside
the fresh parse, stale CALLS/REFERENCES edges included.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _index(
    ingestor: MemgraphIngestor,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        exclude_paths=exclude_paths,
    ).run()


def _function_names(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all(
        "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'renamerepo.' "
        "RETURN f.qualified_name AS qn"
    )
    return {str(row["qn"]) for row in rows}


class TestCachelessRebuild:
    def test_rebuild_without_cache_drops_renamed_symbols(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        repo = tmp_path / "renamerepo"
        repo.mkdir()
        source = repo / "main.py"
        source.write_text("def old_name():\n    return 1\n", encoding="utf-8")
        _index(memgraph_ingestor, repo)
        assert any(
            qn.endswith(".old_name") for qn in _function_names(memgraph_ingestor)
        )

        # A fresh clone: same repo content evolves, but the cache files are
        # gone while the database still holds the previous parse.
        source.write_text("def new_name():\n    return 1\n", encoding="utf-8")
        (repo / cs.HASH_CACHE_FILENAME).unlink()
        (repo / cs.DIR_MTIMES_FILENAME).unlink()

        _index(memgraph_ingestor, repo)
        names = _function_names(memgraph_ingestor)
        assert any(qn.endswith(".new_name") for qn in names), names
        assert not any(qn.endswith(".old_name") for qn in names), names

    def test_rebuild_without_cache_drops_removed_files(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        # A file deleted between the previous index and a cacheless rebuild
        # appears nowhere: not in the (empty) old hashes and not on disk.
        # Orphan pruning reconciles it from the graph's own path listing;
        # this pins that cover.
        repo = tmp_path / "removedrepo"
        repo.mkdir()
        (repo / "kept.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
        (repo / "gone.py").write_text("def gone():\n    return 1\n", encoding="utf-8")
        _index(memgraph_ingestor, repo)

        (repo / "gone.py").unlink()
        (repo / cs.HASH_CACHE_FILENAME).unlink()
        (repo / cs.DIR_MTIMES_FILENAME).unlink()

        _index(memgraph_ingestor, repo)
        rows = memgraph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'removedrepo.' "
            "RETURN f.qualified_name AS qn"
        )
        names = {str(row["qn"]) for row in rows}
        assert any(qn.endswith(".kept") for qn in names), names
        assert not any(qn.endswith(".gone") for qn in names), names

    def test_rebuild_without_cache_drops_newly_excluded_files(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        # A file EXCLUDED between the previous index and a cacheless rebuild
        # still exists on disk, so orphan pruning keeps it, and the empty old
        # hashes cannot mark it deleted. The rebuild must reconcile the
        # graph's own module paths against the current eligible set.
        repo = tmp_path / "exclrepo"
        repo.mkdir()
        (repo / "kept.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
        (repo / "gen.py").write_text("def gen():\n    return 1\n", encoding="utf-8")
        _index(memgraph_ingestor, repo)

        (repo / cs.HASH_CACHE_FILENAME).unlink()
        (repo / cs.DIR_MTIMES_FILENAME).unlink()
        _index(memgraph_ingestor, repo, exclude_paths=frozenset({"gen.py"}))

        rows = memgraph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'exclrepo.' "
            "RETURN f.qualified_name AS qn"
        )
        names = {str(row["qn"]) for row in rows}
        assert any(qn.endswith(".kept") for qn in names), names
        assert not any(qn.endswith(".gen") for qn in names), names

    def test_cacheless_rebuild_spares_sibling_project_with_same_path(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        # Two projects in the shared graph both hold lib/shared.py. The
        # delete-before-reingest of one project's cacheless rebuild must be
        # scoped to that project's qualified names or it takes the sibling's
        # module subtree with it.
        alpha = tmp_path / "alphaproj"
        beta = tmp_path / "betaproj"
        for repo, fn in ((alpha, "alpha_fn"), (beta, "beta_fn")):
            (repo / "lib").mkdir(parents=True)
            (repo / "lib" / "shared.py").write_text(
                f"def {fn}():\n    return 1\n", encoding="utf-8"
            )
            _index(memgraph_ingestor, repo)

        (alpha / cs.HASH_CACHE_FILENAME).unlink()
        (alpha / cs.DIR_MTIMES_FILENAME).unlink()
        _index(memgraph_ingestor, alpha)

        rows = memgraph_ingestor.fetch_all(
            "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'betaproj.' "
            "RETURN f.qualified_name AS qn"
        )
        names = {str(row["qn"]) for row in rows}
        assert any(qn.endswith(".beta_fn") for qn in names), names
