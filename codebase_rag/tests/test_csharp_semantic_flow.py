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


_INTERFACE_TWO_WRITERS_REF = (
    "using System;\n\n"
    "public interface IHelper\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class WriterA : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class WriterB : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    '        dst = src + "!";\n    }\n}\n\n'
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new WriterB();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_ref_through_an_interface_where_every_implementer_writes(
    tmp_path: Path,
) -> None:
    # The must-agreement policy decided on #1356: with several implementers
    # the edge exists exactly when ALL of them write, because whichever body
    # actually runs then performs the write and nothing is fabricated.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "if4", _INTERFACE_TWO_WRITERS_REF, cs.CSharpFrontend.HYBRID
    )


_LINKED_IFACE_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "</Project>\n"
)
_LINKED_IMPL_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup>\n"
    "    <TargetFramework>net8.0</TargetFramework>\n"
    "    <DefineConstants>{defines}</DefineConstants>\n"
    "  </PropertyGroup>\n"
    "  <ItemGroup>\n"
    '    <ProjectReference Include="../Iface/Iface.csproj" />\n'
    '    <Compile Include="../Shared/Helper.cs" />\n'
    "  </ItemGroup>\n"
    "</Project>\n"
)
_LINKED_IFACE = (
    "public interface IHelper\n{\n    void Fill(string src, ref string dst);\n}\n"
)
_LINKED_HELPER = (
    "using System;\n\n"
    "public class Helper : IHelper\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "#if WRITES\n"
    "        dst = src;\n"
    "#else\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n"
    "#endif\n"
    "    }\n}\n"
)
_LINKED_LEAKY = (
    "using System;\n\n"
    "public class LocalWriter\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper helper = new Helper();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    '        var token2 = Environment.GetEnvironmentVariable("K2");\n'
    "        var direct = new LocalWriter();\n"
    '        var sink2 = "";\n'
    "        direct.Fill(token2, ref sink2);\n"
    "        Console.WriteLine(sink2);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_conditional_variants_of_a_shared_file_are_not_collapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same source file linked into two projects with different
    # DefineConstants yields candidates that share their file and span while
    # their bodies differ: one compiles the writing branch, the other the
    # read-only one. Both must survive into the must-agreement conjunction,
    # so the disagreement keeps the write unproven.
    repo = tmp_path / "repo"
    (repo / "Iface").mkdir(parents=True)
    (repo / "Shared").mkdir()
    (repo / "ImplA").mkdir()
    (repo / "ImplB").mkdir()
    (repo / "Iface" / "Iface.csproj").write_text(_LINKED_IFACE_CSPROJ, encoding="utf-8")
    (repo / "Iface" / "IHelper.cs").write_text(_LINKED_IFACE, encoding="utf-8")
    (repo / "Shared" / "Helper.cs").write_text(_LINKED_HELPER, encoding="utf-8")
    (repo / "ImplA" / "ImplA.csproj").write_text(
        _LINKED_IMPL_CSPROJ.format(defines="WRITES"), encoding="utf-8"
    )
    (repo / "ImplB" / "ImplB.csproj").write_text(
        _LINKED_IMPL_CSPROJ.format(defines=""), encoding="utf-8"
    )
    (repo / "ImplB" / "Leaky.cs").write_text(_LINKED_LEAKY, encoding="utf-8")
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
    # Positive control first: the non-shared LocalWriter's direct write-back
    # must be present, proving both projects loaded and ref facts flowed, so
    # the absence below cannot pass vacuously.
    assert ("resource::ENV::K2", _STDOUT) in flows
    assert (_ENV_K, _STDOUT) not in flows


_GENERIC_CONSTRUCTIONS_REF = (
    "using System;\n\n"
    "public interface IGen<T>\n{\n"
    "    void Fill(T src, ref string dst);\n}\n\n"
    "public class Both : IGen<string>, IGen<int>\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n"
    "    void IGen<int>.Fill(int src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IGen<int> helper = new Both();\n"
    '        var sink = "";\n'
    "        helper.Fill(token?.Length ?? 0, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_the_invoked_generic_construction_selects_its_own_implementation(
    tmp_path: Path,
) -> None:
    # Both constructions of IGen are implemented with different bodies; the
    # call goes through IGen<int>, whose explicit implementation only reads,
    # so the writing IGen<string> body must not be selected for it.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "gc1", _GENERIC_CONSTRUCTIONS_REF, cs.CSharpFrontend.HYBRID
    )


