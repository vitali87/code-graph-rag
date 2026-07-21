# Error-stream writes must classify as STDERR, not STDOUT: Java System.err.*,
# JS/TS console.error/console.warn (Node aliases warn to error, both write
# stderr), C++ cerr/clog stream inserts, and Rust eprintln!/eprint! macros.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

WRITES_TO = cs.RelationshipType.WRITES_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

STDERR_DYNAMIC = "resource::STDERR::<dynamic>"
STDOUT_DYNAMIC = "resource::STDOUT::<dynamic>"


def _writes(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
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
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == WRITES_TO
    }


def _has(writes: set[tuple[str, str]], caller: str, resource: str) -> bool:
    return any(a.endswith(caller) and b == resource for a, b in writes)


def test_java_system_err_writes_stderr(tmp_path: Path) -> None:
    files = {
        "Main.java": (
            "public class Main {\n"
            "    void log(String msg) {\n"
            "        System.err.println(msg);\n"
            "    }\n"
            "}\n"
        )
    }
    writes = _writes(tmp_path, files)
    assert _has(writes, "Main.log(String)", STDERR_DYNAMIC), writes
    assert not _has(writes, "Main.log(String)", STDOUT_DYNAMIC), writes


def test_java_system_out_still_writes_stdout(tmp_path: Path) -> None:
    files = {
        "Main.java": (
            "public class Main {\n"
            "    void say(String msg) {\n"
            "        System.out.println(msg);\n"
            "    }\n"
            "}\n"
        )
    }
    writes = _writes(tmp_path, files)
    assert _has(writes, "Main.say(String)", STDOUT_DYNAMIC), writes


def test_js_console_error_and_warn_write_stderr(tmp_path: Path) -> None:
    files = {
        "app.js": (
            "function report(msg) {\n"
            "    console.error(msg);\n"
            "    console.warn(msg);\n"
            "}\n"
        )
    }
    writes = _writes(tmp_path, files)
    assert _has(writes, "app.report", STDERR_DYNAMIC), writes
    assert not _has(writes, "app.report", STDOUT_DYNAMIC), writes


def test_cpp_cerr_and_clog_write_stderr(tmp_path: Path) -> None:
    files = {
        "main.cpp": (
            "#include <iostream>\n"
            "void report(int x) {\n"
            "    std::cerr << x;\n"
            "    std::clog << x;\n"
            "}\n"
        )
    }
    writes = _writes(tmp_path, files)
    assert _has(writes, "main.report", STDERR_DYNAMIC), writes
    assert not _has(writes, "main.report", STDOUT_DYNAMIC), writes


def test_cpp_cout_still_writes_stdout(tmp_path: Path) -> None:
    files = {
        "main.cpp": ("#include <iostream>\nvoid say(int x) {\n    std::cout << x;\n}\n")
    }
    writes = _writes(tmp_path, files)
    assert _has(writes, "main.say", STDOUT_DYNAMIC), writes


def test_rust_eprintln_writes_stderr(tmp_path: Path) -> None:
    files = {"main.rs": ('fn report(x: i32) {\n    eprintln!("{}", x);\n}\n')}
    writes = _writes(tmp_path, files)
    assert _has(writes, "main.report", STDERR_DYNAMIC), writes
    assert not _has(writes, "main.report", STDOUT_DYNAMIC), writes


def test_rust_println_still_writes_stdout(tmp_path: Path) -> None:
    files = {"main.rs": ('fn say(x: i32) {\n    println!("{}", x);\n}\n')}
    writes = _writes(tmp_path, files)
    assert _has(writes, "main.say", STDOUT_DYNAMIC), writes
