# (H) C# Roslyn hybrid frontend (issue #738), the full semantic set: exact
# (H) overload resolution and extension binding via per-invocation call facts,
# (H) exact partial-class symbol identity via declaration-location groups, and
# (H) LINQ query-operator CALLS edges that query syntax hides from tree-sitter
# (H) (a query expression has no invocation nodes). All joins are location-keyed
# (H) and degrade to the tree-sitter heuristics on any key miss, so these tests
# (H) drive the wiring with synthetic facts and need no dotnet toolchain.
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.csharp_frontend import (
    CSharpCallSite,
    CSharpQueryCall,
    CSharpSemanticFacts,
)
from codebase_rag.parsers.csharp_frontend.frontend import _parse_payload
from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"

_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
"""


def _loc(source: str, needle: str) -> tuple[int, int]:
    # (H) (1-based line, 0-based col) of the first occurrence of `needle`,
    # (H) matching tree-sitter/Roslyn location conventions for ASCII sources.
    for line_no, line in enumerate(source.splitlines(), 1):
        if (col := line.find(needle)) >= 0:
            return line_no, col
    raise AssertionError(needle)


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _has(pairs: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        ca.endswith(caller_suffix) and ce.endswith(callee_suffix) for ca, ce in pairs
    )


def _hybrid(monkeypatch: pytest.MonkeyPatch, facts: CSharpSemanticFacts) -> None:
    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    monkeypatch.setattr(gu, "csharp_frontend_available", lambda: True)
    monkeypatch.setattr(gu, "run_csharp_frontend", lambda repo_path: facts)


def test_parse_payload_reads_semantic_fact_sections() -> None:
    payload = json.dumps(
        {
            "types": [
                {
                    "file": "F.cs",
                    "line": 3,
                    "name": "Button",
                    "bases": [{"name": "Widget", "kind": "class"}],
                }
            ],
            "calls": [
                {
                    "file": "A.cs",
                    "line": 5,
                    "col": 8,
                    "name": "Handle",
                    "tfile": "B.cs",
                    "tline": 3,
                    "tcol": 4,
                }
            ],
            "partials": [[{"file": "A.cs", "line": 1}, {"file": "C.cs", "line": 2}]],
            "queries": [
                {
                    "file": "A.cs",
                    "line": 9,
                    "col": 4,
                    "name": "Select",
                    "tfile": "B.cs",
                    "tline": 7,
                    "tcol": 4,
                }
            ],
        }
    )
    facts = _parse_payload(payload)
    assert facts.base_kinds[("F.cs", 3)]["Widget"] == "class"
    assert facts.call_sites[("A.cs", 5, 8, "Handle")] == CSharpCallSite(
        "Handle", "B.cs", 3, 4
    )
    assert facts.partial_groups == [[("A.cs", 1), ("C.cs", 2)]]
    assert facts.query_calls == [CSharpQueryCall("A.cs", 9, 4, "B.cs", 7, 4)]


def test_parse_payload_without_new_sections_yields_empty_facts() -> None:
    # (H) An older tool build (stale cached DLL) emits only `types`; the new
    # (H) sections must default to empty instead of raising.
    facts = _parse_payload(json.dumps({"types": []}))
    assert facts.call_sites == {}
    assert facts.partial_groups == []
    assert facts.query_calls == []


_OVERLOAD_SRC = """namespace N;

public class C
{
    public void Handle(int x) { }
    public void Handle(string s) { }
}

public class App
{
    public C Make() { return new C(); }

    public void Go()
    {
        Make().Handle("x");
    }

