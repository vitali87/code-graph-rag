# An incremental run re-parses every file holding a dependency edge into a
# re-indexed file and restores the remaining inbound edges verbatim. Both
# queries listed INHERITS but not IMPLEMENTS, so touching an interface file
# deleted its Interface node together with the IMPLEMENTS edges pointing at
# it, and the implementors, which hold no import edge into a same-package
# interface, were neither re-parsed nor restored: the edge a clean index
# emits was gone until the next full update (issue #1565).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

JAVA: dict[str, str] = {
    "src/Shape.java": "package app;\npublic interface Shape { int area(); }\n",
    "src/Square.java": (
        "package app;\npublic class Square implements Shape {\n"
        "  public int area() { return 4; }\n}\n"
    ),
}
CSHARP: dict[str, str] = {
    "Shape.cs": "namespace App { public interface IShape { int Area(); } }\n",
    "Square.cs": (
        "namespace App { public class Square : IShape { public int Area() => 4; } }\n"
    ),
}
PHP: dict[str, str] = {
    "Shape.php": "<?php\nnamespace App;\ninterface Shape { public function area(); }\n",
    "Square.php": (
        "<?php\nnamespace App;\n"
        "class Square implements Shape { public function area() { return 4; } }\n"
    ),
}

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
    )
    return nodes, edges


def _implements(snapshot: Snapshot) -> set[tuple[str, str]]:
    return {
        (e[1], e[4])
        for e in snapshot[1]
        if e[2] == cs.RelationshipType.IMPLEMENTS.value
    }


def _index(
    store: _StatefulIngestor, repo: Path, language: cs.SupportedLanguage, force: bool
) -> None:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _touch_after_cache(path: Path, root: Path) -> None:
    # The incremental pass skips a cached file whose mtime is not newer than
    # the hash cache's without hashing it; place the edit past the cache.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + "// touched\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


@pytest.mark.parametrize(
    ("language", "fixture", "interface_file"),
    [
        (cs.SupportedLanguage.JAVA, JAVA, "src/Shape.java"),
        (cs.SupportedLanguage.CSHARP, CSHARP, "Shape.cs"),
        (cs.SupportedLanguage.PHP, PHP, "Shape.php"),
    ],
    ids=["java", "csharp", "php"],
)
def test_incremental_reindex_of_an_interface_keeps_its_implementors(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    interface_file: str,
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in fixture.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    clean = _snapshot(store)
    assert len(_implements(clean)) == 1, "fixture must produce one IMPLEMENTS edge"

    _touch_after_cache(root / interface_file, root)
    _index(store, root, language, force=False)
    after = _snapshot(store)

    assert _implements(after) == _implements(clean)
    assert after == clean


def test_production_queries_walk_implements() -> None:
    # The shipped fix for #1565 is two relation lists in constants/graph.py.
    # The language tests below run against the eval emulator, which filters by
    # its own frozensets, so they stay green even if these queries are gutted.
    # This is the only assertion that fails if the production fix is reverted.
    assert "IMPLEMENTS" in cs.CYPHER_INBOUND_EDGES
    assert "IMPLEMENTS" in cs.CYPHER_AFFECTED_CALLER_PATHS


def test_emulator_affected_caller_rels_match_the_production_query() -> None:
    # Sibling guard to the inbound one: the affected-caller set drifts just as
    # silently, which is the same defect class one level over.
    import re

    from evals.cgr_graph import _AFFECTED_CALLER_RELS

    match = re.search(r"\[:([A-Z|_]+)\]", cs.CYPHER_AFFECTED_CALLER_PATHS)
    assert match is not None, "could not read the relation list from the query"
    production = set(match.group(1).split("|"))
    assert set(_AFFECTED_CALLER_RELS) == production


def test_emulator_inbound_rels_match_the_production_query() -> None:
    # The emulator restores inbound edges for the relations production
    # captures; a set that drifts from CYPHER_INBOUND_EDGES makes every
    # clean-vs-incremental eval silently disagree with production.
    import re

    from evals.cgr_graph import _INBOUND_DEPENDENT_RELS

    match = re.search(r"\[r:([A-Z|_]+)\]", cs.CYPHER_INBOUND_EDGES)
    assert match is not None, "could not read the relation list from the query"
    production = set(match.group(1).split("|"))
    assert set(_INBOUND_DEPENDENT_RELS) == production


def test_the_affected_caller_query_projects_the_key_its_reader_unpacks() -> None:
    # `_affected_caller_keys` reads each row as `row.get(cs.KEY_CALLER_PATH)`,
    # so the RETURN alias and that constant must agree. Interpolating the
    # constant asserts that AGREEMENT; a hardcoded `"AS caller_path"` would
    # instead pin a value, going red on a coordinated rename of both (a
    # working system: measured 2026-08-31, the literal variant fails while
    # this one passes) and staying green if KEY_CALLER_PATH later drifted
    # away from the literal. The emulator builds its rows in Python and never
    # executes this projection, so no behavioural test can see it.
    #
    # The source expression is pinned too, not just the alias: projecting
    # `target.path AS caller_path` keeps the alias correct while returning
    # re-indexed paths instead of dependent-caller paths, which empties the
    # affected-caller set. Measured 2026-08-31: that edit left this file and
    # test_graph_updater_incremental_rename.py green (9 passed) when the
    # assertion covered the alias alone.
    assert f"caller.path AS {cs.KEY_CALLER_PATH}" in cs.CYPHER_AFFECTED_CALLER_PATHS
