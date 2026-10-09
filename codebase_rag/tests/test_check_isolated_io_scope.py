"""`check --isolated` refuses only over IO links the checked project anchors.

The guard asked whether the whole database held one FLOWS_TO or
RESOLVES_TO edge, so on a shared graph one service indexed with
`--capture io` disabled `--isolated` for every other project, though an
isolated run can only lose a link on a Resource its own nodes touch: the
resource prune keeps every Resource another node still anchors (#3188).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryingIngestorProtocol
from codebase_rag.structural_check import CheckError, run_check
from codebase_rag.tests.test_check_isolated_wiring import (
    PROJECT,
    _edit,
    _state,
    indexed,  # noqa: F401  (fixture)
)
from evals.cgr_graph import _StatefulIngestor

_RESOURCE = cs.NodeLabel.RESOURCE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
URL = "resource::NETWORK::/users/1"
ROUTE = "resource::ENDPOINT::GET /users/:id"
LOAD = "isosvc.server.load"
GET_USER = "isosvc.server.getUser"


def _link_other_service(store: _StatefulIngestor) -> None:
    # Another project's cross-service link, as `--capture io` writes it:
    # load() fetches /users/1, which resolves to the route getUser exposes.
    for qn in (LOAD, GET_USER):
        store.ensure_node_batch(
            _FUNCTION, {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_NAME: qn.rsplit(".", 1)[-1]}
        )
    for qn, kind in ((URL, "NETWORK"), (ROUTE, "ENDPOINT")):
        store.ensure_node_batch(
            _RESOURCE, {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_NAME: qn, "kind": kind}
        )
    for frm, rel, to in (
        ((_FUNCTION, LOAD), cs.RelationshipType.READS_FROM, (_RESOURCE, URL)),
        ((_RESOURCE, URL), cs.RelationshipType.RESOLVES_TO, (_RESOURCE, ROUTE)),
        ((_FUNCTION, GET_USER), cs.RelationshipType.EXPOSES, (_RESOURCE, ROUTE)),
    ):
        store.ensure_relationship_batch(
            (frm[0], cs.KEY_QUALIFIED_NAME, frm[1]),
            rel,
            (to[0], cs.KEY_QUALIFIED_NAME, to[1]),
        )


def _isolated(root: Path, store: _StatefulIngestor) -> dict:
    parsers, queries = load_parsers()
    return dict(
        run_check(
            root,
            "HEAD",
            PROJECT,
            # The eval emulator answers every query the check issues.
            cast(QueryingIngestorProtocol, store),
            parsers,
            queries,
            isolated=True,
            capture=resolve_capture([]),
        )
    )


def _io_edges(store: _StatefulIngestor) -> set[tuple]:
    io = {cs.RelationshipType.RESOLVES_TO.value, cs.RelationshipType.EXPOSES.value}
    return {edge for edge in store.edges if edge[2] in io}


def test_another_projects_io_links_do_not_refuse_the_check(
    indexed: tuple[Path, _StatefulIngestor],  # noqa: F811
) -> None:
    root, store = indexed
    _link_other_service(store)
    _edit(root)
    before = _state(store)

    delta = _isolated(root, store)

    assert delta["dangling_callers"][0]["target"] == f"{PROJECT}.pkg.util.helper"
    # The other service's links, and the whole graph, are as they were.
    assert len(_io_edges(store)) == 2
    assert _state(store) == before


def test_a_link_on_a_resource_the_project_touches_still_refuses(
    indexed: tuple[Path, _StatefulIngestor],  # noqa: F811
) -> None:
    # Negative: once the checked project's own function reads the URL that
    # resolves to the route, cutting it could unanchor that link.
    root, store = indexed
    _link_other_service(store)
    store.ensure_relationship_batch(
        (_FUNCTION, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.pkg.app.run"),
        cs.RelationshipType.READS_FROM,
        (_RESOURCE, cs.KEY_QUALIFIED_NAME, URL),
    )
    _edit(root)
    before = _state(store)

    with pytest.raises(CheckError, match=cs.RelationshipType.RESOLVES_TO.value):
        _isolated(root, store)
    assert _state(store) == before


def test_a_route_the_project_exposes_still_refuses(
    indexed: tuple[Path, _StatefulIngestor],  # noqa: F811
) -> None:
    # Negative: the endpoint end of a RESOLVES_TO counts as well.
    root, store = indexed
    _link_other_service(store)
    store.ensure_relationship_batch(
        (_FUNCTION, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.pkg.util.helper"),
        cs.RelationshipType.EXPOSES,
        (_RESOURCE, cs.KEY_QUALIFIED_NAME, ROUTE),
    )
    _edit(root)

    with pytest.raises(CheckError, match=cs.RelationshipType.RESOLVES_TO.value):
        _isolated(root, store)
