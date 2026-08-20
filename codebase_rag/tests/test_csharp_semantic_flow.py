# Roslyn argument-flow facts, slice 1 of issue #1187. C# has the repo's
# deepest semantic integration but its FLOWS_TO edges came from the lexical
# lean walk. AnalyzeDataFlow answers "which locals reach this argument"
# exactly, so expression shapes the syntactic walker cannot thread (builder
# chains, casts, conditionals) still propagate taint. Additive by design: the
# facts only ever ADD taint the lexical reading missed.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.csharp_frontend.frontend import (
    _arg_flows,
    _out_writes,
    csharp_frontend_available,
)

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_ENV_K = "resource::ENV::K"
_STDOUT = "resource::STDOUT::<dynamic>"

_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "</Project>\n"
)
# The builder chain is the discriminator: the lexical walk cannot thread the
# tainted local through `new StringBuilder().Append(t).ToString()`.
_BUILDER_LEAK = (
    "using System;\nusing System.Text;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var wrapped = new StringBuilder().Append(token).ToString();\n"
    "        Console.WriteLine(wrapped);\n"
    "    }\n}\n"
)


def _flows(
    tmp_path: Path, source: str, mode: cs.CSharpFrontend
) -> set[tuple[str, str]]:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "proj.csproj").write_text(_CSPROJ, encoding="utf-8")
    (repo / "Program.cs").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    previous = settings.CSHARP_FRONTEND
    settings.CSHARP_FRONTEND = mode
    try:
        mock = MagicMock()
        GraphUpdater(
            ingestor=mock,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            capture=_CAPTURE_IO,
        ).run()
    finally:
        settings.CSHARP_FRONTEND = previous
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


def test_arg_flow_rows_parse_and_malformed_rows_drop() -> None:
    parsed = _arg_flows(
        [
            {
                "file": "a.cs",
                "line": 4,
                "col": 8,
                "name": "WriteLine",
                "index": 0,
                "symbols": ["token", 5, "other"],
            },
            {"file": "a.cs", "line": 9, "name": "Broken"},
            {
                "file": "a.cs",
                "line": 4,
                "col": 8,
                "name": "WriteLine",
                "index": 1,
                "symbols": [],
            },
        ]
    )
    assert parsed == {("a.cs", 4, 8, "WriteLine"): {0: frozenset({"token", "other"})}}


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_builder_chain_taint_needs_the_semantic_facts(tmp_path: Path) -> None:
    lexical = _flows(tmp_path / "a", _BUILDER_LEAK, cs.CSharpFrontend.TREESITTER)
    assert (_ENV_K, _STDOUT) not in lexical
    semantic = _flows(tmp_path / "b", _BUILDER_LEAK, cs.CSharpFrontend.HYBRID)
    assert (_ENV_K, _STDOUT) in semantic


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_untainted_local_never_gains_a_flow(tmp_path: Path) -> None:
    clean = (
        "using System;\nusing System.Text;\n\n"
        "public class Fine\n{\n"
        "    public void Run()\n    {\n"
        '        var plain = "constant";\n'
        "        var wrapped = new StringBuilder().Append(plain).ToString();\n"
        "        Console.WriteLine(wrapped);\n"
        "    }\n}\n"
    )
    assert _flows(tmp_path / "c", clean, cs.CSharpFrontend.HYBRID) == set()


def test_callee_name_token_matches_every_roslyn_shape() -> None:
    # The Python key must mirror the tool's CalleeNameToken arm for arm, or
    # these call sites silently lose their facts (review on #1338).
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.flow_access.processor import FlowProcessor

    parsers, _queries = load_parsers()
    parser = parsers[cs.SupportedLanguage.CSHARP]
    tree = parser.parse(
        b"class A { void M(C c, string t) { c?.Handle(t); Gen<int>(t);"
        b" c.Obj.Do<T>(t); Plain(t); } }"
    )
    names: list[str] = []

    def walk(node) -> None:
        if node.type == "invocation_expression":
            token = FlowProcessor._csharp_callee_name_node(node)
            assert token is not None
            names.append(token.text.decode("utf-8"))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    assert names == ["Handle", "Gen", "Do", "Plain"]


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_inspected_local_never_fabricates_a_flow(tmp_path: Path) -> None:
    # A tainted local merely INSPECTED contributes no value to the result:
    # DataFlowsIn excludes it, so no edge is fabricated (review on #1338).
    source = (
        "using System;\n\n"
        "public class Inspect\n{\n"
        "    public void Run()\n    {\n"
        '        var token = Environment.GetEnvironmentVariable("K");\n'
        '        var verdict = token == null ? "missing" : "present";\n'
        "        Console.WriteLine(verdict);\n"
        "    }\n}\n"
    )
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "d", source, cs.CSharpFrontend.HYBRID
    )


