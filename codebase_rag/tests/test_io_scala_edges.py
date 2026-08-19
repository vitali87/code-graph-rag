# Scala direct-call I/O sinks (issue #1256): the last language with zero
# READS_FROM/WRITES_TO coverage. The lean walk applies unchanged (Scala calls
# are call_expression nodes with a dotted `function` text); the catalog covers
# Predef console output, scala.io Source/StdIn, the sys.env/sys.props apply
# calls, and the java.lang/java.nio interop surface.
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


def test_scala_console_output_writes_std_streams(tmp_path: Path) -> None:
    files = {
        "App.scala": (
            "object App {\n"
            "  def run(): Unit = {\n"
            '    println("hello")\n'
            '    Console.err.println("bad")\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "App.run", WRITES_TO, "resource::STDOUT::<dynamic>")
    assert _has(rels, "App.run", WRITES_TO, "resource::STDERR::<dynamic>")


def test_scala_source_from_file_reads_literal_path(tmp_path: Path) -> None:
    # The literal path resolves through the childless Scala `string` node,
    # which carries its content only in node.text.
    files = {
        "Cfg.scala": (
            "object Cfg {\n"
            "  def load(): String = {\n"
            '    scala.io.Source.fromFile("/etc/app.conf").mkString\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "Cfg.load", READS_FROM, "resource::FILE::/etc/app.conf")


def test_scala_env_reads_system_and_sys_env(tmp_path: Path) -> None:
    files = {
        "Env.scala": (
            "object Env {\n"
            "  def read(): Unit = {\n"
            '    val home = System.getenv("HOME")\n'
            '    val path = sys.env("PATH")\n'
            '    System.setProperty("app.mode", "prod")\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "Env.read", READS_FROM, "resource::ENV::HOME")
    assert _has(rels, "Env.read", READS_FROM, "resource::ENV::PATH")
    assert _has(rels, "Env.read", WRITES_TO, "resource::ENV::app.mode")


def test_scala_files_write_and_source_from_url(tmp_path: Path) -> None:
    files = {
        "Net.scala": (
            "object Net {\n"
            "  def sync(): Unit = {\n"
            '    val page = scala.io.Source.fromURL("https://example.com/api")\n'
            '    Files.writeString(Paths.get("out.txt"), page.mkString)\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(
        rels, "Net.sync", READS_FROM, "resource::NETWORK::https://example.com/api"
    )
    # The target path is a nested Paths.get(...) call, not a literal.
    assert _has(rels, "Net.sync", WRITES_TO, "resource::FILE::<dynamic>")


def test_scala_local_val_shadows_a_sink_name(tmp_path: Path) -> None:
    files = {
        "Shadow.scala": (
            "object Shadow {\n"
            "  def quiet(): Unit = {\n"
            "    val println = (s: String) => ()\n"
            '    println("not io")\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert not any(a.partition("(")[0].endswith("Shadow.quiet") for a, _r, _b in rels)


def test_scala_stdin_read(tmp_path: Path) -> None:
    files = {
        "In.scala": (
            "object In {\n"
            "  def ask(): String = {\n"
            "    scala.io.StdIn.readLine()\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "In.ask", READS_FROM, "resource::STDIN::<dynamic>")
