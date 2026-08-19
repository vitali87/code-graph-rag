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


def test_scala_triple_quoted_path_strips_delimiters(tmp_path: Path) -> None:
    files = {
        "Raw.scala": (
            "object Raw {\n"
            "  def load(): String = {\n"
            '    scala.io.Source.fromFile("""/etc/app.conf""").mkString\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "Raw.load", READS_FROM, "resource::FILE::/etc/app.conf")


def test_scala_local_objects_shadow_stdlib_heads(tmp_path: Path) -> None:
    # A same-file `object Source`/`object StdIn` (declared OUTSIDE the caller,
    # at module level) is not the scala.io API: calls through it must emit
    # nothing (review on #1256's PR).
    files = {
        "Own.scala": (
            "object Source { def fromFile(p: String): String = p }\n"
            'object StdIn { def readLine(): String = "x" }\n'
            "\n"
            "object App {\n"
            "  def use(): Unit = {\n"
            '    Source.fromFile("/local/thing")\n'
            "    StdIn.readLine()\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert not any(a.partition("(")[0].endswith("App.use") for a, _r, _b in rels)


def test_scala_print_writer_handle_write(tmp_path: Path) -> None:
    # JVM interop handles: `new java.io.PrintWriter(path)` binds a FILE write
    # handle through the Java constructor table; the method write lands on
    # the constructor's literal path.
    files = {
        "W.scala": (
            "object W {\n"
            "  def dump(data: String): Unit = {\n"
            '    val w = new java.io.PrintWriter("out.txt")\n'
            "    w.write(data)\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "W.dump", WRITES_TO, "resource::FILE::out.txt")


def test_scala_buffered_writer_wrapper_resolves_inner_file(tmp_path: Path) -> None:
    files = {
        "B.scala": (
            "object B {\n"
            "  def log(line: String): Unit = {\n"
            "    val out = new java.io.BufferedWriter(\n"
            '      new java.io.FileWriter("app.log")\n'
            "    )\n"
            "    out.write(line)\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "B.log", WRITES_TO, "resource::FILE::app.log")


def test_scala_file_identity_carrier_in_constructor(tmp_path: Path) -> None:
    # `new FileWriter(new File("out.txt"))`: the inner File is not a handle,
    # but it designates the resource, so the writer binds its literal.
    files = {
        "F.scala": (
            "object F {\n"
            "  def save(text: String): Unit = {\n"
            '    val w = new java.io.FileWriter(new java.io.File("out.txt"))\n'
            "    w.write(text)\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "F.save", WRITES_TO, "resource::FILE::out.txt")