    public void GoSafe(C c)
    {
        c?.Handle("y");
    }
}
"""


def test_call_fact_resolves_chained_receiver_to_exact_overload(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) `Make().Handle("x")`: the receiver is a chained call the tree-sitter
    # (H) engine cannot type, and the two overloads share arity 1 so no
    # (H) heuristic may guess. The Roslyn call fact (keyed on the callee NAME
    # (H) token, since nested invocations share an expression start) binds the
    # (H) exact string overload.
    root = temp_repo / "overloadproj"
    root.mkdir()
    (root / "Code.cs").write_text(_OVERLOAD_SRC, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    call_line, call_col = _loc(_OVERLOAD_SRC, 'Handle("x")')
    target_line, target_col = _loc(_OVERLOAD_SRC, "public void Handle(string s)")
    facts = CSharpSemanticFacts(
        base_kinds={},
        call_sites={
            ("Code.cs", call_line, call_col, "Handle"): CSharpCallSite(
                "Handle", "Code.cs", target_line, target_col
            )
        },
        partial_groups=[],
        query_calls=[],
    )
    _hybrid(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.App.Go", "N.C.Handle(string)"), calls
    # (H) Scoped to the fact-driven caller: GoSafe has no fact in THIS test, so
    # (H) its call legitimately falls back to the arity heuristic's guess.
    assert not any(
        ca.endswith("N.App.Go") and ce.endswith("Handle(int)") for ca, ce in calls
    ), calls


def test_call_fact_resolves_conditional_access_invocation(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) `c?.Handle("y")`: tree-sitter wraps the callee in a
    # (H) conditional_access_expression (not a member_access_expression), so the
    # (H) heuristic path never types it; the Roslyn fact keyed on the
    # (H) member_binding name token must still join.
    root = temp_repo / "condproj"
    root.mkdir()
    (root / "Code.cs").write_text(_OVERLOAD_SRC, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    call_line, call_col = _loc(_OVERLOAD_SRC, 'Handle("y")')
    target_line, target_col = _loc(_OVERLOAD_SRC, "public void Handle(string s)")
    facts = CSharpSemanticFacts(
        base_kinds={},
        call_sites={
            ("Code.cs", call_line, call_col, "Handle"): CSharpCallSite(
                "Handle", "Code.cs", target_line, target_col
            )
        },
        partial_groups=[],
        query_calls=[],
    )
    _hybrid(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.App.GoSafe(C)", "N.C.Handle(string)"), calls


_BARE_FANOUT_SRC = """namespace N;

public class C
{
    public void Go()
    {
        Format(1);
    }

    public void Format(int i) { }

    public void Format(string s) { }
}
"""


def test_call_fact_suppresses_same_arity_family_fanout(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) A bare `Format(1)` with a Roslyn call fact is the COMPILER's overload
    # (H) choice: the same-arity family fan-out (which keeps type-dispatched
    # (H) switch families reachable when only arity is known) must NOT fire, or
    # (H) it would revive the provably-uncalled `Format(string)` sibling in
    # (H) dead-code output.
    root = temp_repo / "barefanproj"
    root.mkdir()
    (root / "Code.cs").write_text(_BARE_FANOUT_SRC, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    call_line, call_col = _loc(_BARE_FANOUT_SRC, "Format(1)")
    target_line, target_col = _loc(_BARE_FANOUT_SRC, "public void Format(int i)")
    facts = CSharpSemanticFacts(
        base_kinds={},
        call_sites={
            ("Code.cs", call_line, call_col, "Format"): CSharpCallSite(
                "Format", "Code.cs", target_line, target_col
            )
        },
        partial_groups=[],
        query_calls=[],
    )
    _hybrid(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.C.Go", "N.C.Format(int)"), calls
    assert not _has(calls, "N.C.Go", "N.C.Format(string)"), calls


_PART_A = """namespace N;

public partial class W
{
}
"""

_PART_B = """namespace N;

public partial class W
{
    public void FromOther() { }
}
"""

_PART_CALLER = """namespace N;

public class Decoy
{
    public void FromOther(int a) { }
}

public class User
{
    public void Go(W w)
    {
        w.FromOther();
    }
}
"""


def test_partial_fact_merges_parts_across_directories(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) The syntactic grouping deliberately under-merges: partial parts in
    # (H) DIFFERENT directories of one project stay separate (the directory key
    # (H) exists to avoid cross-project merges), so `w.FromOther()` is ambiguous
    # (H) between two W candidates and stays unresolved (Decoy's same-named
    # (H) method blocks the simple-name fallback). The Roslyn partial fact
    # (H) carries exact symbol identity, so the parts merge and the member on
    # (H) the other part binds.
    root = temp_repo / "partialproj"
    (root / "dirA").mkdir(parents=True)
    (root / "dirB").mkdir()
    (root / "dirA" / "Part1.cs").write_text(_PART_A, encoding="utf-8")
    (root / "dirB" / "Part2.cs").write_text(_PART_B, encoding="utf-8")
    (root / "Caller.cs").write_text(_PART_CALLER, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    line_a, _ = _loc(_PART_A, "public partial class W")
    line_b, _ = _loc(_PART_B, "public partial class W")
    facts = CSharpSemanticFacts(
        base_kinds={},
        call_sites={},
        partial_groups=[[("dirA/Part1.cs", line_a), ("dirB/Part2.cs", line_b)]],
        query_calls=[],
    )
    _hybrid(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.User.Go(W)", "N.W.FromOther"), calls
    assert not any(ce.endswith("Decoy.FromOther(int)") for _, ce in calls), calls


_LINQ_OPS = """namespace N;

