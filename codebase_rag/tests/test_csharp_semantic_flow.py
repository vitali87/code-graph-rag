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
from codebase_rag.parsers.csharp_frontend import run_csharp_frontend
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


_EXTENSION_REF = (
    "using System;\n\n"
    "public static class Extensions\n{\n"
    "    public static void Fill(this string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    '        var sink = "";\n'
    "        token.Fill(ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_receiver_style_extension_ref_argument_writes_back(tmp_path: Path) -> None:
    # Receiver style hides a parameter: `ref sink` is source index 0 on the
    # REDUCED symbol, but ordinal 1 on ReducedFrom, whose ordinal 0 is the
    # receiver. Binding after normalizing analyses the wrong parameter. The
    # taint source is the receiver too, which is not an argument at all.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "x1", _EXTENSION_REF, cs.CSharpFrontend.HYBRID
    )


_GENERIC_REF = (
    "using System;\n\n"
    "public class Helper\n{\n"
    "    public void Fill<T>(T src, ref T dst)\n    {\n"
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
def test_generic_ref_callee_writes_back(tmp_path: Path) -> None:
    # A CONSTRUCTED generic method has substituted parameter symbols, while
    # WrittenInside holds the declaration's originals, so an unnormalized
    # comparison silently reports no write.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "g1", _GENERIC_REF, cs.CSharpFrontend.HYBRID
    )


_LOCAL_FUNCTION_REF = (
    "using System;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    '        var sink = "";\n'
    "        void Fill(string src, ref string dst)\n        {\n"
    "            dst = src;\n        }\n"
    "        Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_local_function_ref_callee_writes_back(tmp_path: Path) -> None:
    # CallableBody handles LocalFunctionStatementSyntax, but nothing exercised
    # it until now (issue #1353).
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "lf1", _LOCAL_FUNCTION_REF, cs.CSharpFrontend.HYBRID
    )


