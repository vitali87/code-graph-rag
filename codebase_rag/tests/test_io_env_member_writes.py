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
    parsers, queries = load_parsers()
    if "javascript" not in parsers:
        pytest.skip("javascript parser not available")
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
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


def test_env_member_assignment_is_a_write(tmp_path: Path) -> None:
    # (H) `process.env.KEY = v` mutates the environment (dotenv's core
    # (H) behavior); the member walk fired on the assignment LHS and emitted a
    # (H) READ, mislabeling every env write.
    rels = _run_io(
        tmp_path,
        {"m.js": "export function set(v) { process.env.MY_KEY = v }\n"},
    )
    assert _has(rels, "m.set", WRITES_TO, "resource::ENV::MY_KEY"), rels
    assert not _has(rels, "m.set", READS_FROM, "resource::ENV::MY_KEY"), rels


def test_env_subscript_assignment_is_a_write(tmp_path: Path) -> None:
    rels = _run_io(
        tmp_path,
        {"m.js": "export function set(v) { process.env['SUB_KEY'] = v }\n"},
    )
    assert _has(rels, "m.set", WRITES_TO, "resource::ENV::SUB_KEY"), rels
    assert not _has(rels, "m.set", READS_FROM, "resource::ENV::SUB_KEY"), rels


def test_env_augmented_assignment_reads_and_writes(tmp_path: Path) -> None:
    # (H) `process.env.KEY += v` reads the old value and writes the new one.
    rels = _run_io(
        tmp_path,
        {"m.js": "export function grow(v) { process.env.PATH_LIKE += v }\n"},
    )
    assert _has(rels, "m.grow", WRITES_TO, "resource::ENV::PATH_LIKE"), rels
    assert _has(rels, "m.grow", READS_FROM, "resource::ENV::PATH_LIKE"), rels


def test_env_update_expression_reads_and_writes(tmp_path: Path) -> None:
    # (H) `process.env.COUNT++` increments in place: an update_expression
    # (H) parent reads the old value and writes the new one.
    rels = _run_io(
        tmp_path,
        {"m.js": "export function bump() { process.env.COUNT++ }\n"},
    )
    assert _has(rels, "m.bump", WRITES_TO, "resource::ENV::COUNT"), rels
    assert _has(rels, "m.bump", READS_FROM, "resource::ENV::COUNT"), rels


def test_env_member_read_stays_a_read(tmp_path: Path) -> None:
    rels = _run_io(
        tmp_path,
        {"m.js": "export function get() { return process.env.READ_KEY }\n"},
    )
    assert _has(rels, "m.get", READS_FROM, "resource::ENV::READ_KEY"), rels
    assert not _has(rels, "m.get", WRITES_TO, "resource::ENV::READ_KEY"), rels
