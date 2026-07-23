# A literal containing an escape sequence must keep its whole rendering in the
# resource identity (issue #944). Most grammars split a string into content
# fragments AROUND an `escape_sequence` sibling, so joining content children
# alone silently drops the escape and everything it separates collapses:
# `'/logs\tdaily'` renders as `/logsdaily` and mislinks to whatever resource
# happens to own that fabricated path. Python nests the escape inside its
# `string_content` node, so it was already whole; every other language here was
# not.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_io(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
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


def _has(rels: set[tuple[str, str, str]], caller: str, resource: str) -> bool:
    return any(
        a.partition("(")[0].endswith(caller) and b == resource for a, _, b in rels
    )


def test_js_string_keeps_escape(tmp_path: Path) -> None:
    files = {"app.js": "function load() {\n  fetch('/logs\\tdaily');\n}\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.load", "resource::NETWORK::/logs\\tdaily"), rels


def test_ts_template_literal_keeps_escape_beside_placeholder(tmp_path: Path) -> None:
    files = {
        "app.ts": (
            "export function load(id: string) {\n"
            "  fetch(`/logs\\tdaily/${id}`);\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.load", "resource::NETWORK::/logs\\tdaily/{id}"), rels


def test_go_interpreted_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "net/http"\n\n'
            "func fetchLogs() (*http.Response, error) {\n"
            '\treturn http.Get("http://svc:8000/logs\\tdaily")\n'
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(
        rels, "main.fetchLogs", "resource::NETWORK::http://svc:8000/logs\\tdaily"
    ), rels


def test_csharp_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  string Load() {\n"
            '    return System.IO.File.ReadAllText("logs\\tdaily.txt");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "A.Load", "resource::FILE::logs\\tdaily.txt"), rels


def test_rust_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "main.rs": (
            "fn load() -> std::io::Result<String> {\n"
            '    std::fs::read_to_string("logs\\tdaily.txt")\n'
            "}\n"
        )
    }
    rels = _run_io(tmp_path, files)
    assert _has(rels, "main.load", "resource::FILE::logs\\tdaily.txt"), rels


def _exposes(tmp_path: Path, files: dict[str, str]) -> set[str]:
    # Every registered ENDPOINT identity, so the server side of the same
    # literal is checked against the same rendering rule as the client side.
    parsers, queries = load_parsers()
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
        c.args[2][2].split("::")[-1]
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == cs.RelationshipType.EXPOSES.value
    }


def test_js_route_path_keeps_escape(tmp_path: Path) -> None:
    # A route path and a client URL must render identically or the two sides
    # of the same literal never link.
    files = {
        "server.js": (
            "const express = require('express')\n"
            "const app = express()\n\n"
            "function daily(req, res) { res.json({}) }\n\n"
            "app.get('/logs\\tdaily', daily)\n"
        )
    }
    assert "GET /logs\\tdaily" in _exposes(tmp_path, files)


def test_go_route_pattern_keeps_escape(tmp_path: Path) -> None:
    files = {
        "main.go": (
            "package main\n\n"
            'import "net/http"\n\n'
            "func daily(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func main() {\n"
            '\thttp.HandleFunc("GET /logs\\tdaily", daily)\n'
            "}\n"
        )
    }
    assert "GET /logs\\tdaily" in _exposes(tmp_path, files)


def test_escape_only_string_carries_identity(tmp_path: Path) -> None:
    # An escape sequence IS literal text, exactly like a `/` fragment, so a
    # string made only of escapes has a real identity and must not fall back
    # to the dynamic node reserved for placeholder-only strings.
    files = {"app.js": "function load() {\n  fetch('\\u002Fapi');\n}\n"}
    rels = _run_io(tmp_path, files)
    assert _has(rels, "app.load", "resource::NETWORK::\\u002Fapi"), rels
