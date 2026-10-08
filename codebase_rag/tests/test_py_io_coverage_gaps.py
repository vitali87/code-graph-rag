"""Everyday Python I/O shapes record their READS_FROM / WRITES_TO edges.

With `--capture io`, an inline `Path("...").read_text()` recorded nothing
although the bound form (`p = Path(...); p.read_text()`) did, and
`sys.stdout.write`, `os.environ.setdefault` and `subprocess.run` /
`os.system` were not sinks at all, so a command read from the environment
and run in a shell left no `ENV -> PROCESS` flow (issue #2778).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_CONFIG = "resource::FILE::/etc/tool/config.toml"
_PROCESS = "resource::PROCESS::<dynamic>"

_TOOL = """\
import asyncio
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def bound_path():
    p = Path("/etc/tool/config.toml")
    return p.read_text()


def inline_path():
    return Path("/etc/tool/config.toml").read_text()


def inline_write(text):
    Path("/tmp/tool.out").write_text(text)


def inline_open():
    return open("/tmp/tool.in").read()


def inline_db():
    return sqlite3.connect("tool.db").execute("SELECT 1")


def write_out(line):
    sys.stdout.write(line)


def write_err(line):
    sys.stderr.write(line)


def env_default():
    return os.environ.setdefault("TOOL_HOME", "/opt/tool")


def env_seed():
    os.environ.setdefault("TOOL_HOME", os.getenv("HOME"))


def run_user_cmd():
    cmd = os.getenv("TOOL_CMD")
    subprocess.run(cmd, shell=True)


def run_make():
    os.system("make build")


def run_others():
    subprocess.call(["ls"])
    subprocess.check_call(["ls"])
    subprocess.check_output(["git", "status"])
    subprocess.Popen(["sleep", "1"])
    os.popen("uptime")


def make_path(p):
    return Path(p)


def not_io(config, items):
    Path("/etc/tool/config.toml").exists()
    make_path("/etc/tool/other.toml").read_text()
    io.StringIO().write("x")
    config.setdefault("TOOL_HOME", "/opt/tool")
    asyncio.run(asyncio.sleep(0))
    items.run()
"""


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str, str]]:
    root = tmp_path_factory.mktemp("io2778") / "tool"
    root.mkdir()
    (root / "tool.py").write_text(_TOOL, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="tool",
        capture=resolve_capture(["io"]),
    ).run(force=True)
    return {
        (str(c.args[0][2]).rsplit(".", 1)[-1], str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in ("READS_FROM", "WRITES_TO", "FLOWS_TO")
    }


def _io(edges: set[tuple[str, str, str]], caller: str) -> set[tuple[str, str]]:
    return {(rel, dst) for src, rel, dst in edges if src == caller}


def test_an_inline_handle_constructor_is_the_resource(
    edges: set[tuple[str, str, str]],
) -> None:
    assert _io(edges, "bound_path") == {("READS_FROM", _CONFIG)}, edges
    assert _io(edges, "inline_path") == {("READS_FROM", _CONFIG)}, edges
    assert _io(edges, "inline_write") == {
        ("WRITES_TO", "resource::FILE::/tmp/tool.out")
    }, edges
    assert _io(edges, "inline_open") == {
        ("READS_FROM", "resource::FILE::/tmp/tool.in")
    }, edges
    assert ("READS_FROM", "resource::DATABASE::tool.db") in _io(edges, "inline_db"), (
        edges
    )


def test_standard_stream_writes(edges: set[tuple[str, str, str]]) -> None:
    assert _io(edges, "write_out") == {("WRITES_TO", "resource::STDOUT::<dynamic>")}, (
        edges
    )
    assert _io(edges, "write_err") == {("WRITES_TO", "resource::STDERR::<dynamic>")}, (
        edges
    )


def test_env_setdefault_reads_and_writes(edges: set[tuple[str, str, str]]) -> None:
    home = "resource::ENV::TOOL_HOME"
    assert _io(edges, "env_default") == {
        ("READS_FROM", home),
        ("WRITES_TO", home),
    }, edges


def test_subprocess_and_os_system_run_a_process(
    edges: set[tuple[str, str, str]],
) -> None:
    assert ("WRITES_TO", _PROCESS) in _io(edges, "run_user_cmd"), edges
    assert _io(edges, "run_make") == {("WRITES_TO", "resource::PROCESS::make build")}, (
        edges
    )
    assert _io(edges, "run_others") == {
        ("WRITES_TO", _PROCESS),
        ("WRITES_TO", "resource::PROCESS::uptime"),
    }, edges


def test_values_flow_into_the_process_and_the_env(
    edges: set[tuple[str, str, str]],
) -> None:
    flows = {(src, dst) for src, rel, dst in edges if rel == "FLOWS_TO"}
    assert ("resource::ENV::TOOL_CMD", _PROCESS) in flows, flows
    # setdefault writes its default into the variable.
    assert ("resource::ENV::HOME", "resource::ENV::TOOL_HOME") in flows, flows


def test_look_alike_calls_are_not_io(edges: set[tuple[str, str, str]]) -> None:
    # Negatives: a non-I/O Path method, a method on a call that is not a
    # handle constructor, an in-memory buffer, a dict's setdefault,
    # asyncio.run and a method named run on a parameter record nothing.
    assert _io(edges, "not_io") == set(), edges
