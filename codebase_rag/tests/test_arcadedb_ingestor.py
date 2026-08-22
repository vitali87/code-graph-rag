from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.arcadedb import ArcadeDBDialect, ArcadeDBIngestor


def _ingestor(**kw: Any) -> ArcadeDBIngestor:
    defaults = dict(
        host="db",
        bolt_port=7687,
        http_port=2480,
        database="cg",
        username="root",
        password="pw",
    )
    return ArcadeDBIngestor(**{**defaults, **kw})


def test_satisfies_the_graph_ingestor_protocol() -> None:
    assert isinstance(_ingestor(), GraphIngestor)


def test_requires_credentials() -> None:
    # ArcadeDB's Bolt listener rejects the `none` auth scheme.
    with pytest.raises(ValueError, match="credentials"):
        _ingestor(username="", password="")


def test_rejects_batch_size_below_one() -> None:
    with pytest.raises(ValueError):
        _ingestor(batch_size=0)


def test_enter_opens_a_driver_with_basic_auth() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        ingestor = _ingestor()
        ingestor.__enter__()
        gdb.driver.assert_called_once_with("bolt://db:7687", auth=("root", "pw"))
        ingestor.__exit__(None, None, None)


def test_exit_closes_the_driver() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        driver = MagicMock()
        gdb.driver.return_value = driver
        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.__exit__(None, None, None)
        driver.close.assert_called_once()


def test_fetch_all_maps_records_to_dicts() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([{"a": 1, "b": 2}])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        assert ingestor.fetch_all("MATCH (n) RETURN n.a AS a, n.b AS b") == [
            {"a": 1, "b": 2}
        ]
        ingestor.__exit__(None, None, None)


def test_fetch_all_does_not_append_a_memory_limit() -> None:
    # The dialect's apply_query_limit is identity here; appending Memgraph's
    # suffix would be a parse error on ArcadeDB.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n) RETURN n")
        sent = session.run.call_args[0][0]
        assert "QUERY MEMORY LIMIT" not in str(sent)
        ingestor.__exit__(None, None, None)


def test_fetch_all_carries_the_timeout_on_the_query_object() -> None:
    # Session.run(query, parameters=None, **kwargs) means a bare timeout=
    # kwarg becomes a Cypher parameter and bounds nothing. This timeout is
    # the only guard left after QUERY MEMORY LIMIT, so pin the vehicle.
    from neo4j import Query

    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n) RETURN n")
        sent = session.run.call_args[0][0]
        assert isinstance(sent, Query)
        assert sent.timeout is not None
        assert "timeout" not in session.run.call_args.kwargs
        ingestor.__exit__(None, None, None)


def test_fetch_all_passes_parameters_through() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.fetch_all("MATCH (n {qn: $qn}) RETURN n", {"qn": "a.b"})
        assert session.run.call_args.kwargs["qn"] == "a.b"
        ingestor.__exit__(None, None, None)


def test_ensure_constraints_runs_the_schema_ddl() -> None:
    # ArcadeDBDialect is __slots__-only (no instance __dict__), so the mock
    # must patch the class rather than `ingestor._dialect` directly; a
    # MagicMock class attribute isn't a descriptor, so it is not bound and
    # the call signature is unaffected.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase"):
        ingestor = _ingestor()
        ingestor.__enter__()
        with patch.object(ArcadeDBDialect, "ensure_schema") as ensure:
            ingestor.ensure_constraints()
            ensure.assert_called_once_with(ingestor)
        ingestor.__exit__(None, None, None)
