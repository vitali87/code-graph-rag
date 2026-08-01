# A literal containing an escape sequence must keep its whole rendering in the
# resource identity (issue #944). Most grammars split a string into content
# fragments AROUND an `escape_sequence` sibling, so joining content children
# alone silently drops the escape and everything it separates collapses:
# `'/logs\tdaily'` renders as `/logsdaily` and mislinks to whatever resource
# happens to own that fabricated path. Python and Lua nest the escape inside
# their content node, so those were already whole; every other language here
# was not. The rendering is the SOURCE text, and both the client sink and the
# server route reader use it, so the two sides of one literal still meet.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
EXPOSES = cs.RelationshipType.EXPOSES.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _edges(
    tmp_path: Path, files: dict[str, str], rel_type: str
) -> set[tuple[str, str]]:
    # (source qn, target identity) for every edge of `rel_type`.
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
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def _reads(tmp_path: Path, files: dict[str, str], caller: str, resource: str) -> bool:
    # Java method qns carry a parameter signature suffix; match on the qn with
    # any trailing `(...)` stripped.
    return any(
        src.partition("(")[0].endswith(caller) and tgt == resource
        for src, tgt in _edges(tmp_path, files, READS_FROM)
    )


def _endpoints(tmp_path: Path, files: dict[str, str]) -> set[str]:
    return {tgt.split("::")[-1] for _src, tgt in _edges(tmp_path, files, EXPOSES)}


def test_js_string_keeps_escape(tmp_path: Path) -> None:
    files = {"app.js": "function load() {\n  fetch('/logs\\tdaily');\n}\n"}
    assert _reads(tmp_path, files, "app.load", "resource::NETWORK::/logs\\tdaily")


def test_ts_template_literal_keeps_escape_beside_placeholder(tmp_path: Path) -> None:
    files = {
        "app.ts": (
            "export function load(id: string) {\n  fetch(`/logs\\tdaily/${id}`);\n}\n"
        )
    }
    assert _reads(tmp_path, files, "app.load", "resource::NETWORK::/logs\\tdaily/{id}")


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
    assert _reads(
        tmp_path,
        files,
        "main.fetchLogs",
        "resource::NETWORK::http://svc:8000/logs\\tdaily",
    )


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
    assert _reads(tmp_path, files, "A.Load", "resource::FILE::logs\\tdaily.txt")


def test_rust_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "main.rs": (
            "fn load() -> std::io::Result<String> {\n"
            '    std::fs::read_to_string("logs\\tdaily.txt")\n'
            "}\n"
        )
    }
    assert _reads(tmp_path, files, "main.load", "resource::FILE::logs\\tdaily.txt")


def test_java_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.io.BufferedReader;\n"
            "import java.io.FileReader;\n"
            "class A {\n"
            "  void load() throws Exception {\n"
            "    BufferedReader br = "
            'new BufferedReader(new FileReader("logs\\tdaily.txt"));\n'
            "    String line = br.readLine();\n"
            "  }\n"
            "}\n"
        )
    }
    assert _reads(tmp_path, files, "A.load", "resource::FILE::logs\\tdaily.txt")


def test_cpp_string_keeps_escape(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <fstream>\n"
            "void load() {\n"
            '    std::ifstream in("logs\\tdaily.txt");\n'
            "    std::string word;\n"
            "    in >> word;\n"
            "}\n"
        )
    }
    assert _reads(tmp_path, files, "main.load", "resource::FILE::logs\\tdaily.txt")


def test_escape_beside_placeholder_only_keeps_identity(tmp_path: Path) -> None:
    # An escape sequence is literal text, exactly like the `/` in
    # `` `${host}/${path}` ``, so a template whose only literal is an escape
    # keeps that identity instead of collapsing to the dynamic node reserved
    # for strings made purely of placeholders.
    files = {
        "app.js": ("function load(host, path) {\n  fetch(`${host}\\t${path}`);\n}\n")
    }
    assert _reads(tmp_path, files, "app.load", "resource::NETWORK::{host}\\t{path}")


def test_placeholders_without_any_literal_stay_dynamic(tmp_path: Path) -> None:
    # The dynamic fallback still applies when nothing literal survives: same
    # named variables in unrelated modules must not share a resource node.
    files = {"app.js": ("function load(host, path) {\n  fetch(`${host}${path}`);\n}\n")}
    assert _reads(tmp_path, files, "app.load", "resource::NETWORK::<dynamic>")


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
    assert "GET /logs\\tdaily" in _endpoints(tmp_path, files)


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
    assert "GET /logs\\tdaily" in _endpoints(tmp_path, files)


def test_go_raw_route_pattern_keeps_backslash(tmp_path: Path) -> None:
    # A Go raw string has no escape children at all; its content node already
    # holds the backslash verbatim, and the reader must leave it alone.
    files = {
        "main.go": (
            "package main\n\n"
            'import "net/http"\n\n'
            "func daily(w http.ResponseWriter, r *http.Request) {}\n\n"
            "func main() {\n"
            "\thttp.HandleFunc(`GET /logs\\tdaily`, daily)\n"
            "}\n"
        )
    }
    assert "GET /logs\\tdaily" in _endpoints(tmp_path, files)