_GENERIC_CONSTRUCTION_WRITER_REF = (
    "using System;\n\n"
    "public interface IGen<T>\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Both : IGen<string>, IGen<int>\n{\n"
    "    void IGen<string>.Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n"
    "    void IGen<int>.Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IGen<string> helper = new Both();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_the_invoked_generic_construction_proves_its_own_write(
    tmp_path: Path,
) -> None:
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "gc2", _GENERIC_CONSTRUCTION_WRITER_REF, cs.CSharpFrontend.HYBRID
    )


_PLUGIN_LIB_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "</Project>\n"
)
_PLUGIN_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>\n"
    "  <ItemGroup>\n"
    '    <Reference Include="MetaLib"><HintPath>{dll}</HintPath></Reference>\n'
    "  </ItemGroup>\n"
    "</Project>\n"
)
_PLUGIN_READER = (
    "using System;\n\n"
    "public class Reader : ILib\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n"
)
_STALE_PLUGIN_APP_CSPROJ = (
    '<Project Sdk="Microsoft.NET.Sdk">\n'
    "  <PropertyGroup>\n"
    "    <TargetFramework>net8.0</TargetFramework>\n"
    "    <AssemblyName>MetaLib</AssemblyName>\n"
    "  </PropertyGroup>\n"
    "  <ItemGroup>\n"
    '    <Reference Include="Plugin"><HintPath>{dll}</HintPath></Reference>\n'
    "  </ItemGroup>\n"
    "</Project>\n"
)
_STALE_PLUGIN_APP_PROGRAM = (
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
def test_a_prebuilt_plugin_implementation_keeps_the_write_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plugin compiled against an identical-identity assembly unifies with
    # the analyzed source, so its metadata Reader genuinely implements the
    # SOURCE-declared contract while owning no analyzable body. The proof
    # must see it and stay unproven, or the visible source Writer would
    # vouch for a runtime receiver that only reads.
    import subprocess

    lib = tmp_path / "libsrc"
    lib.mkdir(parents=True)
    (lib / "MetaLib.csproj").write_text(_PLUGIN_LIB_CSPROJ, encoding="utf-8")
    (lib / "ILib.cs").write_text(_METADATA_IFACE_SOURCE, encoding="utf-8")
    libout = tmp_path / "libbin"
    built = subprocess.run(
        ["dotnet", "build", str(lib), "-o", str(libout), "--nologo", "-v", "q"],
        capture_output=True,
        text=True,
        check=False,
    )
    libdll = libout / "MetaLib.dll"
    if built.returncode != 0 or not libdll.exists():
        pytest.skip(f"could not prebuild the contract assembly: {built.stderr}")

    plugin = tmp_path / "pluginsrc"
    plugin.mkdir()
    (plugin / "Plugin.csproj").write_text(
        _PLUGIN_CSPROJ.format(dll=libdll), encoding="utf-8"
    )
    (plugin / "Reader.cs").write_text(_PLUGIN_READER, encoding="utf-8")
    pluginout = tmp_path / "pluginbin"
    built = subprocess.run(
        ["dotnet", "build", str(plugin), "-o", str(pluginout), "--nologo", "-v", "q"],
        capture_output=True,
        text=True,
        check=False,
    )
    plugindll = pluginout / "Plugin.dll"
    if built.returncode != 0 or not plugindll.exists():
        pytest.skip(f"could not prebuild the plugin assembly: {built.stderr}")

    repo = tmp_path / "proj"
    repo.mkdir()
    (repo / "proj.csproj").write_text(
        _STALE_PLUGIN_APP_CSPROJ.format(dll=plugindll), encoding="utf-8"
    )
    (repo / "ILib.cs").write_text(_METADATA_IFACE_SOURCE, encoding="utf-8")
    (repo / "Program.cs").write_text(_STALE_PLUGIN_APP_PROGRAM, encoding="utf-8")
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
    assert ("resource::ENV::K2", _STDOUT) in flows
    assert (_ENV_K, _STDOUT) not in flows


_VIRTUAL_OVERRIDE_READS_REF = (
    "using System;\n\n"
    "public class HelperBase\n{\n"
    "    public virtual void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Quiet : HelperBase\n{\n"
    "    public override void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        HelperBase helper = new Quiet();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_reading_override_blocks_the_virtual_base_write(tmp_path: Path) -> None:
    # `virtual` dispatches like the interface cases: the base body is live
    # for base instances, but the receiver here runs the read-only override,
    # so proving the write from the base body alone would fabricate the flow.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "vo1", _VIRTUAL_OVERRIDE_READS_REF, cs.CSharpFrontend.HYBRID
    )


_VIRTUAL_ALL_WRITE_REF = (
    "using System;\n\n"
    "public class HelperBase\n{\n"
    "    public virtual void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Louder : HelperBase\n{\n"
    "    public override void Fill(string src, ref string dst)\n    {\n"
    '        dst = src + "!";\n    }\n}\n\n'
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        HelperBase helper = new Louder();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_a_virtual_write_survives_when_every_override_also_writes(
    tmp_path: Path,
) -> None:
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "vo2", _VIRTUAL_ALL_WRITE_REF, cs.CSharpFrontend.HYBRID
    )


_OPEN_GENERIC_READER_REF = (
    "using System;\n\n"
    "public interface IHelper<T>\n{\n"
    "    void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Reader<T> : IHelper<T>\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper<int> helper = new Reader<int>();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_an_open_generic_reader_blocks_the_default_body_write(tmp_path: Path) -> None:
    # Reader<T> implements every construction of IHelper<T>, so its read-only
    # body is a candidate for the IHelper<int> call; the writing default must
    # not be selected just because the open implementer failed to match the
    # constructed interface exactly.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "og1", _OPEN_GENERIC_READER_REF, cs.CSharpFrontend.HYBRID
    )


_OPEN_GENERIC_WRITER_REF = (
    "using System;\n\n"
    "public interface IHelper<T>\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Writer<T> : IHelper<T>\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper<int> helper = new Writer<int>();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_an_open_generic_writer_proves_the_constructed_call(tmp_path: Path) -> None:
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "og2", _OPEN_GENERIC_WRITER_REF, cs.CSharpFrontend.HYBRID
    )


_MIXED_CONSTRUCTIONS_REF = (
    "using System;\n\n"
    "public interface IHelper<T>\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Both<T> : IHelper<T>, IHelper<int>\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n"
    "    void IHelper<int>.Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        IHelper<int> helper = new Both<long>();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_every_matching_construction_of_an_open_implementer_participates(
    tmp_path: Path,
) -> None:
    # Both<T> satisfies IHelper<int> through its explicit read-only
    # implementation, while its general body writes; selecting only the first
    # matching interface instance would let the writer vouch for a call the
    # reader actually serves.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "mc1", _MIXED_CONSTRUCTIONS_REF, cs.CSharpFrontend.HYBRID
    )


