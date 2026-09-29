# `try` / `except` / `else` / `finally` path-sensitivity for the Python flow
# walk. An except handler can run after the try body partially executed, so
# it starts from the union of the pre-try and post-body states; `else` runs
# only after the body completes; `finally` runs on every path, so it applies
# to the merged exit. MAY semantics: a kill counts only on every path.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_SECRET_TO_STDOUT = ("resource::ENV::SECRET", "resource::STDOUT::<dynamic>")


def _run_flow(tmp_path: Path, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    (tmp_path / "m.py").write_text(source, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def test_a_kill_in_the_try_body_leaves_the_handler_path_tainted(
    tmp_path: Path,
) -> None:
    source = (
        "import os\n\n"
        "def work():\n"
        "    s = os.getenv('SECRET')\n"
        "    try:\n"
        "        s = 'clean'\n"
        "    except ValueError:\n"
        "        pass\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT in _run_flow(tmp_path, source)


def test_a_kill_in_every_handler_and_else_kills(tmp_path: Path) -> None:
    source = (
        "import os\n\n"
        "def work():\n"
        "    s = os.getenv('SECRET')\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        s = 'clean'\n"
        "    else:\n"
        "        s = 'clean'\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT not in _run_flow(tmp_path, source)


def test_a_kill_in_finally_applies_to_every_path(tmp_path: Path) -> None:
    source = (
        "import os\n\n"
        "def work():\n"
        "    s = os.getenv('SECRET')\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        pass\n"
        "    finally:\n"
        "        s = 'clean'\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT not in _run_flow(tmp_path, source)


def test_taint_introduced_in_finally_reaches_the_sink(tmp_path: Path) -> None:
    source = (
        "import os\n\n"
        "def work():\n"
        "    s = 'clean'\n"
        "    try:\n"
        "        pass\n"
        "    finally:\n"
        "        s = os.getenv('SECRET')\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT in _run_flow(tmp_path, source)
