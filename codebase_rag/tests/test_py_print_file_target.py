"""Python `print(..., file=...)` writes where its `file=` says.

The registry declared `print` an unconditional STDOUT write, so
`print(msg, file=sys.stderr)` was a STDOUT write and `print(token, file=f)`
with `f` from `open("/tmp/token.txt", "w")` recorded a STDOUT write and an
`ENV -> STDOUT` leak that does not exist, while the real `ENV -> FILE`
flow was missing (issue #2776).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_STDOUT = "resource::STDOUT::<dynamic>"
_STDERR = "resource::STDERR::<dynamic>"
_TOKEN_FILE = "resource::FILE::/tmp/token.txt"
_ENV = "resource::ENV::REPORT_TOKEN"

_REPORT = """\
import os
import sys
from sys import stderr


def warn(msg):
    print(msg, file=sys.stderr)


def warn_imported(msg):
    print(msg, file=stderr)


def save_token():
    token = os.getenv("REPORT_TOKEN")
    with open("/tmp/token.txt", "w") as f:
        print(token, file=f)


def leak_to_stderr():
    print(os.getenv("REPORT_TOKEN"), file=sys.stderr)


def shout():
    print("hello")


def shout_explicitly():
    print("hello", file=sys.stdout)


def leak_to_stdout():
    print(os.getenv("REPORT_TOKEN"))


def to_somewhere(stream):
    print(os.getenv("REPORT_TOKEN"), file=stream)


def to_none():
    print("hello", file=None)


class Journal:
    def __init__(self):
        self.log = open("/tmp/journal.log", "a")

    def note(self, msg):
        print(msg, file=self.log)
"""

# The secret reaches a file and stderr, never stdout.
_NO_STDOUT_LEAK = """\
import os
import sys


def save_token():
    token = os.getenv("REPORT_TOKEN")
    f = open("/tmp/token.txt", "w")
    print(token, file=f)
    f = open("/tmp/later.txt", "w")


def leak_to_stderr():
    print(os.getenv("REPORT_TOKEN"), file=sys.stderr)


def to_somewhere(stream):
    print(os.getenv("REPORT_TOKEN"), file=stream)


def log_started():
    log = open(os.getenv("LOG_PATH"), "w")
    print("started", file=log)
"""


def _edges(tmp_path: Path, source: str = _REPORT) -> set[tuple[str, str, str]]:
    root = tmp_path / "report"
    root.mkdir()
    (root / "report.py").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="report",
        capture=resolve_capture(["io"]),
    ).run(force=True)
    return {
        (str(c.args[0][2]).rsplit(".", 1)[-1], str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in ("WRITES_TO", "FLOWS_TO")
    }


def _writes(edges: set[tuple[str, str, str]], caller: str) -> set[str]:
    return {dst for src, rel, dst in edges if src == caller and rel == "WRITES_TO"}


def test_a_stderr_print_writes_to_stderr(tmp_path: Path) -> None:
    edges = _edges(tmp_path)
    assert _writes(edges, "warn") == {_STDERR}, edges
    assert _writes(edges, "warn_imported") == {_STDERR}, edges


def test_a_print_into_a_file_handle_writes_the_file(tmp_path: Path) -> None:
    edges = _edges(tmp_path)
    assert _writes(edges, "save_token") == {_TOKEN_FILE}, edges
    assert _writes(edges, "note") == {"resource::FILE::/tmp/journal.log"}, edges


def test_the_secret_flows_where_print_sends_it(tmp_path: Path) -> None:
    flows = {(src, dst) for src, rel, dst in _edges(tmp_path) if rel == "FLOWS_TO"}
    assert (_ENV, _TOKEN_FILE) in flows, flows
    assert (_ENV, _STDERR) in flows, flows


def test_stdout_prints_and_unknown_streams(tmp_path: Path) -> None:
    # Negatives: no `file=`, or `file=sys.stdout`, is still STDOUT, and a
    # stream the walk cannot name is not claimed as STDOUT.
    edges = _edges(tmp_path)
    assert _writes(edges, "shout") == {_STDOUT}, edges
    assert _writes(edges, "shout_explicitly") == {_STDOUT}, edges
    assert _writes(edges, "to_none") == {_STDOUT}, edges
    assert _STDOUT not in _writes(edges, "to_somewhere"), edges
    flows = {(src, dst) for src, rel, dst in edges if rel == "FLOWS_TO"}
    # Only `leak_to_stdout` sends the secret to stdout.
    assert (_ENV, _STDOUT) in flows, flows


def test_a_secret_printed_elsewhere_never_reaches_stdout(tmp_path: Path) -> None:
    # Negative: the flow follows the handle bound before the print, not a
    # later rebind, and an unnamed stream is not stdout.
    flows = {
        (src, dst)
        for src, rel, dst in _edges(tmp_path, _NO_STDOUT_LEAK)
        if rel == "FLOWS_TO"
    }
    assert (_ENV, _TOKEN_FILE) in flows, flows
    assert (_ENV, _STDERR) in flows, flows
    assert (_ENV, _STDOUT) not in flows, flows
    assert (_ENV, "resource::FILE::/tmp/later.txt") not in flows, flows
    # The `file=` handle is where the data goes, not data flowing into it.
    assert not any(src == dst for src, dst in flows), flows
