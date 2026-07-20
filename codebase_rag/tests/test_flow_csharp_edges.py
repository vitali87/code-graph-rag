# (H) C# FLOWS_TO taint edges (issue #102 follow-up). C# had READS_FROM/WRITES_TO
# (H) sinks (#825) and resource handles (#826) but no data-flow taint: a value read
# (H) from one resource reaching a write sink emits a resource->resource FLOWS_TO.
# (H) The lean flow walk is descriptor-driven and already ran for C#, but C# wraps
# (H) every call argument in an `argument` node, so the sink-argument taint reader
# (H) and the literal-identity resolver had to unwrap it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
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
        if str(c.args[1]) == FLOWS_TO
    }


def test_csharp_env_flows_to_file_via_variable(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void Leak() {\n"
            '    string s = Environment.GetEnvironmentVariable("SECRET");\n'
            '    File.WriteAllText("out.txt", s);\n'
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows, flows


def test_csharp_env_flows_to_file_inline(tmp_path: Path) -> None:
    # (H) The read source is inlined as the write sink's argument (no variable).
    files = {
        "A.cs": (
            "using System;\n"
            "using System.IO;\n"
            "class A {\n"
            "  void Leak() {\n"
            '    File.WriteAllText("out.txt", '
            'Environment.GetEnvironmentVariable("SECRET"));\n'
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::SECRET", "resource::FILE::out.txt") in flows, flows


def test_csharp_env_flows_to_stdout(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System;\n"
            "class A {\n"
            "  void Log() {\n"
            '    string k = Environment.GetEnvironmentVariable("KEY");\n'
            "    Console.WriteLine(k);\n"
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::ENV::KEY", "resource::STDOUT::<dynamic>") in flows, flows


def test_csharp_file_read_flows_to_file_write(tmp_path: Path) -> None:
    files = {
        "A.cs": (
            "using System.IO;\n"
            "class A {\n"
            "  void Copy() {\n"
            '    string data = File.ReadAllText("in.txt");\n'
            '    File.WriteAllText("out.txt", data);\n'
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert ("resource::FILE::in.txt", "resource::FILE::out.txt") in flows, flows


def test_csharp_untainted_value_emits_no_flow(tmp_path: Path) -> None:
    # (H) A literal argument carries no taint: no FLOWS_TO edge.
    files = {
        "A.cs": (
            "using System.IO;\n"
            "class A {\n"
            "  void Save() {\n"
            '    File.WriteAllText("out.txt", "constant");\n'
            "  }\n"
            "}\n"
        )
    }
    flows = _run_flow(tmp_path, files)
    assert flows == set(), flows
