from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_io(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, resource_qn)
    # (H) for READS_FROM / WRITES_TO edges only.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in (READS_FROM, WRITES_TO)
    }


def _has(rels: set[tuple[str, str, str]], caller: str, rel: str, resource: str) -> bool:
    return any(a.endswith(caller) and r == rel and b == resource for a, r, b in rels)


def test_open_read_literal(tmp_path: Path) -> None:
    files = {"m.py": "def load():\n    open('config.yaml')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.load", READS_FROM, "resource::FILE::config.yaml")


def test_open_write_mode(tmp_path: Path) -> None:
    files = {"m.py": "def save():\n    open('out.txt', 'w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_open_write_dynamic_target(tmp_path: Path) -> None:
    files = {"m.py": "def save(path):\n    open(path, 'w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::<dynamic>")


def test_open_keyword_mode_write(tmp_path: Path) -> None:
    # (H) mode passed by keyword must still refine direction to WRITE.
    files = {"m.py": "def save():\n    open('out.txt', mode='w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_open_all_keyword_args(tmp_path: Path) -> None:
    # (H) both target and mode by keyword: target and direction still resolve.
    files = {"m.py": "def save():\n    open(file='out.txt', mode='w')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::out.txt")


def test_requests_get_read(tmp_path: Path) -> None:
    files = {
        "m.py": "import requests\n\ndef fetch(url):\n    requests.get(url)\n",
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.fetch", READS_FROM, "resource::NETWORK::<dynamic>")


def test_os_getenv_read(tmp_path: Path) -> None:
    files = {"m.py": "import os\n\ndef cfg():\n    os.getenv('HOME')\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.cfg", READS_FROM, "resource::ENV::HOME")


def test_print_write_stdout(tmp_path: Path) -> None:
    files = {"m.py": "def show(x):\n    print(x)\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.show", WRITES_TO, "resource::STDOUT::<dynamic>")


def test_file_handle_write(tmp_path: Path) -> None:
    files = {
        "m.py": ("def save(data):\n    f = open('a.txt', 'w')\n    f.write(data)\n"),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.save", WRITES_TO, "resource::FILE::a.txt")


def test_with_handle_binding_tracked(tmp_path: Path) -> None:
    # (H) `with sqlite3.connect(...) as conn:` binds a handle whose constructor is
    # (H) not itself a sink; the later conn.execute must still attribute the edge.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    with sqlite3.connect('db.sqlite') as conn:\n"
            "        conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::db.sqlite")


def test_handle_reassignment_uses_last_binding(tmp_path: Path) -> None:
    # (H) A rebind must resolve to the last assignment in source order. Uses
    # (H) sqlite3.connect (a handle constructor that is NOT itself a sink) so the
    # (H) only edge comes from handle attribution, isolating traversal order.
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    conn = sqlite3.connect('first.db')\n"
            "    conn = sqlite3.connect('second.db')\n"
            "    conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::second.db")
    assert not _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::first.db")


def test_db_handle_select_is_read(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def q():\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    conn.execute('SELECT * FROM t')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.q", READS_FROM, "resource::DATABASE::db.sqlite")


def test_db_handle_insert_is_write(tmp_path: Path) -> None:
    files = {
        "m.py": (
            "import sqlite3\n\n"
            "def ins():\n"
            "    conn = sqlite3.connect('db.sqlite')\n"
            "    conn.execute('INSERT INTO t VALUES (1)')\n"
        ),
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "m.ins", WRITES_TO, "resource::DATABASE::db.sqlite")


def test_plain_call_emits_no_io(tmp_path: Path) -> None:
    files = {
        "m.py": "def helper():\n    return 1\n\ndef run():\n    helper()\n",
    }
    rels = _run_io(tmp_path, files)
    assert rels == set()
