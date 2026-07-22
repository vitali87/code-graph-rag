"""Concurrent edit_file calls must not write the same tree concurrently.

Offloading the write to a thread removed the event-loop serialisation the
old synchronous write provided, so overlapping writes could interleave.
"""

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

from codebase_rag.tools.file_editor import FileEditor


def test_concurrent_edits_do_not_overlap(tmp_path: Path) -> None:
    editor = FileEditor(project_root=str(tmp_path))
    target = tmp_path / "target.txt"
    target.write_text("initial")

    in_flight = 0
    max_in_flight = 0
    counter_lock = threading.Lock()
    real_write_text = Path.write_text

    def slow_write_text(self: Path, data: str, encoding: str | None = None) -> int:
        nonlocal in_flight, max_in_flight
        with counter_lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        try:
            time.sleep(0.05)
            return real_write_text(self, data, encoding=encoding)
        finally:
            with counter_lock:
                in_flight -= 1

    async def scenario() -> None:
        with patch.object(Path, "write_text", slow_write_text):
            await asyncio.gather(
                editor.edit_file(str(target), "a" * 32),
                editor.edit_file(str(target), "b" * 32),
            )

    asyncio.run(scenario())
    assert max_in_flight == 1
    assert target.read_text() in ("a" * 32, "b" * 32)
