# The stateful eval store keyed a relationship on its endpoints alone, so a
# caller that reached the same callee twice held two edges in the production
# store and one here -- the second site overwrote the first, and every graph
# read the double answers lost it (issue #1911). `MERGE_KEY_PROPS_BY_REL` is
# the production key table; this store now uses it.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def _index(store: _StatefulIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)


def _store_for(tmp_path: Path, source: str) -> _StatefulIngestor:
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "mod.py").write_text(source, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, repo)
    return store


def _calls_between(store: _StatefulIngestor, caller: str, callee: str) -> list[tuple]:
    return [
        edge
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).endswith(caller)
        and str(edge[4]).endswith(callee)
    ]


def test_a_nested_call_to_the_same_callee_keeps_both_sites(tmp_path: Path) -> None:
    store = _store_for(
        tmp_path,
        "def helper(x):\n    return x\n\n\ndef run():\n    return helper(helper(1))\n",
    )

    sites = _calls_between(store, ".run", ".helper")

    assert len(sites) == 2, f"expected both call sites, got {sites}"
    # Two DISTINCT sites, not the same one counted twice: the key carries
    # (line, col), which is what makes them separate edges.
    assert len({site for *_endpoints, site in sites}) == 2


def test_the_endpoint_view_still_reports_one_edge_per_pair(tmp_path: Path) -> None:
    # The accept control. `edges` is what most callers read, and it must keep
    # answering "is there an edge between these two" with one entry -- the
    # multiplicity belongs to `keyed_edges`, not to it.
    store = _store_for(
        tmp_path,
        "def helper(x):\n    return x\n\n\ndef run():\n    return helper(helper(1))\n",
    )

    pairs = [
        edge
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CALLS.value
        and str(edge[1]).endswith(".run")
        and str(edge[4]).endswith(".helper")
    ]

    assert len(pairs) == 1
    assert len(pairs[0]) == 5


def test_a_single_call_is_still_a_single_site(tmp_path: Path) -> None:
    store = _store_for(
        tmp_path,
        "def helper(x):\n    return x\n\n\ndef run():\n    return helper(1)\n",
    )

    assert len(_calls_between(store, ".run", ".helper")) == 1


def test_two_bound_names_from_one_import_statement_are_two_edges(
    tmp_path: Path,
) -> None:
    # IMPORTS keys on (line, col, alias): `from x import a, b` shares a
    # statement span but binds two names, so it is two edges in production.
    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "helpers.py").write_text("def a():\n    pass\n\n\ndef b():\n    pass\n")
    (repo / "mod.py").write_text("from helpers import a, b\n", encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, repo)

    imports = [
        edge
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.IMPORTS.value
        and str(edge[1]).endswith(".mod")
    ]

    assert len(imports) >= 2, f"expected one edge per bound name, got {imports}"
    assert len({site for *_endpoints, site in imports}) == len(imports)


def test_a_structural_edge_keeps_the_endpoint_only_key(tmp_path: Path) -> None:
    # DEFINES is not in MERGE_KEY_PROPS_BY_REL, so its site is empty and its
    # keyed form carries nothing extra. Without this, "key on the site" could
    # be implemented by keying everything on every property it happens to
    # carry, which would split edges the production store merges.
    store = _store_for(tmp_path, "def helper(x):\n    return x\n")

    defines = [
        edge
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.DEFINES.value
    ]

    assert defines, "no DEFINES edge was emitted at all"
    assert all(edge[5] == () for edge in defines)


def test_a_partial_site_cannot_collide_with_a_different_partial_site() -> None:
    # Driven at the store, not through a parser: production drops a key prop
    # that is absent from a batch's row, so two rows can carry DIFFERENT
    # subsets of (line, col). Keying on the values alone would make
    # `{line: 3}` and `{col: 3}` the same edge; keying on (name, value) pairs
    # keeps them apart.
    store = _StatefulIngestor()
    caller = (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "proj.mod.run")
    callee = (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "proj.mod.helper")

    store.ensure_relationship_batch(
        caller, cs.RelationshipType.CALLS.value, callee, {cs.KEY_LINE: 3}
    )
    store.ensure_relationship_batch(
        caller, cs.RelationshipType.CALLS.value, callee, {cs.KEY_COL: 3}
    )

    assert len(store.keyed_edges) == 2
    assert len(store.edges) == 1


def test_a_row_with_no_site_merges_on_its_endpoints() -> None:
    # `MERGE_KEY_PROPS_BY_REL`'s stated rule: props absent from a batch's rows
    # are dropped from the key at flush time, so a CALLS row with no site
    # still merges on endpoints alone. Two such rows are one edge.
    store = _StatefulIngestor()
    caller = (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "proj.mod.run")
    callee = (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "proj.mod.helper")

    store.ensure_relationship_batch(caller, cs.RelationshipType.CALLS.value, callee)
    store.ensure_relationship_batch(caller, cs.RelationshipType.CALLS.value, callee)

    assert len(store.keyed_edges) == 1
