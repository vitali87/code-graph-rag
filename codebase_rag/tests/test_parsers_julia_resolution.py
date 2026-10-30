"""`JuliaResolver` and its scope rules: a nested `module` block's own `using`
shadows the file's, and a selected import (`using .api: alpha`) binds the
member of its source module rather than any same-named function."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.julia.resolution import (
    JuliaResolver,
    _julia_caller_scopes,
    julia_caller_dependent,
)
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "aaa.jl": "module aaa\nexport alpha\nalpha() = 9\nend\n",
    "api.jl": "module api\nalpha() = 7\nend\n",
    "main.jl": (
        "using .aaa\nusing .api: alpha\nf() = alpha()\n"
        "module Inner\nusing .aaa: alpha\ng() = alpha()\nend\n"
    ),
}


def _site(caller_qn: str | None, language: cs.SupportedLanguage) -> CallSite:
    return CallSite("alpha", "jl.main", None, None, caller_qn, language, None, False)


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> JuliaResolver:
    updater = index(temp_repo / "jl", mock_ingestor, _FILES)
    julia = updater.factory.call_processor._resolver.julia
    assert isinstance(julia, JuliaResolver)
    return julia


def test_only_a_caller_in_a_nested_module_depends_on_its_own_scope() -> None:
    julia = cs.SupportedLanguage.JULIA
    assert julia_caller_dependent(_site("jl.main.Inner.g", julia))
    assert not julia_caller_dependent(_site("jl.main.f", julia))
    assert not julia_caller_dependent(_site(None, julia))
    assert not julia_caller_dependent(
        _site("jl.main.Inner.g", cs.SupportedLanguage.PYTHON)
    )


def test_a_callers_scopes_run_innermost_first() -> None:
    assert _julia_caller_scopes("jl.main.A.B.g", "jl.main") == [
        "jl.main.A.B",
        "jl.main.A",
        "jl.main",
    ]
    assert _julia_caller_scopes("jl.main.f", "jl.main") == ["jl.main"]
    # A caller outside the module is resolved in the module alone.
    assert _julia_caller_scopes("other.f", "jl.main") == ["jl.main"]


def test_a_nested_modules_import_shadows_the_files(
    resolver: JuliaResolver, mock_ingestor: RecordingIngestor
) -> None:
    calls = edges(mock_ingestor, cs.RelationshipType.CALLS)
    assert ("jl.main.f", "jl.api.api.alpha") in calls
    assert ("jl.main.Inner.g", "jl.aaa.aaa.alpha") in calls
    assert ("jl.main.Inner.g", "jl.api.api.alpha") not in calls


def test_the_scoped_import_answers_only_for_a_julia_caller(
    resolver: JuliaResolver,
) -> None:
    julia = cs.SupportedLanguage.JULIA
    assert resolver.resolve_julia_scoped_import(
        "alpha", "jl.main", julia, "jl.main.Inner.g"
    ) == (cs.NodeLabel.FUNCTION, "jl.aaa.aaa.alpha")
    assert (
        resolver.resolve_julia_scoped_import(
            "alpha", "jl.main", cs.SupportedLanguage.PYTHON, "jl.main.f"
        )
        is None
    )
    assert resolver.resolve_julia_scoped_import("alpha", "jl.main", julia, None) is None


def test_a_selected_import_binds_the_member_of_its_module(
    resolver: JuliaResolver,
) -> None:
    assert resolver.julia_selected_import_member("jl.api", "alpha") == (
        cs.NodeLabel.FUNCTION,
        "jl.api.api.alpha",
    )
    assert resolver.julia_selected_import_member("jl.api", "missing") is None
