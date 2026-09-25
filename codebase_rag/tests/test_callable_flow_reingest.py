"""Callable-parameter flow records must describe the CURRENT source.

The call processor outlives a single pass (watch mode, the MCP updater, a
reused `run()`), so a call site's recorded callback kept emitting its CALLS
edge after the edit that removed it. Records of files the pass does not
re-walk must survive, though: a pass-through chain can seed a slot from a
file a scoped re-ingest leaves alone."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from watchdog.events import FileModifiedEvent

import realtime_updater
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import get_relationships

MAIN_TEMPLATE = (
    "def run(cb):\n"
    "    cb()\n\n\n"
    "def on_a():\n"
    "    return 1\n\n\n"
    "def on_b():\n"
    "    return 2\n\n\n"
    "run({arg})\n"
)


def _write(project: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(mock_ingestor, "CALLS")
    }


def _updater(project: Path, mock_ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    return GraphUpdater(
        ingestor=mock_ingestor, repo_path=project, parsers=parsers, queries=queries
    )


def _watch(updater: GraphUpdater) -> realtime_updater.CodeChangeEventHandler:
    handler = realtime_updater.CodeChangeEventHandler(updater, debounce_seconds=0)
    handler.ignore_patterns = handler.ignore_patterns - {"tmp", "temp"}
    return handler


def test_watch_edit_drops_the_replaced_callback_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "flow_watch"
    _write(project, {"main.py": MAIN_TEMPLATE.format(arg="on_a")})
    updater = _updater(project, mock_ingestor)
    updater.run()
    base = project.name
    assert (f"{base}.main.run", f"{base}.main.on_a") in _calls(mock_ingestor)

    handler = _watch(updater)
    main = project / "main.py"
    # Several saves: each one used to append the file's records again.
    for _ in range(3):
        main.write_text(MAIN_TEMPLATE.format(arg="on_b"), encoding="utf-8")
        mock_ingestor.reset_mock()
        handler.dispatch(FileModifiedEvent(str(main)))

        calls = _calls(mock_ingestor)
        assert (f"{base}.main.run", f"{base}.main.on_b") in calls
        assert (f"{base}.main.run", f"{base}.main.on_a") not in calls, sorted(calls)


def test_forced_rerun_drops_the_replaced_callback_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "flow_force"
    _write(project, {"main.py": MAIN_TEMPLATE.format(arg="on_a")})
    updater = _updater(project, mock_ingestor)
    updater.run()

    (project / "main.py").write_text(MAIN_TEMPLATE.format(arg="on_b"), encoding="utf-8")
    mock_ingestor.reset_mock()
    updater.run(force=True)

    base = project.name
    calls = _calls(mock_ingestor)
    assert (f"{base}.main.run", f"{base}.main.on_b") in calls
    assert (f"{base}.main.run", f"{base}.main.on_a") not in calls, sorted(calls)


def test_scoped_edit_keeps_a_seed_from_an_untouched_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # app.py seeds outer's param; outer forwards it to lib.run, which invokes
    # it. Editing lib.py re-walks lib.py and its caller mid.py, never app.py,
    # yet run's recreated node still needs run -> on_c, which only app.py's
    # record supplies.
    project = temp_repo / "flow_chain"
    _write(
        project,
        {
            "lib.py": "def run(cb):\n    cb()\n",
            "mid.py": "from lib import run\n\n\ndef outer(cb):\n    run(cb)\n",
            "app.py": (
                "from mid import outer\n\n\n"
                "def on_c():\n"
                "    return 3\n\n\n"
                "outer(on_c)\n"
            ),
        },
    )
    updater = _updater(project, mock_ingestor)
    updater.run()
    base = project.name
    edge = (f"{base}.lib.run", f"{base}.app.on_c")
    assert edge in _calls(mock_ingestor)

    lib = project / "lib.py"
    lib.write_text(lib.read_text() + "# touched\n", encoding="utf-8")
    mock_ingestor.reset_mock()
    _watch(updater).dispatch(FileModifiedEvent(str(lib)))

    assert edge in _calls(mock_ingestor), sorted(_calls(mock_ingestor))


def test_forgetting_a_file_does_not_build_the_call_processor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An incremental run clears a changed file's state before the definition
    # pass; building the processor there snapshots half-built state, which
    # left C# methods ingested under bare, unnamespaced names.
    _write(temp_repo, {"main.py": MAIN_TEMPLATE.format(arg="on_a")})
    updater = _updater(temp_repo, mock_ingestor)

    updater.remove_file_from_state(temp_repo / "main.py")

    assert updater.factory._call_processor is None