# An `out` argument is written BY the callee, so the lexical walk sees the
# variable declared and never assigned: `parsed` looks clean and the sink it
# feeds has no edge (slice 2 of issue #1187).
_OUT_PARAM_LEAK = (
    "using System;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        if (int.TryParse(token, out var parsed))\n        {\n"
    "            Console.WriteLine(parsed);\n        }\n"
    "    }\n}\n"
)
_FIRST_PARTY_OUT_LEAK = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public bool TryCopy(string src, out string dst)\n    {\n"
    "        dst = src;\n        return true;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    "        if (helper.TryCopy(token, out var copied))\n        {\n"
    "            Console.WriteLine(copied);\n        }\n"
    "    }\n}\n"
)
_REF_LEAK = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


def test_out_write_rows_parse_and_malformed_rows_drop() -> None:
    parsed = _out_writes(
        [
            {
                "file": "a.cs",
                "line": 7,
                "col": 12,
                "name": "TryParse",
                "index": 1,
                "symbol": "parsed",
            },
            {"file": "a.cs", "line": 9, "name": "Broken"},
            {
                "file": "a.cs",
                "line": 7,
                "col": 12,
                "name": "TryParse",
                "index": 2,
                "symbol": 5,
            },
        ]
    )
    assert parsed == {("a.cs", 7, 12, "TryParse"): {1: "parsed"}}


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_out_parameter_write_back_needs_the_semantic_facts(tmp_path: Path) -> None:
    # int.TryParse is EXTERNAL and still writes its out parameter, so the fact
    # has to be emitted for external callees too.
    lexical = _flows(tmp_path / "o1", _OUT_PARAM_LEAK, cs.CSharpFrontend.TREESITTER)
    assert (_ENV_K, _STDOUT) not in lexical
    semantic = _flows(tmp_path / "o2", _OUT_PARAM_LEAK, cs.CSharpFrontend.HYBRID)
    assert (_ENV_K, _STDOUT) in semantic


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_first_party_out_parameter_write_back(tmp_path: Path) -> None:
    lexical = _flows(
        tmp_path / "f1", _FIRST_PARTY_OUT_LEAK, cs.CSharpFrontend.TREESITTER
    )
    assert (_ENV_K, _STDOUT) not in lexical
    semantic = _flows(tmp_path / "f2", _FIRST_PARTY_OUT_LEAK, cs.CSharpFrontend.HYBRID)
    assert (_ENV_K, _STDOUT) in semantic


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_parameter_write_back(tmp_path: Path) -> None:
    semantic = _flows(tmp_path / "r1", _REF_LEAK, cs.CSharpFrontend.HYBRID)
    assert (_ENV_K, _STDOUT) in semantic


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_clean_out_parameter_never_gains_a_flow(tmp_path: Path) -> None:
    clean = (
        "using System;\n\n"
        "public class Fine\n{\n"
        "    public void Run()\n    {\n"
        '        if (int.TryParse("42", out var parsed))\n        {\n'
        "            Console.WriteLine(parsed);\n        }\n"
        "    }\n}\n"
    )
    assert _flows(tmp_path / "c2", clean, cs.CSharpFrontend.HYBRID) == set()


# A `ref` parameter the callee never assigns: `ref` PERMITS a write, it does
# not promise one, so treating every ref argument as written would invent an
# ENV -> STDOUT flow through a variable the helper only reads.
_READ_ONLY_REF = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public void Inspect(string src, ref string other)\n    {\n"
    "        Console.Error.WriteLine(src.Length + other.Length);\n    }\n}\n\n"
    "public class Fine\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Inspect(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_read_only_ref_parameter_never_gains_a_flow(tmp_path: Path) -> None:
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "r2", _READ_ONLY_REF, cs.CSharpFrontend.HYBRID
    )


_EXPRESSION_BODIED_REF = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public void Fill(string src, ref string dst) => dst = src;\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)
_NAMED_REF_ARGUMENTS = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(dst: ref sink, src: token);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_expression_bodied_ref_callee_still_writes_back(tmp_path: Path) -> None:
    # `=> dst = src` has no block body; rejecting it would treat a writing
    # callee as read-only and silently drop the edge.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "e1", _EXPRESSION_BODIED_REF, cs.CSharpFrontend.HYBRID
    )


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_named_ref_argument_binds_to_its_own_parameter(tmp_path: Path) -> None:
    # Named arguments are reordered: `dst` sits at source index 0 while its
    # parameter is ordinal 1, so an ordinal lookup would analyse `src`.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "n1", _NAMED_REF_ARGUMENTS, cs.CSharpFrontend.HYBRID
    )


_PARTIAL_REF = (
    "using System;\n\n"
    "public partial class Helper\n{\n"
    "    public partial void Fill(string src, ref string dst);\n}\n\n"
    "public partial class Helper\n{\n"
    "    public partial void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_partial_method_ref_callee_resolves_to_the_implementing_part(
    tmp_path: Path,
) -> None:
    # The invoked symbol can be the DEFINING part, which has no body; without
    # normalizing to the implementation the write looks unprovable and a real
    # flow disappears.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "p1", _PARTIAL_REF, cs.CSharpFrontend.HYBRID
    )
