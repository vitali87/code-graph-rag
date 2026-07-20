# (H) C# I/O handles (issue #102 follow-up, second increment). The first C# I/O
# (H) increment covered direct BCL sinks (Console/Environment/File); this adds
# (H) `new`-shaped resource handles whose later method calls are attributed to the
# (H) same resource: StreamReader/StreamWriter/FileStream (FILE), HttpClient
# (H) (NETWORK) and ADO.NET SqlConnection/SqlCommand (DATABASE, incl. the
# (H) `conn.CreateCommand()` derive). C# binds a declarator's initializer as the
# (H) last UNFIELDED named child (no `value` field), so the binding walk needed
# (H) that plumbing.
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


def test_csharp_streamreader_handle_reads_file(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System.IO;\n"
            "class A {\n"
            "  string Load() {\n"
            '    var r = new StreamReader("cfg.txt");\n'
            "    return r.ReadToEnd();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Load", READS_FROM, "resource::FILE::cfg.txt"), rels


def test_csharp_streamwriter_handle_writes_file(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System.IO;\n"
            "class A {\n"
            "  void Save(string data) {\n"
            '    var w = new StreamWriter("out.txt");\n'
            "    w.WriteLine(data);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Save", WRITES_TO, "resource::FILE::out.txt"), rels


def test_csharp_streamreader_fully_qualified_resolves(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  string Load() {\n"
            '    var r = new System.IO.StreamReader("cfg.txt");\n'
            "    return r.ReadLine();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Load", READS_FROM, "resource::FILE::cfg.txt"), rels


def test_csharp_httpclient_get_reads_network(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System.Net.Http;\n"
            "class A {\n"
            "  async System.Threading.Tasks.Task Fetch(string url) {\n"
            "    var c = new HttpClient();\n"
            "    await c.GetStringAsync(url);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Fetch", READS_FROM, "resource::NETWORK::<dynamic>"), rels


def test_csharp_httpclient_post_writes_network(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System.Net.Http;\n"
            "class A {\n"
            "  async System.Threading.Tasks.Task Send(string url, HttpContent body) {\n"
            "    var c = new HttpClient();\n"
            "    await c.PostAsync(url, body);\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Send", WRITES_TO, "resource::NETWORK::<dynamic>"), rels


def test_csharp_sqlconnection_command_reads_database(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using Microsoft.Data.SqlClient;\n"
            "class A {\n"
            "  void Query() {\n"
            '    var conn = new SqlConnection("Server=.;Database=db");\n'
            "    var cmd = conn.CreateCommand();\n"
            "    cmd.ExecuteReader();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(
        rels, "A.Query", READS_FROM, "resource::DATABASE::Server=.;Database=db"
    ), rels


def test_csharp_sqlcommand_execute_non_query_writes_database(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using Microsoft.Data.SqlClient;\n"
            "class A {\n"
            "  void Update() {\n"
            '    var conn = new SqlConnection("Server=.;Database=db");\n'
            "    var cmd = conn.CreateCommand();\n"
            "    cmd.ExecuteNonQuery();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(
        rels, "A.Update", WRITES_TO, "resource::DATABASE::Server=.;Database=db"
    ), rels


def test_csharp_new_sqlcommand_reads_database(tmp_path: Path) -> None:
    # (H) A command constructed directly (`new SqlCommand(sql, conn)`) is a DATABASE
    # (H) handle; its resource identity comes from the connection (arg1), not the SQL
    # (H) text, so it is <dynamic> here.
    files = {
        "A.cs": (
            "using Microsoft.Data.SqlClient;\n"
            "class A {\n"
            "  void Query(SqlConnection conn) {\n"
            '    var cmd = new SqlCommand("SELECT 1", conn);\n'
            "    cmd.ExecuteReader();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Query", READS_FROM, "resource::DATABASE::<dynamic>"), rels
