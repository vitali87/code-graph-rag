"""Projects over the same files keep their Folder/File nodes across syncs.

Folder and File nodes MERGE on absolute_path, so one checkout indexed under
two project names shares them by design. The start-up migration for the superseded relative-path key
(issue #897) treated any node more than one Project reaches as that key's
merge and deleted it, so the next sync of ANY project emptied both trees
(issue #3025). Only a node outside an owner's tree is the old key's merge.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_STRUCTURE = (
    "MATCH (:Project {name: $name})"
    "-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(n) "
    "WHERE n:File OR n:Folder RETURN count(DISTINCT n) AS n"
)


def _index(ingestor: MemgraphIngestor, root: Path, name: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=True)
    ingestor.flush_all()


def _structure(ingestor: MemgraphIngestor, name: str) -> int:
    return int(ingestor.fetch_all(_STRUCTURE, {"name": name})[0]["n"])


def _shop(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg" / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    return root


def test_two_names_on_one_checkout_survive_another_sync(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    ing = memgraph_ingestor
    ing.ensure_constraints()
    shop = _shop(tmp_path / "twonames")
    other = tmp_path / "otherproj"
    other.mkdir()
    (other / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    _index(ing, shop, "shopsvc")
    _index(ing, shop, "shop-svc")
    before = (_structure(ing, "shopsvc"), _structure(ing, "shop-svc"))
    assert before[0] > 0, before
    assert before[1] == before[0], before

    # Every sync, of any project, starts with the migration.
    ing.ensure_constraints()
    _index(ing, other, "unrelated")

    after = (_structure(ing, "shopsvc"), _structure(ing, "shop-svc"))
    assert after == before


def test_windows_roots_keep_the_nodes_they_share(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # Project.root_path is the resolved path as the OS spells it, so on
    # Windows it carries backslashes, while absolute_path is always POSIX.
    # Compared unnormalised, every shared node read as outside its owners'
    # trees and was purged (review of PR 3216).
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (svc:Project {name: 'svc', root_path: $root}), "
        "(copy:Project {name: 'svc-copy', root_path: $root}), "
        "(app:Folder {path: 'app', absolute_path: $app}), "
        "(main:File {path: 'app/main.py', absolute_path: $main}), "
        "(svc)-[:CONTAINS_FOLDER]->(app), "
        "(copy)-[:CONTAINS_FOLDER]->(app), "
        "(app)-[:CONTAINS_FILE]->(main)",
        {
            "root": "C:\\repos\\svc",
            "app": "C:/repos/svc/app",
            "main": "C:/repos/svc/app/main.py",
        },
    )

    ing.ensure_constraints()

    assert _structure(ing, "svc") == 2
    assert _structure(ing, "svc-copy") == 2


@pytest.mark.parametrize(
    ("root", "nested", "app", "main"),
    [
        ("/", "/app", "/app", "/app/main.py"),
        ("C:\\", "C:\\app", "C:/app", "C:/app/main.py"),
    ],
    ids=["posix", "windows"],
)
def test_filesystem_roots_keep_the_nodes_they_share(
    memgraph_ingestor: MemgraphIngestor, root: str, nested: str, app: str, main: str
) -> None:
    # A project at `/` or a drive root records a root that already ends in
    # the separator; with another one appended, `//` or `C://` prefixed no
    # child path, so every node it shares read as outside its tree and was
    # purged (review of PR 3216).
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (top:Project {name: 'top', root_path: $root}), "
        "(copy:Project {name: 'top-copy', root_path: $root}), "
        "(inner:Project {name: 'inner', root_path: $nested}), "
        "(app:Folder {path: 'app', absolute_path: $app}), "
        "(main:File {path: 'app/main.py', absolute_path: $main}), "
        "(top)-[:CONTAINS_FOLDER]->(app), "
        "(copy)-[:CONTAINS_FOLDER]->(app), "
        "(inner)-[:CONTAINS_FILE]->(main), "
        "(app)-[:CONTAINS_FILE]->(main)",
        {"root": root, "nested": nested, "app": app, "main": main},
    )

    ing.ensure_constraints()

    assert _structure(ing, "top") == 2
    assert _structure(ing, "top-copy") == 2
    assert _structure(ing, "inner") == 1


@pytest.mark.parametrize(
    ("svc_root", "cli_root", "shared", "own"),
    [
        ("/repos/svc", "/repos/cli", "/repos/svc/app", "/repos/cli/lib"),
        ("C:\\repos\\svc", "C:\\repos\\cli", "C:/repos/svc/app", "C:/repos/cli/lib"),
    ],
    ids=["posix", "windows"],
)
def test_a_cross_directory_merge_is_still_purged(
    memgraph_ingestor: MemgraphIngestor,
    svc_root: str,
    cli_root: str,
    shared: str,
    own: str,
) -> None:
    # Negative: two projects in different directories reaching one node is
    # the old key's merge even when both record their roots, so it goes.
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (svc:Project {name: 'svc', root_path: $svc_root}), "
        "(cli:Project {name: 'cli', root_path: $cli_root}), "
        "(shared:Folder {path: 'app', absolute_path: $shared}), "
        "(own:Folder {path: 'lib', absolute_path: $own}), "
        "(svc)-[:CONTAINS_FOLDER]->(shared), "
        "(cli)-[:CONTAINS_FOLDER]->(shared), "
        "(cli)-[:CONTAINS_FOLDER]->(own)",
        {"svc_root": svc_root, "cli_root": cli_root, "shared": shared, "own": own},
    )

    ing.ensure_constraints()

    left = ing.fetch_all("MATCH (n:Folder) RETURN n.absolute_path AS p ORDER BY p")
    assert left == [{"p": own}], left


@pytest.mark.parametrize(
    "new_root",
    [None, "/repos/svc"],
    ids=["both-rootless", "beside-an-enclosing-root"],
)
def test_a_node_shared_with_a_rootless_project_is_purged(
    memgraph_ingestor: MemgraphIngestor, new_root: str | None
) -> None:
    # A Project written before root_path existed records no tree, so a node it
    # shares is the old key's merge. collect() drops nulls, so a detector that
    # collected the roots saw none (or only the enclosing one), reported no
    # damage, and the purge never ran (review of PR 3216).
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (old:Project {name: 'old'}), "
        "(new:Project {name: 'new', root_path: $new_root}), "
        "(app:Folder {path: 'app', absolute_path: '/repos/svc/app'}), "
        "(old)-[:CONTAINS_FOLDER]->(app), "
        "(new)-[:CONTAINS_FOLDER]->(app)",
        {"new_root": new_root},
    )

    ing.ensure_constraints()

    assert ing.fetch_all("MATCH (n:Folder) RETURN count(n) AS n") == [{"n": 0}]


def test_a_rootless_owner_is_found_behind_its_package(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # A Package records its directory as its own absolute_path, so a detector
    # that read each immediate parent's tree from it saw a File two projects
    # reach through their Packages as inside both, never reached the rootless
    # Project behind one of them, and skipped the purge that counts that
    # owner as legacy (review of PR 3216).
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing._execute_query(
        "CREATE (old:Project {name: 'old'}), "
        "(new:Project {name: 'new', root_path: '/repos/svc'}), "
        "(po:Package {qualified_name: 'old.pkg', absolute_path: $pkg}), "
        "(pn:Package {qualified_name: 'new.pkg', absolute_path: $pkg}), "
        "(f:File {path: 'pkg/x.py', absolute_path: $file}), "
        "(old)-[:CONTAINS_PACKAGE]->(po), (new)-[:CONTAINS_PACKAGE]->(pn), "
        "(po)-[:CONTAINS_FILE]->(f), (pn)-[:CONTAINS_FILE]->(f)",
        {"pkg": "/repos/svc/pkg", "file": "/repos/svc/pkg/x.py"},
    )

    ing.ensure_constraints()

    assert ing.fetch_all("MATCH (n:File) RETURN count(n) AS n") == [{"n": 0}]
