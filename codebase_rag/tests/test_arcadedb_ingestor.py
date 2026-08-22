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


# ArcadeDBIngestor does not yet implement flush_nodes, flush_relationships,
# clean_database, list_projects, list_project_roots, delete_project, or
# export_graph_to_dict (batching lands in Task 12, admin ops in Task 13), so
# it does not yet structurally satisfy GraphIngestor. strict=True: if this
# starts passing before Task 13 removes the marker, the run must fail so the
# stale xfail cannot silently stop asserting anything.
@pytest.mark.xfail(strict=True, reason="GraphIngestor protocol is completed in Task 13")
def test_satisfies_the_graph_ingestor_protocol() -> None:
    assert isinstance(_ingestor(), GraphIngestor)


@pytest.mark.parametrize(
    "username,password",
    [
        pytest.param("", "", id="both-empty"),
        pytest.param(" ", "pw", id="whitespace-only-username"),
        pytest.param("root", " ", id="whitespace-only-password"),
        pytest.param(" ", " ", id="whitespace-only-both"),
    ],
)
def test_requires_credentials(username: str, password: str) -> None:
    # ArcadeDB's Bolt listener rejects the `none` auth scheme, and a
    # whitespace-only credential is never valid either -- it must fail here,
    # at construction, not later when the driver rejects a blank credential.
    with pytest.raises(ValueError, match="credentials"):
        _ingestor(username=username, password=password)


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


def test_execute_write_runs_the_query_and_returns_none() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        result = MagicMock()
        result.__iter__.return_value = iter([])
        session = MagicMock()
        session.run.return_value = result
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        assert ingestor.execute_write("CREATE (n:Foo {qn: $qn})", {"qn": "a.b"}) is None
        session.run.assert_called_once()
        assert session.run.call_args.kwargs["qn"] == "a.b"
        ingestor.__exit__(None, None, None)


def test_node_buffer_flushes_at_batch_size() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase"):
        ingestor = _ingestor(batch_size=2)
        ingestor.__enter__()
        # ArcadeDBIngestor is __slots__-only (no instance __dict__), so the
        # mock must patch the class rather than the instance attribute; see
        # the identical constraint noted in
        # test_ensure_constraints_runs_the_schema_ddl above.
        with patch.object(ArcadeDBIngestor, "flush_nodes") as flush:
            ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
            flush.assert_not_called()
            ingestor.ensure_node_batch("Function", {"qualified_name": "b"})
            flush.assert_called_once()
        ingestor.__exit__(None, None, None)


def test_flush_nodes_sends_an_unwound_merge() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a", "name": "a"})
        ingestor.flush_nodes()

        # session.run's first positional arg is a neo4j.Query object (needed
        # to carry the timeout, see test_fetch_all_carries_the_timeout_on_the_
        # query_object) rather than a bare string, so the query text is read
        # off its .text attribute.
        query = session.run.call_args[0][0].text
        assert query.startswith("UNWIND $batch AS row")
        assert "MERGE (n:Function {qualified_name: row.id})" in query
        assert session.run.call_args.kwargs["batch"] == [
            {"id": "a", "props": {"name": "a"}}
        ]
        ingestor.__exit__(None, None, None)


def test_flush_relationships_splits_by_merge_key_signature() -> None:
    # Issue #722: rows carrying different distinguishing props must not
    # share a MERGE key, or parallel FLOWS_TO edges collapse into one.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([{"created": 1}])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        for via in ("arg", "ret"):
            ingestor.ensure_relationship_batch(
                ("Function", "qualified_name", "a"),
                "FLOWS_TO",
                ("Function", "qualified_name", "b"),
                {"via": via, "kind": "direct"},
            )
        ingestor.flush_relationships()

        merged = [c[0][0].text for c in session.run.call_args_list]
        assert any("via: row.props.via" in q for q in merged)
        ingestor.__exit__(None, None, None)


def test_flush_retries_a_concurrent_modification_error() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = [
            RuntimeError("Concurrent modification of record #1:0"),
            iter([]),
        ]
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        ingestor.flush_nodes()  # must not raise

        assert session.run.call_count == 2
        ingestor.__exit__(None, None, None)


def test_flush_reraises_a_permanent_error() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = RuntimeError("Syntax error at line 1")
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        with pytest.raises(RuntimeError, match="Syntax error"):
            ingestor.flush_nodes()
        ingestor.__exit__(None, None, None)


def test_exit_flushes_buffered_nodes_on_the_happy_path() -> None:
    # No exception: __exit__ still owes a final flush_all(), or nodes
    # buffered right up to the end of a successful run are dropped silently.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        ingestor.__exit__(None, None, None)

        session.run.assert_called_once()


def test_exit_best_effort_flushes_buffered_nodes_on_exception() -> None:
    # An exception mid-run must not silently drop buffered nodes: __exit__
    # makes a best-effort flush_all() call, and the original exception (not
    # a secondary flush failure) is what the caller sees.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        try:
            ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
            raise ValueError("boom mid-ingest")
        except ValueError:
            ingestor.__exit__(ValueError, ValueError("boom mid-ingest"), None)

        session.run.assert_called_once()


def test_exit_swallows_a_secondary_flush_failure_during_exception_handling() -> None:
    # A flush_all() failure while already unwinding a real exception must
    # not mask that original exception -- it is merely logged.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = RuntimeError("Syntax error at line 1")
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_node_batch("Function", {"qualified_name": "a"})
        # __exit__ itself must not raise, despite the secondary flush error.
        ingestor.__exit__(ValueError, ValueError("original error"), None)

        # The flush must actually have been attempted (and failed) -- a
        # no-op __exit__ would also satisfy "did not raise" without this.
        session.run.assert_called_once()


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
