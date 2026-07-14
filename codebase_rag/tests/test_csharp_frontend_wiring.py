# (H) C# Roslyn hybrid frontend (issue #738), phase B1: the opt-in semantic layer
# (H) corrects INHERITS-vs-IMPLEMENTS where the parse-time I-prefix heuristic is
# (H) wrong. The wiring decides which path runs, gated on CSHARP_FRONTEND + a
# (H) discoverable .csproj + a usable dotnet toolchain.
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
from codebase_rag.parsers.csharp_frontend.frontend import _parse_payload
from codebase_rag.tests.conftest import get_relationships, run_updater

SKIP = "c_sharp"

# (H) A base list the I-prefix heuristic gets exactly backwards: IWidget is a
# (H) class (heuristic reads the I-prefix as an interface) and Renderer is an
# (H) interface (heuristic, seeing no I-prefix on the first base, reads it as the
# (H) base class). C# requires the base class first, so `Button : IWidget, Renderer`
# (H) is valid with IWidget the base class.
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


def test_parse_payload_drops_conflicting_duplicate_simple_names() -> None:
    # (H) Two bases with the same simple name but different kinds (`: A.Widget,
    # (H) B.Widget`, one class + one interface) cannot be told apart by simple
    # (H) name, so the name is dropped from the map and the heuristic decides;
    # (H) a same-kind duplicate is kept.
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
    result = _parse_payload(payload)
    assert "Widget" not in result[("F.cs", 3)]
    assert result[("F.cs", 3)]["Handler"] == "interface"
    # (H) A duplicate that agrees on kind is unambiguous, so it survives.
    assert result[("G.cs", 5)]["Base"] == "class"


def test_frontend_off_clears_stale_base_kinds(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # (H) A reused updater (watch mode) that ran hybrid must not keep applying the
    # (H) old oracle when a later run has the frontend off: the early return still
    # (H) resets the map to empty so Pass 2 falls back to the heuristic.
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


def test_default_treesitter_keeps_iprefix_heuristic(temp_repo: Path) -> None:
    root = temp_repo / "defaultproj"
    _write_project(root)

    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    inherits = _pairs(ingestor, "INHERITS")
    # (H) With the default flag the frontend never runs: the heuristic reads the
    # (H) I-prefixed base class IWidget as an interface, so it is NOT an INHERITS.
    assert not _has(inherits, "N.Button", "N.IWidget"), inherits


def test_roslyn_frontend_corrects_base_classification(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not csharp_frontend_available():
        pytest.skip("dotnet not available")
    root = temp_repo / "roslynproj"
    _write_project(root)

    # (H) Self-gate on the toolchain actually producing facts: a missing SDK
    # (H) workload, offline NuGet, or a build failure yields an empty map, in
    # (H) which case there is nothing to assert (the frontend degraded to
    # (H) tree-sitter, covered by the default-path test above).
    facts = run_csharp_frontend(root)
    if not facts:
        pytest.skip("Roslyn frontend could not build/restore in this environment")

    monkeypatch.setattr(gu.settings, "CSHARP_FRONTEND", cs.CSharpFrontend.HYBRID)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing=SKIP)

    inherits = _pairs(ingestor, "INHERITS")
    implements = _pairs(ingestor, "IMPLEMENTS")
    # (H) The semantic model fixes both: IWidget is the base class (INHERITS),
    # (H) Renderer is the interface (IMPLEMENTS), the reverse of the heuristic.
    assert _has(inherits, "N.Button", "N.IWidget"), inherits
    assert _has(implements, "N.Button", "N.Renderer"), implements
    assert not _has(implements, "N.Button", "N.IWidget"), implements
    assert not _has(inherits, "N.Button", "N.Renderer"), inherits
