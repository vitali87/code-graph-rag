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


WRITES_TO = cs.RelationshipType.WRITES_TO.value


def _run_io_directed(
    tmp_path: Path, files: dict[str, str]
) -> set[tuple[str, str, str]]:
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
        if str(c.args[1]) in (READS_FROM, WRITES_TO)
    }


def _edge(rels: set[tuple[str, str, str]], caller: str, rel: str, res: str) -> bool:
    return any(a.endswith(caller) and r == rel and b == res for a, r, b in rels)


class TestFetchMethodOption:
    """A fetch with a write verb in its options object is a write, not a read
    (issue #878 review): the direction gate would otherwise strip its
    endpoint link.
    """

    RESOURCE = "resource::NETWORK::http://svc:8000/users/1"

    def test_fetch_post_option_is_write(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function save() {\n"
                    "  return fetch('http://svc:8000/users/1', {method: 'POST'})\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.save", WRITES_TO, self.RESOURCE), rels
        assert not _edge(rels, "m.save", READS_FROM, self.RESOURCE), rels

    def test_fetch_delete_option_is_write(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function drop() {\n"
                    "  return fetch('http://svc:8000/users/1', {method: 'delete'})\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.drop", WRITES_TO, self.RESOURCE), rels

    def test_fetch_without_options_stays_read(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function load() {\n"
                    "  return fetch('http://svc:8000/users/1')\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.load", READS_FROM, self.RESOURCE), rels
        assert not _edge(rels, "m.load", WRITES_TO, self.RESOURCE), rels

    def test_fetch_dynamic_method_is_read_write(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function send(verb) {\n"
                    "  return fetch('http://svc:8000/users/1', {method: verb})\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.send", READS_FROM, self.RESOURCE), rels
        assert _edge(rels, "m.send", WRITES_TO, self.RESOURCE), rels

    def test_fetch_opaque_options_variable_is_read_write(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function send(opts) {\n"
                    "  return fetch('http://svc:8000/users/1', opts)\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.send", READS_FROM, self.RESOURCE), rels
        assert _edge(rels, "m.send", WRITES_TO, self.RESOURCE), rels


class TestTemplateLiteralUrls:
    """A template literal is the dominant way JS clients build URLs; its
    interpolations must become placeholders, not discard the whole URL
    (issue #884, the JS analogue of the Python f-string fix).
    """

    def test_fetch_template_literal_keeps_placeholder(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function load(productId) {\n"
                    "  return fetch(`http://svc:8000/products/${productId}`)\n"
                    "}\n"
                )
            },
        )
        assert _edge(
            rels,
            "m.load",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{productId}",
        ), rels

    def test_axios_template_literal_keeps_placeholder(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "import axios from 'axios'\n\n"
                    "export function stock(id) {\n"
                    "  return axios.get(`http://svc:8000/products/${id}/stock`)\n"
                    "}\n"
                )
            },
        )
        assert _edge(
            rels,
            "m.stock",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{id}/stock",
        ), rels

    def test_all_interpolation_template_stays_dynamic(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function go(base, path) {\n"
                    "  return fetch(`${base}${path}`)\n"
                    "}\n"
                )
            },
        )
        assert _edge(rels, "m.go", READS_FROM, "resource::NETWORK::<dynamic>"), rels

    def test_delimiter_bearing_substitution_collapses(self, tmp_path: Path) -> None:
        rels = _run_io_directed(
            tmp_path,
            {
                "m.js": (
                    "export function page(offset, limit) {\n"
                    "  return fetch(`http://svc:8000/products/${offset / limit}`)\n"
                    "}\n"
                )
            },
        )
        assert _edge(
            rels,
            "m.page",
            READS_FROM,
            "resource::NETWORK::http://svc:8000/products/{*}",
        ), rels