_LIB_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "</Project>\n"
)
_APP_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "  <ItemGroup>\n"
    '    <ProjectReference Include="../Lib/Lib.csproj" />\n'
    "  </ItemGroup>\n"
    "</Project>\n"
)
_LIB_HELPER = (
    "namespace Lib;\n\n"
    "public class Helper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n"
)
_APP_PROGRAM = (
    "using System;\nusing Lib;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        var helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


def _write_cross_project_repo(repo: Path) -> None:
    (repo / "Lib").mkdir(parents=True)
    (repo / "App").mkdir(parents=True)
    (repo / "Lib" / "Lib.csproj").write_text(_LIB_CSPROJ, encoding="utf-8")
    (repo / "Lib" / "Helper.cs").write_text(_LIB_HELPER, encoding="utf-8")
    (repo / "App" / "App.csproj").write_text(_APP_CSPROJ, encoding="utf-8")
    (repo / "App" / "Program.cs").write_text(_APP_PROGRAM, encoding="utf-8")


def _cross_project_flows(repo: Path) -> set[tuple[str, str]]:
    _write_cross_project_repo(repo)
    parsers, queries = load_parsers()
    previous = settings.CSHARP_FRONTEND
    settings.CSHARP_FRONTEND = cs.CSharpFrontend.HYBRID
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


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_write_back_across_a_project_reference(tmp_path: Path) -> None:
    # The callee body lives in a REFERENCED project, so it belongs to another
    # compilation; asking the caller's for a model of that tree throws, and the
    # write was previously unprovable and silently dropped (issue #1353).
    assert (_ENV_K, _STDOUT) in _cross_project_flows(tmp_path / "sln")


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_cross_project_ref_emits_the_write_fact_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sibling test asserts the downstream EDGE, which could in principle
    # survive for an unrelated reason. This one pins the fact the frontend
    # actually produces: argument index 1 of `Fill` writes `sink`.
    repo = tmp_path / "facts"
    _write_cross_project_repo(repo)
    monkeypatch.setattr(settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    facts = run_csharp_frontend(repo)
    writes = {key[3]: value for key, value in facts.out_writes.items()}
    assert writes.get("Fill") == {1: "sink"}


# The block-bodied local function is already covered above. These two add the
# shapes it does not reach: the EXPRESSION-bodied local function, and the
# read-only negative that keeps the branch from writing back unconditionally
# (issue #1353 item 1).
_LOCAL_FUNCTION_EXPRESSION_REF = (
    "using System;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    "        void Fill(string src, ref string dst) => dst = src;\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    '        var sink = "";\n'
    "        Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)
_LOCAL_FUNCTION_READ_ONLY_REF = (
    "using System;\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    "        void Inspect(string src, ref string other)\n        {\n"
    "            Console.Error.WriteLine(src.Length + other.Length);\n        }\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    '        var sink = "";\n'
    "        Inspect(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_expression_bodied_local_function_ref_callee_writes_back(
    tmp_path: Path,
) -> None:
    # `CallableBody` reaches for `local.ExpressionBody?.Expression` on this
    # shape; nothing exercised that fallback before.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "lf2", _LOCAL_FUNCTION_EXPRESSION_REF, cs.CSharpFrontend.HYBRID
    )


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_read_only_ref_local_function_never_gains_a_flow(tmp_path: Path) -> None:
    # The local-function branch must keep the same discipline as the method one:
    # a `ref` parameter that is only READ does not write back, so treating every
    # ref argument as written would invent an edge. The callee reports to STDERR
    # deliberately: writing to STDOUT there would leak the token through the
    # callee's OWN sink and the test would pass or fail for the wrong reason.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "lf3", _LOCAL_FUNCTION_READ_ONLY_REF, cs.CSharpFrontend.HYBRID
    )


# Interface dispatch hides the writer: GetSymbolInfo resolves the call to the
# INTERFACE member, whose declaration has no body, so the write-back proof
# answered false and the flow was silently dropped (issue #1356). With exactly
# one implementing type in the loaded compilations, its body IS the callee,
# the same sole-implementer policy the Go frontend applies to interface edges.
_INTERFACE_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Helper : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_write_back_through_sole_interface_implementer(tmp_path: Path) -> None:
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "if1", _INTERFACE_REF, cs.CSharpFrontend.HYBRID
    )


_INTERFACE_READ_ONLY_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Inspect(string src, ref string other);\n}\n\n"
    "public class Helper : IHelper\n{\n"
    "    public void Inspect(string src, ref string other)\n    {\n"
    "        Console.Error.WriteLine(other.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Inspect(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_read_only_ref_through_an_interface_never_gains_a_flow(
    tmp_path: Path,
) -> None:
    # Resolving the sole implementer must not weaken the no-fabrication rule:
    # the implementation only READS the ref parameter, so no edge.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "if2", _INTERFACE_READ_ONLY_REF, cs.CSharpFrontend.HYBRID
    )


_INTERFACE_TWO_IMPLEMENTERS_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Writer : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Reader : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Reader();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_through_an_interface_with_two_implementers_stays_unproven(
    tmp_path: Path,
) -> None:
    # With two implementers the writer is not statically known, and one of
    # them only reads: the write is proven only when EVERY candidate body
    # writes, so assuming the writing one would fabricate a flow when the
    # reached implementation only reads, exactly the failure the proof
    # exists to prevent.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "if3", _INTERFACE_TWO_IMPLEMENTERS_REF, cs.CSharpFrontend.HYBRID
    )


_ABSTRACT_REF = (
    "using System;\n\n"
    "public abstract class HelperBase\n{\n"
    "    public abstract void Fill(string src, ref string dst);\n}\n\n"
    "public class Helper : HelperBase\n{\n"
    "    public override void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        HelperBase helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_write_back_through_sole_abstract_override(tmp_path: Path) -> None:
    # An abstract member has the same defect shape as an interface member: the
    # invoked symbol's declaration carries no body (issue #1356).
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "ab1", _ABSTRACT_REF, cs.CSharpFrontend.HYBRID
    )


_DEFAULT_INTERFACE_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Writer : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Reader : IHelper\n{\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Reader();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_default_interface_method_never_resolves_to_an_override(
    tmp_path: Path,
) -> None:
    # A DEFAULT interface member owns a body and is not abstract, so the call
    # can land on the inherited default (Reader here). Resolving a sole
    # override (Writer) would attribute the write to a body the call never
    # reaches; the default body itself only reads, so no edge.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "di1", _DEFAULT_INTERFACE_REF, cs.CSharpFrontend.HYBRID
    )


