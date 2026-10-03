# Issue #2451, against Memgraph: the orphan prune deletes a Package by its
# qualified name, scoped to the project and repository it read the row from.
# A Package derived through an in-repo directory link shared its target's
# absolute path, so the old delete by that path took the real Package too.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import structure_processor
from codebase_rag.utils import path_utils

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def _updater(ingestor: MemgraphIngestor, repo: Path, project: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )


def _packages(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all("MATCH (p:Package) RETURN p.qualified_name AS qn")
    return {str(row["qn"]) for row in rows}


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "m.py").write_text("X = 1\n")
    return repo


def test_an_upgraded_sync_drops_a_linked_package_and_keeps_its_target(
    memgraph_ingestor: MemgraphIngestor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path, "repo")
    (repo / "app").mkdir()
    try:
        (repo / "app" / "vendored").symlink_to("../pkg", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks need privileges on this host")
    # The graph a build before the fix left: the link's own Package.
    with monkeypatch.context() as patched:
        for module in (path_utils, structure_processor):
            patched.setattr(module, "is_symlink_entry", lambda _path: False)
        _updater(memgraph_ingestor, repo, "proj").run(force=True)
    assert {"proj.pkg", "proj.app.vendored"} <= _packages(memgraph_ingestor)
    # The upgrade changes the parser fingerprint (the fix edits fingerprinted
    # sources), which is what sends the first sync past the in-sync fast path.
    (repo / cs.PARSER_FINGERPRINT_FILENAME).unlink()

    _updater(memgraph_ingestor, repo, "proj").run()

    packages = _packages(memgraph_ingestor)
    assert "proj.app.vendored" not in packages
    assert "proj.pkg" in packages


def test_pruning_one_project_leaves_another_projects_package(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    alpha = _repo(tmp_path, "alpha")
    beta = _repo(tmp_path, "beta")
    _updater(memgraph_ingestor, alpha, "alpha").run(force=True)
    _updater(memgraph_ingestor, beta, "beta").run(force=True)

    (alpha / "pkg" / "__init__.py").unlink()
    _updater(memgraph_ingestor, alpha, "alpha").run()

    packages = _packages(memgraph_ingestor)
    assert "alpha.pkg" not in packages
    assert "beta.pkg" in packages


def test_the_package_delete_query_is_scoped(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    mine = (tmp_path / "mine").as_posix()
    other = (tmp_path / "other").as_posix()
    memgraph_ingestor.execute_write(
        "CREATE (:Package {qualified_name: 'shared.pkg', absolute_path: $path})",
        {"path": f"{other}/pkg"},
    )

    def delete(project: str, absolute_path: str) -> None:
        memgraph_ingestor.execute_write(
            cs.CYPHER_DELETE_PACKAGE_BY_QN,
            {
                cs.KEY_QUALIFIED_NAME: "shared.pkg",
                cs.KEY_PROJECT_NAME: project,
                cs.KEY_PROJECT_PREFIX: f"{project}.",
                cs.KEY_ABSOLUTE_PATH: absolute_path,
            },
        )

    # Another checkout under the same name, and another project at this path.
    delete("shared", f"{mine}/pkg")
    delete("elsewhere", f"{other}/pkg")
    assert "shared.pkg" in _packages(memgraph_ingestor)

    delete("shared", f"{other}/pkg")
    assert "shared.pkg" not in _packages(memgraph_ingestor)
