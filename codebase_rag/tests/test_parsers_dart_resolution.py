"""`DartResolver`: a type name read in the library it is written in, the
extension member a receiver reaches (the most specific one), what a hop
through a member returns, the import prefix a local shadows, and the member
an external generic base hands back through its type arguments."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.call_site import CallSite
from codebase_rag.parsers.dart.resolution import DartResolver
from codebase_rag.tests.collaborator_support import index
from evals.recorded_graph import RecordingIngestor

_DART = cs.SupportedLanguage.DART
_SHAPES = "dp.lib.shapes"
_MAIN = "dp.lib.main"
_FILES = {
    "pubspec.yaml": "name: dp\n",
    "lib/shapes.dart": (
        "import 'package:flutter/widgets.dart';\n\n"
        "class Shape {}\n"
        "class Square extends Shape {\n  Square grow() => Square();\n}\n"
        "class Panel {\n  void render() {}\n}\n\n"
        "extension OnShape on Shape {\n  int sides() => 0;\n}\n"
        "extension OnSquare on Square {\n  int sides() => 4;\n}\n\n"
        "class Screen extends State<Panel> {\n"
        "  void build() {\n    widget.render();\n  }\n}\n"
    ),
    "lib/main.dart": (
        "import 'shapes.dart' as s;\n\n"
        "void main() {\n  s.Square().grow().sides();\n}\n\n"
        "void shadow(int s) {\n  print(s);\n}\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> DartResolver:
    updater = index(temp_repo / "dp", mock_ingestor, _FILES)
    dart = updater.factory.call_processor._resolver.dart
    assert isinstance(dart, DartResolver)
    return dart


def _site(call_name: str, class_context: str | None = None) -> CallSite:
    return CallSite(call_name, _SHAPES, None, class_context, None, _DART, None, False)


def test_a_written_type_is_read_in_its_own_library(resolver: DartResolver) -> None:
    square = f"{_SHAPES}.Square"
    assert resolver.resolve_dart_written_type("s.Square", _MAIN) == square
    assert resolver.resolve_dart_written_type("Square", _SHAPES) == square
    # An earlier hop's qn is kept as it is.
    assert resolver.resolve_dart_written_type(square, _MAIN) == square


def test_the_most_specific_extension_wins(resolver: DartResolver) -> None:
    assert resolver.dart_extension_member(f"{_SHAPES}.Square", "sides", _MAIN) == (
        cs.NodeLabel.METHOD,
        f"{_SHAPES}.OnSquare.sides",
    )
    assert resolver.dart_extension_member(f"{_SHAPES}.Shape", "sides", _MAIN) == (
        cs.NodeLabel.METHOD,
        f"{_SHAPES}.OnShape.sides",
    )
    assert resolver.dart_extension_member(f"{_SHAPES}.Panel", "sides", _MAIN) is None


def test_a_hop_returns_the_type_its_member_declares(resolver: DartResolver) -> None:
    square = f"{_SHAPES}.Square"
    assert resolver.dart_member_return_type(square, "grow", _MAIN) == square
    assert resolver.dart_member_return_type(square, "missing", _MAIN) is None


def test_an_import_prefix_names_its_libraries(resolver: DartResolver) -> None:
    assert resolver.dart_prefix_targets("s", _MAIN) == [_SHAPES]
    assert resolver.dart_prefix_targets("t", _MAIN) == []
    # `shadow`'s parameter `s` binds the name over its body, not before it.
    source = _FILES["lib/main.dart"].encode()
    assert resolver.dart_prefix_is_shadowed("s", _MAIN, source.index(b"print(s)"))
    assert not resolver.dart_prefix_is_shadowed("s", _MAIN, source.index(b"s.Square"))
    # A site with no position cannot be placed inside a shadowing scope.
    assert not resolver.dart_prefix_is_shadowed("s", _MAIN, None)


def test_an_external_generic_bases_argument_answers_its_member(
    resolver: DartResolver,
) -> None:
    screen = f"{_SHAPES}.Screen"
    assert resolver.dart_caller_dependent(_site("widget.render", screen))
    assert resolver.resolve_dart_external_base_arg_member(
        _site("widget.render", screen)
    ) == (cs.NodeLabel.METHOD, f"{_SHAPES}.Panel.render")
    # Outside such a class, or for a member no argument type has, nothing.
    assert not resolver.dart_caller_dependent(_site("widget.render", None))
    assert (
        resolver.resolve_dart_external_base_arg_member(_site("widget.missing", screen))
        is None
    )
