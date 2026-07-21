from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_io(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "javascript" not in parsers:
        pytest.skip("javascript parser not available")
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
        if str(c.args[1]) == READS_FROM
    }


def _reads_file(rels: set[tuple[str, str, str]], caller_suffix: str) -> bool:
    return any(
        caller.endswith(caller_suffix) and resource.startswith("resource::FILE::")
        for caller, _rel, resource in rels
    )


def test_default_import_matches_file_sink(tmp_path: Path) -> None:
    # `import fs from 'fs'` maps fs -> fs.default; the sink lookup must
    # collapse the default-export segment to hit `fs.readFileSync`.
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import fs from 'fs'\n"
                "export function load(p) { return fs.readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_aliased_default_import_matches_file_sink(tmp_path: Path) -> None:
    # `import myFs from 'fs'` leaves no raw-text `fs.` prefix, so only the
    # normalised form (myFs -> fs.default) can match the sink.
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import myFs from 'fs'\n"
                "export function load(p) { return myFs.readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_node_prefixed_default_import_matches_file_sink(tmp_path: Path) -> None:
    # `import fs from 'node:fs'` (the officially recommended builtin form)
    # maps fs -> node:fs.default: both the scheme and the default-export
    # segment must be stripped for the sink key to match.
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import fs from 'node:fs'\n"
                "export function load(p) { return fs.readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_aliased_node_prefixed_default_import_matches_file_sink(
    tmp_path: Path,
) -> None:
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import fsNode from 'node:fs'\n"
                "export function load(p) { return fsNode.readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_node_prefixed_namespace_import_matches_file_sink(tmp_path: Path) -> None:
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import * as fs from 'node:fs'\n"
                "export function load(p) { return fs.readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_node_prefixed_named_import_matches_file_sink(tmp_path: Path) -> None:
    rels = _run_io(
        tmp_path,
        {
            "m.js": (
                "import { readFileSync } from 'node:fs'\n"
                "export function load(p) { return readFileSync(p, 'utf8') }\n"
            )
        },
    )
    assert _reads_file(rels, "m.load"), rels


def test_local_default_import_does_not_hit_builtin_sink(tmp_path: Path) -> None:
    # A default import of FIRST-PARTY code named like a builtin must stay
    # unmatched: ./fs resolves to a local module, not the node builtin.
    rels = _run_io(
        tmp_path,
        {
            "fs.js": "export default { readFileSync(p) { return p } }\n",
            "m.js": (
                "import fs from './fs'\n"
                "export function load(p) { return fs.readFileSync(p, 'utf8') }\n"
            ),
        },
    )
    assert not _reads_file(rels, "m.load"), rels