_DEFAULT_INTERFACE_DEAD_DEFAULT_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Helper : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_dead_default_body_does_not_hide_the_sole_override(tmp_path: Path) -> None:
    # Every implementing type overrides the default, so the default body can
    # never execute; the sole override is the only reachable callee and its
    # write must not be masked by the unreachable read-only default.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "di2", _DEFAULT_INTERFACE_DEAD_DEFAULT_REF, cs.CSharpFrontend.HYBRID
    )


_METADATA_IFACE_SOURCE = (
    "public interface ILib\n{\n    void Fill(string src, ref string dst);\n}\n"
)
_METADATA_APP_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "  <ItemGroup>\n"
    '    <Reference Include="MetaLib"><HintPath>{dll}</HintPath></Reference>\n'
    "  </ItemGroup>\n"
    "</Project>\n"
)
_METADATA_APP_PROGRAM = (
    "using System;\n\n"
    "public class Writer : ILib\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        ILib helper = new Writer();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    '        var token2 = Environment.GetEnvironmentVariable("K2");\n'
    "        var direct = new Writer();\n"
    '        var sink2 = "";\n'
    "        direct.Fill(token2, ref sink2);\n"
    "        Console.WriteLine(sink2);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_metadata_interface_never_resolves_to_source_implementers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A contract living in a PREBUILT assembly can have implementers in other
    # referenced assemblies that source scanning cannot see, so the source
    # implementation set is never complete and proving a write from it could
    # fabricate a flow for a runtime receiver from metadata. Only contracts
    # declared in loaded source resolve.
    import subprocess

    lib = tmp_path / "libsrc"
    lib.mkdir(parents=True)
    (lib / "MetaLib.csproj").write_text(_LIB_CSPROJ, encoding="utf-8")
    (lib / "ILib.cs").write_text(_METADATA_IFACE_SOURCE, encoding="utf-8")
    out = tmp_path / "libbin"
    built = subprocess.run(
        ["dotnet", "build", str(lib), "-o", str(out), "--nologo", "-v", "q"],
        capture_output=True,
        text=True,
        check=False,
    )
    dll = out / "MetaLib.dll"
    if built.returncode != 0 or not dll.exists():
        pytest.skip(f"could not prebuild the metadata assembly: {built.stderr}")

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "proj.csproj").write_text(
        _METADATA_APP_CSPROJ.format(dll=dll), encoding="utf-8"
    )
    (repo / "Program.cs").write_text(_METADATA_APP_PROGRAM, encoding="utf-8")
    parsers, queries = load_parsers()
    monkeypatch.setattr(settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    flows = {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }
    # Positive control first: the DIRECT call's write-back must be present,
    # proving the Roslyn frontend actually built and emitted ref facts, so
    # the absence below cannot pass vacuously on a broken build.
    assert ("resource::ENV::K2", _STDOUT) in flows
    assert (_ENV_K, _STDOUT) not in flows


_DERIVED_DEFAULT_READS_REF = (
    "using System;\n\n"
    "public interface IBase\n{\n"
    "    void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public interface IDerived : IBase\n{\n"
    "    void IBase.Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Impl : IDerived\n{\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IBase helper = new Impl();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_reading_derived_interface_default_blocks_the_base_default_write(
    tmp_path: Path,
) -> None:
    # The implementer receives the DERIVED interface's explicit default, which
    # only reads; the writing base default never executes for it, so proving
    # the write from the base body alone would fabricate the flow.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "dd1", _DERIVED_DEFAULT_READS_REF, cs.CSharpFrontend.HYBRID
    )


_DERIVED_DEFAULT_WRITES_REF = (
    "using System;\n\n"
    "public interface IBase\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public interface IDerived : IBase\n{\n"
    "    void IBase.Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Impl : IDerived\n{\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IBase helper = new Impl();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_writing_derived_interface_default_proves_the_base_member_write(
    tmp_path: Path,
) -> None:
    # The abstract base member's only reachable body is the derived
    # interface's explicit default, and it writes.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "dd2", _DERIVED_DEFAULT_WRITES_REF, cs.CSharpFrontend.HYBRID
    )
