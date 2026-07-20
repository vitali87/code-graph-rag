# (H) Java networking I/O: the java.net / java.net.http surface (URL.openStream,
# (H) URLConnection, HttpClient.send) is NETWORK, previously invisible to the
# (H) catalog which only modelled java.net.Socket as a SOCKET.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

READS_FROM = cs.RelationshipType.READS_FROM.value
WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
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
    return any(
        a.partition("(")[0].endswith(caller) and r == rel and b == resource
        for a, r, b in rels
    )


def test_java_url_open_stream_reads_network(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.net.URL;\n"
            "import java.io.InputStream;\n"
            "class A {\n"
            "  void fetch() throws Exception {\n"
            '    URL u = new URL("http://example.com/data");\n'
            "    InputStream in = u.openStream();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(
        rels, "A.fetch", READS_FROM, "resource::NETWORK::http://example.com/data"
    ), rels


def test_java_http_client_send_touches_network(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.net.http.HttpClient;\n"
            "class A {\n"
            "  void call(Object req, Object handler) throws Exception {\n"
            "    HttpClient client = HttpClient.newHttpClient();\n"
            "    client.send(req, handler);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.call", READS_FROM, "resource::NETWORK::<dynamic>") or _has(
        rels, "A.call", WRITES_TO, "resource::NETWORK::<dynamic>"
    ), rels


def test_java_socket_streams_touch_socket(tmp_path: Path) -> None:
    files = {
        "A.java": (
            "import java.net.Socket;\n"
            "class A {\n"
            "  void talk() throws Exception {\n"
            '    Socket s = new Socket("host", 80);\n'
            "    var in = s.getInputStream();\n"
            "    var out = s.getOutputStream();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.talk", READS_FROM, "resource::SOCKET::host"), rels
    assert _has(rels, "A.talk", WRITES_TO, "resource::SOCKET::host"), rels
