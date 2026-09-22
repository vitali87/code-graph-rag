# The stateful graph double keeps one record per relationship SITE the way
# the real store does (issue #1522): parallel edges are told apart by
# MERGE_KEY_PROPS_BY_REL, a re-emission of the same site updates it in
# place (`MERGE ... SET r += props`), and the inbound-edge capture the
# incremental restore reads returns every site, not the last one.

from __future__ import annotations

from codebase_rag import constants as cs
from evals.cgr_graph import _StatefulIngestor

_FN = cs.NodeLabel.FUNCTION.value
_MOD = cs.NodeLabel.MODULE.value
_CALLS = cs.RelationshipType.CALLS.value
_IMPORTS = cs.RelationshipType.IMPORTS.value


def _store() -> _StatefulIngestor:
    store = _StatefulIngestor()
    store.ensure_node_batch(
        _FN, {cs.KEY_QUALIFIED_NAME: "p.a.run", cs.KEY_PATH: "a.py"}
    )
    store.ensure_node_batch(
        _FN, {cs.KEY_QUALIFIED_NAME: "p.util.helper", cs.KEY_PATH: "util.py"}
    )
    store.ensure_node_batch(_MOD, {cs.KEY_QUALIFIED_NAME: "p.a", cs.KEY_PATH: "a.py"})
    store.ensure_node_batch(
        _MOD, {cs.KEY_QUALIFIED_NAME: "p.util", cs.KEY_PATH: "util.py"}
    )
    return store


def _call(store: _StatefulIngestor, props: dict[str, object] | None) -> None:
    store.ensure_relationship_batch(
        (_FN, cs.KEY_QUALIFIED_NAME, "p.a.run"),
        _CALLS,
        (_FN, cs.KEY_QUALIFIED_NAME, "p.util.helper"),
        props,  # type: ignore[arg-type]
    )


def _import(store: _StatefulIngestor, props: dict[str, object]) -> None:
    store.ensure_relationship_batch(
        (_MOD, cs.KEY_QUALIFIED_NAME, "p.a"),
        _IMPORTS,
        (_MOD, cs.KEY_QUALIFIED_NAME, "p.util"),
        props,  # type: ignore[arg-type]
    )


def test_inbound_capture_returns_one_row_per_site() -> None:
    store = _store()
    _call(store, {cs.KEY_LINE: 3, cs.KEY_COL: 4})
    _call(store, {cs.KEY_LINE: 7, cs.KEY_COL: 8})

    rows = store.fetch_all(
        cs.CYPHER_INBOUND_EDGES, {cs.CYPHER_PARAM_PATHS: ["util.py"]}
    )

    sites = sorted(
        (row[cs.KEY_PROPS][cs.KEY_LINE], row[cs.KEY_PROPS][cs.KEY_COL])  # type: ignore[index]
        for row in rows
        if row[cs.KEY_REL] == _CALLS
    )
    assert sites == [(3, 4), (7, 8)]


def test_reemitting_a_site_updates_it_by_its_merge_key() -> None:
    store = _store()
    site = {cs.KEY_LINE: 1, cs.KEY_COL: 0, cs.KEY_ALIAS: "helper"}
    _import(store, {**site, cs.KEY_IMPORTED_NAME: "helper"})
    _import(store, {**site, cs.KEY_IMPORTED_NAME: "helper", cs.KEY_END_COL: 30})
    # A different alias on the same statement is its own edge.
    _import(store, {**site, cs.KEY_ALIAS: "other", cs.KEY_IMPORTED_NAME: "other"})

    edge = (_MOD, "p.a", _IMPORTS, _MOD, "p.util")
    assert store.sites_of(edge) == [
        {**site, cs.KEY_IMPORTED_NAME: "helper", cs.KEY_END_COL: 30},
        {**site, cs.KEY_ALIAS: "other", cs.KEY_IMPORTED_NAME: "other"},
    ]


def test_a_siteless_reemission_merges_into_the_existing_edge() -> None:
    # `MERGE (a)-[r:CALLS]->(b)` with no key props matches the edge already
    # there, so the store holds one relationship, not a second bare one.
    store = _store()
    _call(store, {cs.KEY_LINE: 3, cs.KEY_COL: 4})
    _call(store, None)

    edge = (_FN, "p.a.run", _CALLS, _FN, "p.util.helper")
    assert store.sites_of(edge) == [{cs.KEY_LINE: 3, cs.KEY_COL: 4}]
