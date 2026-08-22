"""Unit tests for the pure classification logic in conftest.py.

Not marked `integration` on purpose: `_backend_from_callspec_params` is a
plain function over a dict, needs no Docker container, and should run in
the fast suite so a regression here is caught without paying for
integration setup.
"""

from __future__ import annotations

from codebase_rag.tests.integration.conftest import (
    BACKENDS,
    _backend_from_callspec_params,
)


def test_reads_backend_from_graph_container_param() -> None:
    assert _backend_from_callspec_params({"graph_container": "arcadedb"}) == "arcadedb"


def test_falls_back_to_default_backend_when_unparametrised() -> None:
    assert _backend_from_callspec_params({}) == str(BACKENDS[0])


def test_not_confused_by_an_unrelated_axis_named_like_a_backend() -> None:
    # Regression: an unrelated parametrize axis whose value happens to be
    # the string "memgraph" must not be mistaken for the actual backend,
    # which the earlier substring-matching-on-test-id approach did.
    params = {"graph_container": "arcadedb", "other_axis": "memgraph"}
    assert _backend_from_callspec_params(params) == "arcadedb"
