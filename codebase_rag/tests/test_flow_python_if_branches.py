# `if` / `elif` / `else` path-sensitivity for the Python flow walk. Each
# branch body is EXCLUSIVE and walks against a copy of the state reached by
# its condition, and an `elif` condition runs on every path that gets past
# the earlier ones. Without an `else`, the skip path keeps the incoming
# state. MAY semantics: a kill counts only when it happens on every path.
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


def test_a_kill_in_one_elif_branch_keeps_the_other_branches_taint(
    tmp_path: Path,
) -> None:
    source = (
        "import os\n\n"
        "def work(x):\n"
        "    s = os.getenv('SECRET')\n"
        "    if x == 1:\n"
        "        pass\n"
        "    elif x == 2:\n"
        "        s = 'clean'\n"
        "    else:\n"
        "        pass\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT in _run_flow(tmp_path, source)


def test_a_kill_on_every_if_elif_else_branch_kills(tmp_path: Path) -> None:
    source = (
        "import os\n\n"
        "def work(x):\n"
        "    s = os.getenv('SECRET')\n"
        "    if x == 1:\n"
        "        s = 'clean'\n"
        "    elif x == 2:\n"
        "        s = 'clean'\n"
        "    else:\n"
        "        s = 'clean'\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT not in _run_flow(tmp_path, source)


def test_a_kill_on_every_branch_without_else_keeps_the_skip_path(
    tmp_path: Path,
) -> None:
    source = (
        "import os\n\n"
        "def work(x):\n"
        "    s = os.getenv('SECRET')\n"
        "    if x == 1:\n"
        "        s = 'clean'\n"
        "    elif x == 2:\n"
        "        s = 'clean'\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT in _run_flow(tmp_path, source)


def test_taint_introduced_in_an_elif_body_reaches_the_join(tmp_path: Path) -> None:
    source = (
        "import os\n\n"
        "def work(x):\n"
        "    s = 'clean'\n"
        "    if x == 1:\n"
        "        pass\n"
        "    elif x == 2:\n"
        "        s = os.getenv('SECRET')\n"
        "    print(s)\n"
    )
    assert _SECRET_TO_STDOUT in _run_flow(tmp_path, source)
