# C# I/O: the language had full structural support (issue #102) but ZERO
# flow/IO modelling. First increment: direct sinks for the effective-global
# BCL surface (System.Console, System.Environment, System.IO.File), which are
# never in import_map, so the catalog is not import-gated.
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


def test_csharp_console_writeline_writes_stdout(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            'class A {\n  void Log() {\n    System.Console.WriteLine("hi");\n  }\n}\n'
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Log", WRITES_TO, "resource::STDOUT::<dynamic>"), rels


def test_csharp_get_environment_variable_reads_env(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  string Tmp() {\n"
            '    return System.Environment.GetEnvironmentVariable("SECRET");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Tmp", READS_FROM, "resource::ENV::SECRET"), rels


def test_csharp_file_read_all_text_reads_file(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  string Load() {\n"
            '    return System.IO.File.ReadAllText("cfg.txt");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Load", READS_FROM, "resource::FILE::cfg.txt"), rels


def test_csharp_file_write_all_text_writes_file(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  void Save(string data) {\n"
            '    System.IO.File.WriteAllText("out.txt", data);\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Save", WRITES_TO, "resource::FILE::out.txt"), rels


def test_csharp_console_error_writes_stderr(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "class A {\n"
            "  void Warn() {\n"
            '    System.Console.Error.WriteLine("boom");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Warn", WRITES_TO, "resource::STDERR::<dynamic>"), rels


def test_csharp_using_imported_short_names_resolve(tmp_path: Path) -> None:
    # With `using System;` / `using System.IO;` the calls are written in their
    # short form (Console.X, File.X); both spellings are keyed.
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void M() {\n"
            '    string s = Environment.GetEnvironmentVariable("K");\n'
            '    File.WriteAllText("o.txt", s);\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.M", READS_FROM, "resource::ENV::K"), rels
    assert _has(rels, "A.M", WRITES_TO, "resource::FILE::o.txt"), rels


def test_csharp_reordered_named_args_resolve_correct_path(tmp_path: Path) -> None:
    # C# named args can be reordered, so the target must be matched by name,
    # not by positional index: here `path` is the 2nd argument.
    files = {
        "A.cs": (
            "using System.IO;\n"
            "class A {\n"
            "  void Save(string data) {\n"
            '    File.WriteAllText(contents: data, path: "out.txt");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Save", WRITES_TO, "resource::FILE::out.txt"), rels


def test_csharp_named_target_arg_resolves(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System;\n"
            "class A {\n"
            "  string Get() {\n"
            '    return Environment.GetEnvironmentVariable(variable: "SECRET");\n'
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Get", READS_FROM, "resource::ENV::SECRET"), rels


def test_csharp_console_readline_reads_stdin(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System;\n"
            "class A {\n"
            "  string Ask() {\n"
            "    return Console.ReadLine();\n"
            "  }\n"
            "}\n"
        )
    }
    rels = _run(tmp_path, files)
    assert _has(rels, "A.Ask", READS_FROM, "resource::STDIN::<dynamic>"), rels
