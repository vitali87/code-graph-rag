"""Issue #2920: a cancelled MCP call holds the lock until its thread ends.

Cancelling a request cancels its handler, and a bare `asyncio.to_thread`
unwound at once: the `async with self._ingestor_lock` released while the
thread kept writing, so a `delete_project` sent right after a cancelled
`index_repository` ran underneath it, and the index wrote 13,404 nodes back
into a deleted project.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry

THREAD_WAIT_S = 5.0


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
    )


class _Gate:
    """A thread body that runs until the test lets it finish."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self) -> str:
        self.started.set()
        self.release.wait(THREAD_WAIT_S)
        self.finished.set()
        return "indexed"


async def _cancel_once_running(task: asyncio.Task[str], gate: _Gate) -> None:
    await asyncio.to_thread(gate.started.wait, THREAD_WAIT_S)
    task.cancel()
    # Let the cancellation unwind as far as it will.
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_cancelled_index_keeps_the_lock_until_its_thread_ends(
    registry: MCPToolsRegistry,
) -> None:
    gate = _Gate()
    with patch.object(registry, "_index_repository_sync", gate):
        task = asyncio.ensure_future(registry.index_repository())
        await _cancel_once_running(task, gate)

        assert registry._ingestor_lock.locked()
        assert not task.done()

        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert gate.finished.is_set()
    assert not registry._ingestor_lock.locked()


async def test_a_delete_after_a_cancelled_index_waits_for_it(
    registry: MCPToolsRegistry,
) -> None:
    gate = _Gate()
    order: list[str] = []

    def delete(project_name: str) -> dict[str, object]:
        order.append(f"delete after index finished: {gate.finished.is_set()}")
        return {"success": True}

    with (
        patch.object(registry, "_index_repository_sync", gate),
        patch.object(registry, "_delete_project_sync", delete),
    ):
        index = asyncio.ensure_future(registry.index_repository())
        await _cancel_once_running(index, gate)
        delete_call = asyncio.ensure_future(registry.delete_project("proj"))
        for _ in range(5):
            await asyncio.sleep(0)
        gate.release.set()
        await delete_call
        with pytest.raises(asyncio.CancelledError):
            await index

    assert order == ["delete after index finished: True"]


async def test_a_cancelled_index_whose_thread_fails_is_still_cancelled(
    registry: MCPToolsRegistry,
) -> None:
    gate = _Gate()

    def failing() -> str:
        gate()
        raise RuntimeError("store down")

    with patch.object(registry, "_index_repository_sync", failing):
        task = asyncio.ensure_future(registry.index_repository())
        await _cancel_once_running(task, gate)
        assert registry._ingestor_lock.locked()

        gate.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not registry._ingestor_lock.locked()


# Negative: what must not change.


async def test_an_uncancelled_index_returns_its_result(
    registry: MCPToolsRegistry,
) -> None:
    gate = _Gate()
    gate.release.set()
    with patch.object(registry, "_index_repository_sync", gate):
        assert await registry.index_repository() == "indexed"
    assert not registry._ingestor_lock.locked()


async def test_a_failing_index_still_reports_its_error(
    registry: MCPToolsRegistry,
) -> None:
    def boom() -> str:
        raise RuntimeError("store down")

    with patch.object(registry, "_index_repository_sync", boom):
        assert "store down" in await registry.index_repository()
    assert not registry._ingestor_lock.locked()
