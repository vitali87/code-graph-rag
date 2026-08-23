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


# Task 13 added clean_database, list_projects, list_project_roots,
# delete_project, and export_graph_to_dict, so ArcadeDBIngestor now
# structurally satisfies GraphIngestor.
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
    # Issue #722: rows for the same endpoints may carry different sets of
    # distinguishing props -- not just different values for the same set.
    # One row here has both `via` and `kind`; the other has only `via`. If
    # the implementation grouped rows by candidate prop *values* (or ignored
    # presence and always used the full candidate tuple as the merge key),
    # this would still produce a single query and pass a weaker assertion --
    # so this test requires two calls with two distinct MERGE key shapes,
    # matching the asymmetric-props shape of the Memgraph integration
    # regression test (test_mixed_via_and_viales_edges_do_not_collapse in
    # tests/integration/test_cypher_queries.py).
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([{"created": 1}])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.ensure_relationship_batch(
            ("Function", "qualified_name", "a"),
            "FLOWS_TO",
            ("Function", "qualified_name", "b"),
            {"via": "arg", "kind": "direct"},
        )
        ingestor.ensure_relationship_batch(
            ("Function", "qualified_name", "a"),
            "FLOWS_TO",
            ("Function", "qualified_name", "b"),
            {"via": "ret"},
        )
        ingestor.flush_relationships()

        assert session.run.call_count == 2
        merged = [c[0][0].text for c in session.run.call_args_list]
        both_props = [q for q in merged if "kind: row.props.kind" in q]
        via_only = [q for q in merged if "kind: row.props.kind" not in q]
        assert len(both_props) == 1
        assert len(via_only) == 1
        assert (
            "MERGE (a)-[r:FLOWS_TO {via: row.props.via, kind: row.props.kind}]->(b)"
            in both_props[0]
        )
        assert "MERGE (a)-[r:FLOWS_TO {via: row.props.via}]->(b)" in via_only[0]
        ingestor.__exit__(None, None, None)


def test_flush_relationships_dedupes_identical_pattern_rows_within_one_batch() -> None:
    # Task 14 review finding: ArcadeDB has no unique index on relationships
    # (only vertex types get one), so its Cypher MERGE can't see an earlier
    # row's write from the *same* UNWIND-batched statement -- two rows that
    # MERGE onto the identical pattern created two edges instead of one.
    # _dedupe_rows_sharing_a_merge_pattern collapses such rows client-side
    # before the batch is sent. This pins two things a query-text-only
    # assertion can't: (1) only ONE row reaches session.run's `batch` kwarg
    # for the pair below, not two, and (2) the surviving row's props are a
    # per-key OVERLAY of both rows (later value wins per key, earlier keys
    # not present in the later row survive) -- not a blunt "keep the last
    # row entirely", which would silently drop `keep_me`.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([{"created": 1}])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        # CALLS has no MERGE_KEY_PROPS_BY_REL entry, so merge_key_props is
        # () and both rows share the same (from_val, to_val, ()) dedup key
        # -- the exact "no distinguishing props" shape real call-graph edges
        # have.
        ingestor.ensure_relationship_batch(
            ("Function", "qualified_name", "a"),
            "CALLS",
            ("Function", "qualified_name", "b"),
            {"keep_me": "first", "line_number": 1},
        )
        ingestor.ensure_relationship_batch(
            ("Function", "qualified_name", "a"),
            "CALLS",
            ("Function", "qualified_name", "b"),
            {"line_number": 2},
        )
        ingestor.flush_relationships()

        assert session.run.call_count == 1
        batch = session.run.call_args.kwargs["batch"]
        assert batch == [
            {
                "from_val": "a",
                "to_val": "b",
                "props": {"keep_me": "first", "line_number": 2},
            }
        ]
        ingestor.__exit__(None, None, None)


def test_flush_relationships_chunks_a_hot_target_across_multiple_merge_calls() -> None:
    # The deterministic regression guard for _chunk_endpoint_disjoint: if
    # it were ever reverted to `return [rows]` or bypassed, this test
    # fails immediately, without needing a live server to reproduce the
    # timing-dependent engine race it exists for (an ArcadeDB UNWIND batch
    # where 2+ rows share an endpoint can deadlock or silently drop a row
    # -- see arcadedb.py's docstring on _chunk_endpoint_disjoint, and
    # test_parallel_flush_into_one_hot_target in
    # tests/integration/test_graph_backend_conformance.py for the
    # real-concurrency side of this coverage). Three distinct rows here --
    # different from_val each, so _dedupe_rows_sharing_a_merge_pattern
    # does not collapse them -- all target the same vertex, so no two of
    # them can ever share a chunk: three separate session.run calls, each
    # carrying exactly one row, not one call carrying all three.
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([{"created": 1}])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        for src in ("a", "b", "c"):
            ingestor.ensure_relationship_batch(
                ("Function", "qualified_name", src),
                "CALLS",
                ("Function", "qualified_name", "hot"),
            )
        ingestor.flush_relationships()

        assert session.run.call_count == 3
        sent_batches = [c.kwargs["batch"] for c in session.run.call_args_list]
        assert all(len(batch) == 1 for batch in sent_batches)
        sent_pairs = {
            (batch[0]["from_val"], batch[0]["to_val"]) for batch in sent_batches
        }
        assert sent_pairs == {("a", "hot"), ("b", "hot"), ("c", "hot")}
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


def test_admin_operations_use_the_shared_cypher() -> None:
    from codebase_rag.cypher_queries import CYPHER_DELETE_ALL, CYPHER_LIST_PROJECTS

    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.clean_database()
        # session.run's first positional arg is a neo4j.Query object (needed
        # to carry the timeout, see test_fetch_all_carries_the_timeout_on_the_
        # query_object), so the query text is read off its .text attribute.
        assert session.run.call_args[0][0].text == CYPHER_DELETE_ALL

        session.run.return_value = iter([])
        ingestor.list_projects()
        assert CYPHER_LIST_PROJECTS in session.run.call_args[0][0].text
        ingestor.__exit__(None, None, None)


def test_delete_project_also_prunes_shared_nodes() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.return_value = iter([])
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        ingestor.delete_project("alpha")

        # delete_project issues three writes (CYPHER_DELETE_PROJECT, the
        # prune pass, CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES); pin the count
        # so a dropped call isn't masked by the two `any()` checks below
        # still finding matches among the remaining calls.
        assert session.run.call_count == 3
        sent = [c[0][0].text for c in session.run.call_args_list]
        assert any("Resource" in q for q in sent)
        assert any("ExternalModule" in q for q in sent)
        ingestor.__exit__(None, None, None)


def test_export_graph_to_dict_reports_counts() -> None:
    with patch("codebase_rag.services.graph.arcadedb.GraphDatabase") as gdb:
        session = MagicMock()
        session.run.side_effect = [
            iter([{"node_id": 1, "labels": ["Function"], "properties": {}}]),
            iter([]),
        ]
        gdb.driver.return_value.session.return_value.__enter__.return_value = session

        ingestor = _ingestor()
        ingestor.__enter__()
        data = ingestor.export_graph_to_dict()
        assert data["metadata"]["total_nodes"] == 1
        assert data["metadata"]["total_relationships"] == 0
        ingestor.__exit__(None, None, None)