_GENERIC_BASE_OVERRIDE_REF = (
    "using System;\n\n"
    "public class Base<T>\n{\n"
    "    public virtual void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n}\n\n"
    "public class IntReader : Base<int>\n{\n"
    "    public override void Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    public void Run()\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    "        Base<string> helper = new Base<string>();\n"
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n"
    "    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_an_override_of_another_construction_does_not_block_the_write(
    tmp_path: Path,
) -> None:
    # IntReader overrides Base<int>.Fill only; a call through Base<string>
    # can never reach it, so its read-only body must not join that call's
    # conjunction and mask the base write.
    assert (_ENV_K, _STDOUT) in _flows(
        tmp_path / "gb1", _GENERIC_BASE_OVERRIDE_REF, cs.CSharpFrontend.HYBRID
    )


_OPEN_CALLSITE_CLOSED_IMPL_REF = (
    "using System;\n\n"
    "public interface IHelper<T>\n{\n"
    "    void Fill(string src, ref string dst);\n}\n\n"
    "public class Both<T> : IHelper<T>, IHelper<int>\n{\n"
    "    public void Fill(string src, ref string dst)\n    {\n"
    "        dst = src;\n    }\n"
    "    void IHelper<int>.Fill(string src, ref string dst)\n    {\n"
    "        Console.Error.WriteLine(dst.Length + src.Length);\n    }\n}\n\n"
    "public class Leaky\n{\n"
    "    private void Pump<T>(IHelper<T> helper)\n    {\n"
    '        var token = Environment.GetEnvironmentVariable("K");\n'
    '        var sink = "";\n'
    "        helper.Fill(token, ref sink);\n"
    "        Console.WriteLine(sink);\n    }\n"
    "    public void Run()\n    {\n"
    "        Pump<int>(new Both<long>());\n    }\n}\n"
)


@pytest.mark.skipif(
    not csharp_frontend_available(), reason="Roslyn frontend needs a dotnet toolchain"
)
def test_an_open_call_site_sees_closed_implementations_too(tmp_path: Path) -> None:
    # Inside Pump<T> the interface construction is unbound, so the call can
    # resolve to ANY construction at runtime, including the read-only
    # explicit IHelper<int> body; the writing open implementation alone must
    # not prove the write.
    assert (_ENV_K, _STDOUT) not in _flows(
        tmp_path / "oc1", _OPEN_CALLSITE_CLOSED_IMPL_REF, cs.CSharpFrontend.HYBRID
    )
