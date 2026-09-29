# Real-Memgraph check of `CYPHER_UNRESOLVED_IMPORTER_PATHS` (issue #1682).
# Memgraph binds `STARTS WITH` tighter than `+`, so the unparenthesised
# `STARTS WITH name + '.'` compared first and then added '.' to a boolean:
# every call raised, the updater's `except` returned no importers, and a file
# created after its importer never got that importer re-parsed. The unit
# tests drive the eval double, which mirrors the query in Python; only a real
# database proves the Cypher itself parses and filters.
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def test_the_query_returns_importers_of_a_module_or_its_members(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    memgraph_ingestor.execute_write(
        "CREATE (:Module {qualified_name: 'proj.a', path: 'a.py'})"
        "-[:IMPORTS]->(:Module {qualified_name: 'lib.helpers'}), "
        "(:Module {qualified_name: 'proj.b', path: 'b.py'})"
        "-[:IMPORTS]->(:Module {qualified_name: 'lib.helpers.tool'}), "
        "(:Module {qualified_name: 'proj.c', path: 'c.py'})"
        "-[:IMPORTS]->(:Module {qualified_name: 'lib.helpersx'}), "
        "(:Module {qualified_name: 'proj.d', path: 'd.py'})"
        "-[:IMPORTS]->(:Module {qualified_name: 'proj.local'})"
    )

    rows = memgraph_ingestor.fetch_all(
        cs.CYPHER_UNRESOLVED_IMPORTER_PATHS,
        {
            cs.CYPHER_PARAM_MODULE_NAMES: ["lib.helpers"],
            cs.KEY_PROJECT_PREFIX: "proj.",
        },
    )

    # The module itself and a member under it match; a name that merely
    # shares the prefix without a dot boundary does not, and a target inside
    # the project is resolved rather than dangling.
    assert sorted(r[cs.KEY_CALLER_PATH] for r in rows) == ["a.py", "b.py"]
