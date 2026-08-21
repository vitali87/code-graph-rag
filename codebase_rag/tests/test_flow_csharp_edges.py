# C# FLOWS_TO taint edges (issue #102 follow-up). C# had READS_FROM/WRITES_TO
# sinks (#825) and resource handles (#826) but no data-flow taint: a value read
# from one resource reaching a write sink emits a resource->resource FLOWS_TO.
# The lean flow walk is descriptor-driven and already ran for C#, but C# wraps
# every call argument in an `argument` node, so the sink-argument taint reader
# and the literal-identity resolver had to unwrap it.
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
    # The read source is inlined as the write sink's argument (no variable).
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
    # A literal argument carries no taint: no FLOWS_TO edge.
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


_ENV_K = "resource::ENV::K"
_STDOUT = "resource::STDOUT::<dynamic>"
_AWAIT_PLUMBING = (
    "using System;\nusing System.Threading.Tasks;\n\n"
    "public class Leaky\n{\n"
    "    private async Task<string> FetchAsync()\n    {\n"
    "        await Task.Delay(1);\n"
    '        return Environment.GetEnvironmentVariable("K");\n    }\n\n'
    "    public async Task Run()\n    {\n"
    "        var token = await FetchAsync().ConfigureAwait(false);\n"
    "        Console.WriteLine(token);\n    }\n}\n"
)


def test_configure_await_preserves_taint(tmp_path: Path) -> None:
    # `ConfigureAwait` returns the SAME value in a different wrapper. Without it
    # in the transparent set the walk stops at the receiver and a very common
    # library-code shape loses its edge entirely (issue #1187).
    flows = _run_flow(tmp_path, {"Program.cs": _AWAIT_PLUMBING})
    assert (_ENV_K, _STDOUT) in flows


def test_await_plumbing_chain_preserves_taint(tmp_path: Path) -> None:
    # The blocking form of the same plumbing: GetAwaiter().GetResult().
    source = _AWAIT_PLUMBING.replace(
        "var token = await FetchAsync().ConfigureAwait(false);",
        "var token = FetchAsync().GetAwaiter().GetResult();",
    ).replace("public async Task Run()", "public void Run()")
    flows = _run_flow(tmp_path, {"Program.cs": source})
    assert (_ENV_K, _STDOUT) in flows


def test_terminal_method_on_a_tainted_receiver_emits_no_flow(tmp_path: Path) -> None:
    # The guard on the other side: a method that does NOT return the receiver's
    # value must not propagate taint, or the transparent set becomes a blanket
    # "any method preserves taint" rule.
    source = (
        "using System;\n\n"
        "public class Fine\n{\n"
        "    public void Run()\n    {\n"
        '        var token = Environment.GetEnvironmentVariable("K");\n'
        "        Console.WriteLine(token.Length);\n    }\n}\n"
    )
    assert (_ENV_K, _STDOUT) not in _run_flow(tmp_path, {"Program.cs": source})


def test_value_task_as_task_preserves_taint(tmp_path: Path) -> None:
    # `AsTask` converts a ValueTask to a Task without changing the value, so it
    # belongs in the transparent set for the same reason as ConfigureAwait.
    source = (
        "using System;\nusing System.Threading.Tasks;\n\n"
        "public class Leaky\n{\n"
        "    private async ValueTask<string> FetchAsync()\n    {\n"
        "        await Task.Delay(1);\n"
        '        return Environment.GetEnvironmentVariable("K");\n    }\n\n'
        "    public async Task Run()\n    {\n"
        "        var token = await FetchAsync().AsTask();\n"
        "        Console.WriteLine(token);\n    }\n}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, {"Program.cs": source})