public static class Ops
{
    public static object Select(this Src s, object f)
    {
        return f;
    }
}
"""

_LINQ_SRC_TYPE = """namespace N;

public class Src
{
}
"""

_LINQ_QUERY = """namespace N;

public class Q
{
    public object Go(Src s)
    {
        return from x in s select x;
    }
}
"""


def test_query_fact_emits_calls_edge_for_linq_operator(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) Query syntax has NO invocation nodes, so tree-sitter can never emit
    # (H) the `select` -> Ops.Select edge; the Roslyn query fact adds it, keyed
    # (H) on the enclosing member's declaration location.
    root = temp_repo / "linqproj"
    root.mkdir()
    (root / "Ops.cs").write_text(_LINQ_OPS, encoding="utf-8")
    (root / "Src.cs").write_text(_LINQ_SRC_TYPE, encoding="utf-8")
    (root / "Q.cs").write_text(_LINQ_QUERY, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    caller_line, caller_col = _loc(_LINQ_QUERY, "public object Go(Src s)")
    target_line, target_col = _loc(_LINQ_OPS, "public static object Select")
    facts = CSharpSemanticFacts(
        base_kinds={},
        call_sites={},
        partial_groups=[],
        query_calls=[
            CSharpQueryCall(
                "Q.cs", caller_line, caller_col, "Ops.cs", target_line, target_col
            )
        ],
    )
    _hybrid(monkeypatch, facts)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.Q.Go(Src)", "N.Ops.Select(Src, object)"), calls


_E2E_CODE = """namespace N;

public class C
{
    public void Handle(int x) { }
    public void Handle(string s) { }
}

public static class Ext
{
    public static int Twice(this C c) { return 2; }
}

public class App
{
    public C Make() { return new C(); }

    public void Go()
    {
        Make().Handle("x");
        Make().Twice();
    }
}
"""

_E2E_LINQ = """using System;

namespace N;

public class Src
{
}

public static class Ops
{
    public static Src Select(this Src s, Func<int, int> f)
    {
        return s;
    }
}

