"""File tools do their disk I/O on a thread, never on the event loop.

FileReader and FileWriter were `async` but read and wrote directly, so a large
file stalled every other request the MCP server was serving; FileEditor and
the graph handlers already offloaded theirs.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import file_writer
from codebase_rag.tools.file_reader import FileReader
from codebase_rag.tools.file_writer import FileWriter


async def test_a_read_runs_off_the_event_loop(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        seen.append(threading.current_thread())
        return real(self, *args, **kwargs)

    with patch.object(Path, "read_text", spy):
        result = await FileReader(str(tmp_path)).read_file("a.py")
    assert result.content == "x = 1\n"
    assert seen and loop_thread not in seen


async def test_a_write_runs_off_the_event_loop(tmp_path: Path) -> None:
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real = file_writer.write_text_atomic

    def spy(path: Path, text: str) -> None:
        seen.append(threading.current_thread())
        real(path, text)

    with patch.object(file_writer, "write_text_atomic", spy):
        result = await FileWriter(str(tmp_path)).create_file("new/b.py", "y = 2\n")
    assert result.success
    assert (tmp_path / "new" / "b.py").read_text(encoding="utf-8") == "y = 2\n"
    assert seen and loop_thread not in seen


async def test_a_directory_listing_runs_off_the_event_loop(tmp_path: Path) -> None:
    registry = MCPToolsRegistry(
        project_root=str(tmp_path), ingestor=MagicMock(), cypher_gen=MagicMock()
    )
    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []

    def lister(directory_path: str) -> str:
        seen.append(threading.current_thread())
        return "listing"

    registry._directory_lister_tool = MagicMock(function=lister)
    assert await registry.list_directory(".") == "listing"
    assert seen and loop_thread not in seen


async def test_a_create_waits_for_an_edit_holding_the_shared_lock(
    tmp_path: Path,
) -> None:
    # FileWriter took no lock, so a create and a surgical edit of one path
    # from parallel tool calls were not serialised against each other.
    from codebase_rag.tools.file_editor import FileEditor

    editor = FileEditor(project_root=str(tmp_path))
    writer = FileWriter(project_root=str(tmp_path), write_lock=editor.write_lock)
    async with editor.write_lock:
        task = asyncio.create_task(writer.create_file("a.py", "x = 1\n"))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert not (tmp_path / "a.py").exists()
    result = await task
    assert result.success
