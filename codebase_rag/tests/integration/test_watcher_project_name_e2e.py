"""Issue #2432 against a real Memgraph: `cgr start`, then `cgr watch`, one project.

The reproduction from the issue: sync a checkout with `cgr start
--update-graph`, start the watcher on it, edit a file. The watcher used to
re-index the checkout into a second project named after the bare directory
and apply the edit there, so `cgr start`'s project never saw it. The watcher
runs as the shipped `cgr watch` command (issue #2433).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner
from watchdog.events import FileModifiedEvent

from codebase_rag import watch
from codebase_rag.cli import app
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.integration]

runner = CliRunner()


def test_the_watchers_edit_reaches_the_project_cgr_start_synced(
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, port = str(memgraph_container["host"]), int(memgraph_container["port"])
    shop = (tmp_path / "watchdemo" / "shop").resolve()
    shop.mkdir(parents=True)
    source = shop / "m.py"
    source.write_text("def a():\n    return 1\n", encoding="utf-8")

    def connect(batch_size: int) -> MemgraphIngestor:
        return MemgraphIngestor(host=host, port=port, batch_size=batch_size)

    with (
        patch("codebase_rag.cli.connect_memgraph", connect),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        synced = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(shop),
                "--update-graph",
                "--no-start-stack",
                "--no-embeddings",
            ],
        )
    assert synced.exit_code == 0, synced.output

    observer_cls = MagicMock()

    def edit_then_stop(_interval: float) -> None:
        # The watch loop's first sleep: the watcher has scanned and is live.
        handler = observer_cls.return_value.schedule.call_args.args[0]
        source.write_text(
            "def a():\n    return 1\n\ndef b_added():\n    return a()\n",
            encoding="utf-8",
        )
        handler.dispatch(FileModifiedEvent(str(source)))
        raise KeyboardInterrupt

    monkeypatch.setattr(watch, "connect_memgraph", connect)
    monkeypatch.setattr(watch, "Observer", observer_cls)
    monkeypatch.setattr(
        watch, "time", SimpleNamespace(sleep=edit_then_stop, time=time.time)
    )
    watched = runner.invoke(
        app,
        ["watch", "--repo-path", str(shop), "--debounce", "0", "--no-start-stack"],
    )
    assert watched.exit_code == 0, watched.output

    project = derive_project_name(shop)
    projects = memgraph_ingestor.fetch_all(
        "MATCH (p:Project {root_path: $root}) RETURN p.name AS name",
        {"root": str(shop)},
    )
    assert [row["name"] for row in projects] == [project]
    functions = memgraph_ingestor.fetch_all(
        "MATCH (f:Function) RETURN f.qualified_name AS qn ORDER BY qn"
    )
    assert [row["qn"] for row in functions] == [
        f"{project}.m.a",
        f"{project}.m.b_added",
    ]
