# C# Roslyn hybrid frontend (issue #738), phase B1: the opt-in semantic layer
# corrects INHERITS-vs-IMPLEMENTS where the parse-time I-prefix heuristic is
# wrong. The wiring decides which path runs, gated on CSHARP_FRONTEND + a
# discoverable .csproj + a usable dotnet toolchain.
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.csharp_frontend import (
    csharp_frontend_available,
    run_csharp_frontend,
)
from codebase_rag.parsers.csharp_frontend.frontend import (
    _parse_payload,
    find_csharp_project,
)
from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"

# A base list the I-prefix heuristic gets exactly backwards: IWidget is a
# class (heuristic reads the I-prefix as an interface) and Renderer is an
# interface (heuristic, seeing no I-prefix on the first base, reads it as the
# base class). C# requires the base class first, so `Button : IWidget, Renderer`
# is valid with IWidget the base class.
_TYPES = """
namespace N;

public interface Renderer { void Render(); }

public class IWidget { public int Handle; }

public class Button : IWidget, Renderer
{
    public void Render() { }
}
"""

_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
"""


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "Types.cs").write_text(_TYPES, encoding="utf-8")
    (root / "Sample.csproj").write_text(_CSPROJ, encoding="utf-8")


def _pairs(mock_ingestor: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2]) for c in get_relationships(mock_ingestor, rel_type)
    }


def _has(pairs: set[tuple[str, str]], child_suffix: str, parent_suffix: str) -> bool:
    return any(
        ch.endswith(child_suffix) and pa.endswith(parent_suffix) for ch, pa in pairs
    )


_SLNX = """<Solution>
  <Project Path="Sample.csproj" />
