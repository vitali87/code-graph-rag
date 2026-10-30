"""`CSharpResolver`: a qualified path read the way C# reads it (namespace,
type, member; `global::` and `::` aliases; a local that shadows the head),
and same-named types ranked by the namespaces a file encloses and imports."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.csharp.resolution import (
    CSharpResolver,
    _split_csharp_qualified_path,
)
from codebase_rag.tests.collaborator_support import index
from evals.recorded_graph import RecordingIngestor

_CS = cs.SupportedLanguage.CSHARP
_MODELS = "cp.Widget.App.Models"
_OTHER = "cp.Other.App.Other"
_USE = "cp.Use"
_FILES = {
    "App.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>\n',
    "Widget.cs": (
        "namespace App.Models {\n"
        "  public class Widget {\n    public static Widget Make() { return null; }\n  }\n"
        "  public class Base {\n    public void Inherited() {}\n  }\n"
        "  public class Child : Base {}\n"
        "}\n"
    ),
    "Other.cs": "namespace App.Other {\n  public class Widget {}\n}\n",
    "Use.cs": (
        "using App.Models;\n\n"
        "namespace App.Services {\n"
        "  public class User {\n    void Go() { App.Models.Widget.Make(); }\n  }\n"
        "}\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> CSharpResolver:
    updater = index(temp_repo / "cp", mock_ingestor, _FILES)
    csharp = updater.factory.call_processor._resolver.csharp
    assert isinstance(csharp, CSharpResolver)
    return csharp


def _site(
    call_name: str,
    local_var_types: dict[str, str] | None = None,
    language: cs.SupportedLanguage = _CS,
    constructing: bool = False,
) -> CallSite:
    return CallSite(
        call_name, _USE, local_var_types, None, None, language, None, constructing
    )


def test_a_path_splits_on_dots_outside_type_arguments() -> None:
    assert _split_csharp_qualified_path("A.B<C.D>.E") == ["A", "B<C.D>", "E"]
    assert _split_csharp_qualified_path("Single") == ["Single"]


def test_a_namespace_type_member_path_binds_the_member(
    resolver: CSharpResolver,
) -> None:
    assert resolver.resolve_csharp_qualified_path(_site("App.Models.Widget.Make")) == (
        True,
        (cs.NodeLabel.METHOD, f"{_MODELS}.Widget.Make"),
    )
    # A member the type only inherits is found on its base.
    assert resolver.resolve_csharp_qualified_path(
        _site("App.Models.Child.Inherited")
    ) == (True, (cs.NodeLabel.METHOD, f"{_MODELS}.Base.Inherited"))


def test_a_global_or_alias_qualified_type_names_the_class(
    resolver: CSharpResolver,
) -> None:
    widget = (cs.NodeLabel.CLASS, f"{_MODELS}.Widget")
    assert resolver.resolve_csharp_qualified_path(
        _site("global::App.Models.Widget")
    ) == (True, widget)
    assert resolver.csharp_qualified_path_binds("App.Models.Widget", _USE, None, _CS)


def test_a_local_shadowing_the_head_leaves_the_path_to_its_type(
    resolver: CSharpResolver,
) -> None:
    shadowed = _site("App.Models.Widget.Make", local_var_types={"App": "Thing"})
    assert resolver.resolve_csharp_qualified_path(shadowed) == (False, None)


def test_only_a_csharp_call_is_answered(resolver: CSharpResolver) -> None:
    other = _site("App.Models.Widget.Make", language=cs.SupportedLanguage.JAVA)
    assert resolver.resolve_csharp_qualified_path(other) == (False, None)
    assert not resolver.csharp_qualified_path_binds(
        "App.Models.Widget", _USE, None, cs.SupportedLanguage.JAVA
    )


def test_a_same_named_type_from_an_imported_namespace_is_preferred(
    resolver: CSharpResolver,
) -> None:
    candidates = [f"{_OTHER}.Widget", f"{_MODELS}.Widget"]
    assert resolver.csharp_prefer_imported(candidates, _USE, "Widget", _CS) == [
        f"{_MODELS}.Widget"
    ]
    # One candidate, or another language, is left as it is.
    assert (
        resolver.csharp_prefer_imported(candidates[:1], _USE, "Widget", _CS)
        == (candidates[:1])
    )
    assert (
        resolver.csharp_prefer_imported(
            candidates, _USE, "Widget", cs.SupportedLanguage.JAVA
        )
        == candidates
    )
