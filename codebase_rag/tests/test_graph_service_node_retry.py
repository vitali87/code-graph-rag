from _thread import LockType
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import BatchWrapper, PropertyValue


@pytest.fixture(params=[False, True], ids=["serial", "parallel"])
def graph_cursor(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[MemgraphIngestor, MagicMock], None, None]:
    cursor = MagicMock()

    def connect(self: MemgraphIngestor) -> MagicMock:
        connection = MagicMock()
        connection.cursor.return_value = cursor
        return connection

    monkeypatch.setattr(MemgraphIngestor, "_create_connection", connect)
    ingestor = MemgraphIngestor(
        host="localhost", port=7687, dialect=get_dialect(DIALECT_MEMGRAPH)
    )
    ingestor.conn = connect(ingestor)
    with ThreadPoolExecutor(max_workers=2) as executor:
        if request.param:
            ingestor._executor = executor
        yield ingestor, cursor


@pytest.mark.parametrize("use_merge", [False, True], ids=["create", "merge"])
def test_retry_preserves_failed_nodes_without_replaying_successful_labels(
    graph_cursor: tuple[MemgraphIngestor, MagicMock], use_merge: bool
) -> None:
    ingestor, cursor = graph_cursor
    ingestor._use_merge = use_merge
    failure = ConnectionError("transient node write failure")
    attempts: list[list[PropertyValue]] = []
    written: list[PropertyValue] = []

    def execute(query: str, params: BatchWrapper) -> None:
        ids = [row["id"] for row in params["batch"]]
        attempts.append(ids)
        if ids == ["project.a", "project.b"] and attempts.count(ids) == 1:
            raise failure
        written.extend(ids)

    cursor.execute.side_effect = execute
    first = ("Module", {"qualified_name": "project.a"})
    second = ("Module", {"qualified_name": "project.b"})
    ingestor.ensure_node_batch(*first)
    ingestor.ensure_node_batch("Project", {"name": "project"})
    ingestor.ensure_node_batch(*second)
    ingestor.ensure_node_batch("Function", {"qualified_name": "project.a.run"})

    with pytest.raises(ConnectionError) as raised:
        ingestor.flush_nodes()

    assert raised.value is failure
    assert sorted(written) == ["project", "project.a.run"]
    assert ingestor.node_buffer == [first, second]

    ingestor.flush_nodes()

    assert ingestor.node_buffer == []
    assert sorted(written) == ["project", "project.a", "project.a.run", "project.b"]
    assert attempts.count(["project"]) == 1
    assert attempts.count(["project.a.run"]) == 1
    assert attempts.count(["project.a", "project.b"]) == 2


def test_node_auto_flush_propagates_failure_and_leaves_nodes_for_retry(
    graph_cursor: tuple[MemgraphIngestor, MagicMock],
) -> None:
    ingestor, cursor = graph_cursor
    ingestor.batch_size = 1
    failure = ConnectionError("transient node write failure")
    cursor.execute.side_effect = [failure, None]

    with pytest.raises(ConnectionError) as raised:
        ingestor.ensure_node_batch("Module", {"qualified_name": "project.a"})

    assert raised.value is failure
    assert ingestor.node_buffer == [("Module", {"qualified_name": "project.a"})]

    ingestor.flush_nodes()

    assert ingestor.node_buffer == []
    assert cursor.execute.call_count == 2
    assert cursor.execute.call_args_list[0] == cursor.execute.call_args_list[1]