public class Q
{
    public Src Go(Src s)
    {
        return from x in s select x;
    }
}
"""


def test_roslyn_tool_emits_semantic_facts(temp_repo: Path) -> None:
    # (H) End-to-end against the real bundled tool: the compiled Roslyn frontend
    # (H) must emit exact-overload call facts, reduced extension-method call
    # (H) facts, partial declaration groups, and first-party LINQ query-operator
    # (H) facts, all with locations matching the source layout.
    from codebase_rag.parsers.csharp_frontend import (
        csharp_frontend_available,
        run_csharp_frontend,
    )

    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    root = temp_repo / "e2eproj"
    (root / "dirA").mkdir(parents=True)
    (root / "dirB").mkdir()
    (root / "E2E.cs").write_text(_E2E_CODE, encoding="utf-8")
    (root / "Linq.cs").write_text(_E2E_LINQ, encoding="utf-8")
    (root / "dirA" / "PartA.cs").write_text(_PART_A, encoding="utf-8")
    (root / "dirB" / "PartB.cs").write_text(_PART_B, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    facts = run_csharp_frontend(root)
    if not (
        facts.base_kinds
        or facts.call_sites
        or facts.partial_groups
        or facts.query_calls
    ):
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    handle_line, _ = _loc(_E2E_CODE, "public void Handle(string s)")
    handle = [f for k, f in facts.call_sites.items() if k[3] == "Handle"]
    assert any(
        f.target_file == "E2E.cs" and f.target_line == handle_line for f in handle
    ), facts.call_sites

    twice_line, _ = _loc(_E2E_CODE, "public static int Twice(this C c)")
    twice = [f for k, f in facts.call_sites.items() if k[3] == "Twice"]
    assert any(
        f.target_file == "E2E.cs" and f.target_line == twice_line for f in twice
    ), facts.call_sites

    line_a, _ = _loc(_PART_A, "public partial class W")
    line_b, _ = _loc(_PART_B, "public partial class W")
    assert any(
        {("dirA/PartA.cs", line_a), ("dirB/PartB.cs", line_b)} <= set(group)
        for group in facts.partial_groups
    ), facts.partial_groups

    select_line, _ = _loc(_E2E_LINQ, "public static Src Select")
    go_line, _ = _loc(_E2E_LINQ, "public Src Go(Src s)")
    assert any(
        q.caller_file == "Linq.cs"
        and q.caller_line == go_line
        and q.target_file == "Linq.cs"
        and q.target_line == select_line
        for q in facts.query_calls
    ), facts.query_calls


def test_hybrid_end_to_end_produces_semantic_edges(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) The full pipeline with REAL Roslyn facts (no synthetic maps): proves
    # (H) the tool's emitted line/col keys actually match tree-sitter's node
    # (H) positions and the Pass-2 registries, end to end.
    from codebase_rag.parsers.csharp_frontend import (
        csharp_frontend_available,
        run_csharp_frontend,
    )

    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    root = temp_repo / "hybride2e"
    (root / "dirA").mkdir(parents=True)
    (root / "dirB").mkdir()
    (root / "Code.cs").write_text(_OVERLOAD_SRC, encoding="utf-8")
    (root / "Linq.cs").write_text(_E2E_LINQ, encoding="utf-8")
    (root / "dirA" / "Part1.cs").write_text(_PART_A, encoding="utf-8")
    (root / "dirB" / "Part2.cs").write_text(_PART_B, encoding="utf-8")
    (root / "Caller.cs").write_text(_PART_CALLER, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    facts = run_csharp_frontend(root)
    if not facts.call_sites:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.App.Go", "N.C.Handle(string)"), calls
    # (H) The conditional-access form binds through the real Roslyn fact too.
    assert _has(calls, "N.App.GoSafe(C)", "N.C.Handle(string)"), calls
    assert not any(ce.endswith("Handle(int)") for _, ce in calls), calls
    assert _has(calls, "N.User.Go(W)", "N.W.FromOther"), calls
    assert any(
        ca.endswith("N.Q.Go(Src)") and ".Ops.Select(" in ce for ca, ce in calls
    ), calls


_UNICODE_SRC = """namespace N;

public class C
{
    public void Handle(int x) { }
    public void Handle(string s) { }
}

public class App
{
    public C Make() { return new C(); }

    public void Go()
    {
        var s = "café"; Make().Handle(s);
    }
}
"""


def test_call_fact_survives_non_ascii_prefix_on_line(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) tree-sitter columns are BYTE offsets; Roslyn's LinePosition.Character
    # (H) is UTF-16 code units. A non-ASCII character before the callee name
    # (H) token on the same line makes the two diverge, so the tool must emit
    # (H) byte columns or the join silently misses. Real facts end to end.
    from codebase_rag.parsers.csharp_frontend import (
        csharp_frontend_available,
        run_csharp_frontend,
    )

    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    root = temp_repo / "unicodeproj"
    root.mkdir()
    (root / "Code.cs").write_text(_UNICODE_SRC, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")

    facts = run_csharp_frontend(root)
    if not facts.call_sites:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    calls = _pairs(ingestor, "CALLS")
    assert _has(calls, "N.App.Go", "N.C.Handle(string)"), calls
    assert not any(ce.endswith("Handle(int)") for _, ce in calls), calls


def test_frontend_off_clears_stale_semantic_facts(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) Watch-mode reset must cover the new fact stores, not just base kinds:
    # (H) a later run with the frontend off may not keep binding stale targets.
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=MagicMock(), repo_path=temp_repo, parsers=parsers, queries=queries
    )
    dp = updater.factory.definition_processor
    dp.csharp_call_sites[("Stale.cs", 1, 0, "Old")] = CSharpCallSite(
        "Old", "Stale.cs", 2, 0
    )
    updater._csharp_partial_decls = [[("Stale.cs", 1)]]
    updater._csharp_query_calls = [CSharpQueryCall("Stale.cs", 1, 0, "Stale.cs", 2, 0)]
    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)

    updater._run_csharp_frontend()

    assert dp.csharp_call_sites == {}
    assert updater._csharp_partial_decls == []
    assert updater._csharp_query_calls == []