</Solution>
"""

_EMPTY_PAYLOAD = '{"types":[],"calls":[],"partials":[],"queries":[]}'


def test_find_csharp_project_discovers_slnx_solution(temp_repo: Path) -> None:
    # Repos migrated to the XML solution format ship a .slnx and no .sln
    # (e.g. Polly); missing it silently degrades hybrid to one csproj.
    root = temp_repo / "slnxroot"
    _write_project(root)
    (root / "Sample.slnx").write_text(_SLNX, encoding="utf-8")

    found = find_csharp_project(root)

    assert found is not None
    assert found.name == "Sample.slnx"


def test_parse_payload_warns_with_tool_stderr_when_facts_empty() -> None:
    # A zero-fact run (SDK pin mismatch, unloadable solution) must surface
    # the tool's stderr diagnostics instead of looking identical to success.
    from loguru import logger

    records: list[str] = []
    sink_id = logger.add(records.append, level="WARNING")
    try:
        facts = _parse_payload(_EMPTY_PAYLOAD, stderr="[projects] 0")
    finally:
        logger.remove(sink_id)

    assert facts.base_kinds == {}
    assert any("[projects] 0" in r for r in records), records


def test_parse_payload_stays_quiet_when_facts_present() -> None:
    from loguru import logger

    payload = json.dumps(
        {"types": [{"file": "F.cs", "line": 3, "name": "B", "bases": []}]}
    )
    records: list[str] = []
    sink_id = logger.add(records.append, level="WARNING")
    try:
        _parse_payload(payload, stderr="[projects] 1")
    finally:
        logger.remove(sink_id)

    assert not records, records


def test_roslyn_tool_opens_slnx_solution(temp_repo: Path) -> None:
    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    # Environment self-gate on a sibling csproj-only project: only when the
    # plain path provably works may the .slnx path be required to work too,
    # so a .slnx regression cannot hide behind the skip.
    plain = temp_repo / "plainproj"
    _write_project(plain)
    if not run_csharp_frontend(plain).base_kinds:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    # Two projects under one .slnx: the csproj fallback loads exactly one of
    # them, so facts from BOTH prove the solution itself was opened.
    root = temp_repo / "slnxproj"
    root.mkdir()
    for member in ("A", "B"):
        sub = root / member
        _write_project(sub)
        (sub / "Sample.csproj").rename(sub / f"{member}.csproj")
    (root / "Two.slnx").write_text(
        '<Solution>\n  <Project Path="A/A.csproj" />\n'
        '  <Project Path="B/B.csproj" />\n</Solution>\n',
        encoding="utf-8",
    )

    fact_files = {file for file, _ in run_csharp_frontend(root).base_kinds}
    assert any(f.startswith("A/") for f in fact_files), fact_files
    assert any(f.startswith("B/") for f in fact_files), fact_files


def test_parse_payload_drops_conflicting_duplicate_simple_names() -> None:
    # Two bases with the same simple name but different kinds (`: A.Widget,
    # B.Widget`, one class + one interface) cannot be told apart by simple
    # name, so the name is dropped from the map and the heuristic decides;
    # a same-kind duplicate is kept.
    payload = json.dumps(
        {
            "types": [
                {
                    "file": "F.cs",
                    "line": 3,
                    "name": "Button",
                    "bases": [
                        {"name": "Widget", "kind": "class"},
                        {"name": "Widget", "kind": "interface"},
                        {"name": "Handler", "kind": "interface"},
                    ],
                },
                {
                    "file": "G.cs",
                    "line": 5,
                    "name": "Panel",
                    "bases": [
                        {"name": "Base", "kind": "class"},
                        {"name": "Base", "kind": "class"},
                    ],
                },
            ]
        }
    )
    result = _parse_payload(payload).base_kinds
    assert "Widget" not in result[("F.cs", 3)]
    assert result[("F.cs", 3)]["Handler"] == "interface"
    # A duplicate that agrees on kind is unambiguous, so it survives.
    assert result[("G.cs", 5)]["Base"] == "class"


def test_frontend_off_clears_stale_base_kinds(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reused updater (watch mode) that ran hybrid must not keep applying the
    # old oracle when a later run has the frontend off: the early return still
    # resets the map to empty so Pass 2 falls back to the heuristic.
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    updater = gu.GraphUpdater(
        ingestor=MagicMock(), repo_path=temp_repo, parsers=parsers, queries=queries
    )
    updater.factory.definition_processor.csharp_base_kinds = {
        ("Stale.cs", 1): {"Old": "class"}
    }
    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)

    updater._run_csharp_frontend()

    assert updater.factory.definition_processor.csharp_base_kinds == {}


def test_treesitter_mode_keeps_iprefix_heuristic(temp_repo: Path) -> None:
    root = temp_repo / "defaultproj"
    _write_project(root)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    inherits = _pairs(ingestor, "INHERITS")
    # In tree-sitter mode (the suite-wide test pin) the frontend never
    # runs: the heuristic reads the I-prefixed base class IWidget as an
    # interface, so it is NOT an INHERITS.
    assert not _has(inherits, "N.Button", "N.IWidget"), inherits


def test_roslyn_frontend_corrects_base_classification(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    root = temp_repo / "roslynproj"
    _write_project(root)

    # Self-gate on the toolchain actually producing facts: a missing SDK
    # workload, offline NuGet, or a build failure yields an empty map, in
    # which case there is nothing to assert (the frontend degraded to
    # tree-sitter, covered by the default-path test above).
    facts = run_csharp_frontend(root)
    if not facts.base_kinds:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    inherits = _pairs(ingestor, "INHERITS")
    implements = _pairs(ingestor, "IMPLEMENTS")
    # The semantic model fixes both: IWidget is the base class (INHERITS),
    # Renderer is the interface (IMPLEMENTS), the reverse of the heuristic.
    assert _has(inherits, "N.Button", "N.IWidget"), inherits
    assert _has(implements, "N.Button", "N.Renderer"), implements
    assert not _has(implements, "N.Button", "N.IWidget"), implements
    assert not _has(inherits, "N.Button", "N.Renderer"), inherits


def _button_facts() -> object:
    from codebase_rag.parsers.csharp_frontend import CSharpSemanticFacts

    line = next(
        i for i, text in enumerate(_TYPES.splitlines(), 1) if "class Button" in text
    )
    return CSharpSemanticFacts(
        base_kinds={("Types.cs", line): {"IWidget": "class", "Renderer": "interface"}},
        call_sites={},
        partial_groups=[],
        query_calls=[],
        external_sites=set(),
    )


def test_default_csharp_frontend_is_auto() -> None:
    # The shipped default: hybrid wherever a dotnet toolchain exists,
    # pure tree-sitter otherwise. Read from the FIELD default because the
    # test suite pins the live settings instance to tree-sitter.
    from codebase_rag.config import AppConfig

    assert AppConfig.model_fields["CSHARP_FRONTEND"].default == cs.CSharpFrontend.AUTO


def test_auto_mode_runs_frontend_when_dotnet_available(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "autoproj"
    _write_project(root)
    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.AUTO)
    monkeypatch.setattr(gu, "csharp_frontend_available", lambda: True)
    monkeypatch.setattr(gu, "run_csharp_frontend", lambda repo_path: _button_facts())

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    inherits = _pairs(ingestor, "INHERITS")
    assert _has(inherits, "N.Button", "N.IWidget"), inherits


def test_auto_mode_falls_back_silently_without_dotnet(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "autofallproj"
    _write_project(root)
    frontend_runner = MagicMock()
    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.AUTO)
    monkeypatch.setattr(gu, "csharp_frontend_available", lambda: False)
    monkeypatch.setattr(gu, "run_csharp_frontend", frontend_runner)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    frontend_runner.assert_not_called()
    inherits = _pairs(ingestor, "INHERITS")
    assert not _has(inherits, "N.Button", "N.IWidget"), inherits