def test_relationship_auto_flush_retries_nodes_before_writing_edges(
    graph_cursor: tuple[MemgraphIngestor, MagicMock],
) -> None:
    ingestor, cursor = graph_cursor
    ingestor.batch_size = 3
    failure = ConnectionError("transient node write failure")
    cursor.execute.side_effect = failure
    ingestor.ensure_node_batch("Module", {"qualified_name": "project.a"})
    ingestor.ensure_node_batch("Module", {"qualified_name": "project.b"})
    source = ("Module", "qualified_name", "project.a")
    target = ("Module", "qualified_name", "project.b")
    for _ in range(2):
        ingestor.ensure_relationship_batch(source, "IMPORTS", target)

    with pytest.raises(ConnectionError) as raised:
        ingestor.ensure_relationship_batch(source, "IMPORTS", target)

    assert raised.value is failure
    assert cursor.execute.call_count == 1
    assert ingestor._rel_count == 3
    written: list[PropertyValue] = []
    relationship_counts: list[int] = []

    def execute(query: str, params: BatchWrapper) -> None:
        if "RETURN count(r)" in query:
            count = sum(
                row["from_val"] in written and row["to_val"] in written
                for row in params["batch"]
            )
            relationship_counts.append(count)
            cursor.fetchall.return_value = [(count,)]
        else:
            written.extend(row["id"] for row in params["batch"])

    cursor.description = [MagicMock()]
    cursor.description[0].name = "created"
    cursor.execute.side_effect = execute
    ingestor.flush_all()

    assert written == ["project.a", "project.b"]
    assert relationship_counts == [3]
    assert ingestor.node_buffer == []
    assert ingestor._rel_count == 0
    assert not ingestor._rel_groups


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_nodes_appended_during_flush_remain_buffered(
    graph_cursor: tuple[MemgraphIngestor, MagicMock], fail: bool
) -> None:
    ingestor, cursor = graph_cursor
    started = Event()
    release = Event()
    failed_node = ("Module", {"qualified_name": "project.a"})
    appended_node = ("Project", {"name": "later"})
    ingestor.ensure_node_batch("Project", {"name": "project"})
    ingestor.ensure_node_batch(*failed_node)

    def execute(query: str, params: BatchWrapper) -> None:
        if "Module" in query:
            started.set()
            assert release.wait(timeout=5)
            if fail:
                raise ConnectionError("transient node write failure")

    cursor.execute.side_effect = execute
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(ingestor.flush_nodes)
        try:
            assert started.wait(timeout=5)
            ingestor.ensure_node_batch(*appended_node)
        finally:
            release.set()
        if fail:
            with pytest.raises(ConnectionError, match="transient node write failure"):
                future.result(timeout=5)
        else:
            future.result(timeout=5)

    expected = [failed_node, appended_node] if fail else [appended_node]
    assert ingestor.node_buffer == expected


def _observe_lock_contention(ingestor: MemgraphIngestor, waiting: Event) -> None:
    def observe(lock: LockType) -> MagicMock:
        def enter() -> None:
            if not lock.acquire(blocking=False):
                waiting.set()
                assert lock.acquire(timeout=5)

        observed = MagicMock()
        observed.__enter__.side_effect = enter
        observed.__exit__.side_effect = lambda *_args: lock.release()
        return observed

    for slot in ingestor.__slots__:
        value = getattr(ingestor, slot)
        if isinstance(value, LockType):
            setattr(ingestor, slot, observe(value))


@pytest.mark.parametrize("auto_flush", [False, True], ids=["explicit", "auto"])
@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_overlapping_flushes_do_not_replay_nodes_or_lose_new_failures(
    graph_cursor: tuple[MemgraphIngestor, MagicMock], auto_flush: bool, fail: bool
) -> None:
    ingestor, cursor = graph_cursor
    ingestor._use_merge = False
    if auto_flush:
        ingestor.batch_size = 3
    started = Event()
    release = Event()
    second_ready = Event()
    _observe_lock_contention(ingestor, second_ready)
    pending = ("Function", {"qualified_name": "project.pending"})
    ingestor.ensure_node_batch("Module", {"qualified_name": "project.a"})
    ingestor.ensure_node_batch("Project", {"name": "project"})
    written: list[PropertyValue] = []

    def execute(query: str, params: BatchWrapper) -> None:
        ids = [row["id"] for row in params["batch"]]
        if ids == ["project.a"] and not started.is_set():
            started.set()
            assert release.wait(timeout=5)
        if ids == ["project.pending"] and fail:
            raise ConnectionError("transient pending node failure")
        written.extend(ids)

    def second_flush() -> None:
        try:
            ingestor.ensure_node_batch(*pending)
            if not auto_flush:
                ingestor.flush_nodes()
        finally:
            second_ready.set()

    cursor.execute.side_effect = execute
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(ingestor.flush_nodes)
        try:
            assert started.wait(timeout=5)
            second = callers.submit(second_flush)
            assert second_ready.wait(timeout=5)
        finally:
            release.set()
        first.result(timeout=5)
        if fail:
            with pytest.raises(ConnectionError, match="transient pending node failure"):
                second.result(timeout=5)
        else:
            second.result(timeout=5)

    if fail:
        assert ingestor.node_buffer == [pending]
        fail = False
        ingestor.flush_nodes()
    assert ingestor.node_buffer == []
    assert written.count("project") == 1
    assert written.count("project.a") == 1
    assert written.count("project.pending") == 1
