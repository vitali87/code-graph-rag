# (H) Dart/Flutter structural support (issue #140): classes, mixins, extensions,
# (H) enhanced enums, factory/named constructors, functions/methods, imports and
# (H) pubspec dependencies. The tree-sitter-dart grammar splits a definition into
# (H) a signature node and a sibling function_body and has no call-expression
# (H) node, so cgr provides structural nodes/imports/inheritance (no CALLS edges).
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_nodes,
    get_relationships,
    run_updater,
)
from codebase_rag.types_defs import NodeType

SKIP = "dart"


@pytest.fixture
def dart_project(temp_repo: Path) -> Path:
    project = temp_repo / "dart_defs"
    project.mkdir()
    return project


def _endswith_any(names: set[str], suffix: str) -> bool:
    return any(n.endswith(suffix) for n in names)


def _props_for(mock_ingestor: MagicMock, node_type: str, suffix: str) -> dict:
    for call in get_nodes(mock_ingestor, node_type):
        props = call[0][1]
        if props["qualified_name"].endswith(suffix):
            return props
    raise AssertionError(f"no {node_type} ending with {suffix!r}")


def test_type_declaration_kinds(dart_project: Path, mock_ingestor: MagicMock) -> None:
    (dart_project / "types.dart").write_text(
        """
class Plain {}
abstract class Shape {}
mixin Swimmer {}
extension StringExt on String { String shout() => toUpperCase(); }
enum Color { red, green, blue }
enum Planet {
  earth(5.9),
  mars(0.6);
  final double mass;
  const Planet(this.mass);
}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    for name in ("types.Plain", "types.Shape", "types.Swimmer", "types.StringExt"):
        assert _endswith_any(classes, name), f"missing {name}: {classes}"
    enums = get_node_names(mock_ingestor, NodeType.ENUM)
    assert _endswith_any(enums, "types.Color")
    assert _endswith_any(enums, "types.Planet")


def test_functions_and_methods(dart_project: Path, mock_ingestor: MagicMock) -> None:
    (dart_project / "funcs.dart").write_text(
        """
int add(int a, int b) => a + b;

Future<int> fetchData() async {
  await Future.delayed(Duration(seconds: 1));
  return 42;
}

class Repo {
  int count = 0;
  void increment() {
    count++;
  }
  static Repo create() => Repo();
  factory Repo.empty() => Repo();
  int get value => count;
}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    funcs = get_node_names(mock_ingestor, NodeType.FUNCTION) | get_node_names(
        mock_ingestor, NodeType.METHOD
    )
    for name in (
        "funcs.add",
        "funcs.fetchData",
        "funcs.Repo.increment",
        "funcs.Repo.create",
        "funcs.Repo.empty",
        "funcs.Repo.value",
    ):
        assert _endswith_any(funcs, name), f"missing {name}: {funcs}"


def test_function_span_covers_body(
    dart_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The signature/body split means a naive capture ends the node at the
    # (H) signature line; dart_definition_end_point extends it over the body so
    # (H) the snippet spans the whole function.
    (dart_project / "span.dart").write_text(
        "int compute(int x) {\n  var y = x + 1;\n  return y;\n}\n",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    props = _props_for(mock_ingestor, NodeType.FUNCTION, "span.compute")
    assert props["start_line"] == 1
    assert props["end_line"] == 4, props


def test_imports_resolved(dart_project: Path, mock_ingestor: MagicMock) -> None:
    lib = dart_project / "lib"
    lib.mkdir()
    (lib / "helper.dart").write_text("int helper() => 1;\n", encoding="utf-8")
    (lib / "app.dart").write_text(
        """
import 'package:flutter/material.dart';
import 'dart:async';
import 'helper.dart';

void run() {}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    imports = get_relationships(mock_ingestor, "IMPORTS")
    targets = {c.args[2][2] for c in imports}
    # (H) External package + dart core kept verbatim; relative import resolves
    # (H) to the internal module qn.
    assert any("package:flutter/material.dart" in t for t in targets), targets
    assert any("dart:async" in t for t in targets), targets
    assert any(t.endswith("lib.helper") for t in targets), targets


def test_inheritance_and_implements(
    dart_project: Path, mock_ingestor: MagicMock
) -> None:
    (dart_project / "widgets.dart").write_text(
        """
class StatefulWidget {}
class Comparable {}
mixin Swimmer {}

class Counter extends StatefulWidget {}
class Fish extends StatefulWidget with Swimmer implements Comparable {}
mixin Diver on StatefulWidget {}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "INHERITS")
    }
    implements = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "IMPLEMENTS")
    }
    # (H) `with Swimmer` (mixin) and `mixin Diver on StatefulWidget` (on-clause)
    # (H) are both INHERITS; `implements Comparable` is IMPLEMENTS.
    assert ("Counter", "StatefulWidget") in inherits, inherits
    assert ("Fish", "StatefulWidget") in inherits, inherits
    assert ("Fish", "Swimmer") in inherits, inherits
    assert ("Diver", "StatefulWidget") in inherits, inherits
    assert ("Fish", "Comparable") in implements, implements


def test_generic_supertypes_strip_type_arguments(
    dart_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A generic supertype's `type_arguments` (`State<HomePage>`) is a sibling
    # (H) of the base `type_identifier`, so extraction yields the bare base name
    # (H) (State/Comparable) - the common Flutter `extends State<T>` pattern.
    (dart_project / "gen.dart").write_text(
        """
class State<T> {}
class HomePage {}
class Comparable<T> {}
class Mix {}
class _HomePageState extends State<HomePage> with Mix implements Comparable<int> {}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    inherits = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "INHERITS")
    }
    implements = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "IMPLEMENTS")
    }
    assert ("_HomePageState", "State") in inherits, inherits
    assert ("_HomePageState", "Mix") in inherits, inherits
    assert ("_HomePageState", "Comparable") in implements, implements


def test_enum_implements_interface(
    dart_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) A Dart enum (plain or enhanced) can `implements` an interface. Enums use
    # (H) the shared `enum_declaration` node type, so they ride the IMPLEMENTS gate.
    (dart_project / "en.dart").write_text(
        """
abstract class Shape {}
enum Kind implements Shape {
  a(1),
  b(2);
  final int v;
  const Kind(this.v);
}
""",
        encoding="utf-8",
    )
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    implements = {
        (c.args[0][2].split(".")[-1], c.args[2][2].split(".")[-1])
        for c in get_relationships(mock_ingestor, "IMPLEMENTS")
    }
    assert ("Kind", "Shape") in implements, implements


def test_pubspec_indentation_variants(
    dart_project: Path, mock_ingestor: MagicMock
) -> None:
    # (H) Dependency blocks may be indented by any consistent amount (2 spaces,
    # (H) 4 spaces, ...); entries at the block's own indent level are packages
    # (H) regardless of that width. Nested blocks (sdk:) sit deeper and are
    # (H) recorded name-only via their parent key.
    (dart_project / "pubspec.yaml").write_text(
        """
name: deep_app
dependencies:
    flutter:
        sdk: flutter
    http: ^1.0.0
    provider: ^6.0.0
""",
        encoding="utf-8",
    )
    (dart_project / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    names = {
        c.args[2][2] for c in get_relationships(mock_ingestor, "DEPENDS_ON_EXTERNAL")
    }
    for pkg in ("http", "provider", "flutter"):
        assert pkg in names, f"missing dependency {pkg}: {names}"


def test_pubspec_dependencies(dart_project: Path, mock_ingestor: MagicMock) -> None:
    (dart_project / "pubspec.yaml").write_text(
        """
name: my_app
description: A sample app.
dependencies:
  flutter:
    sdk: flutter
  http: ^1.0.0
  provider: ^6.0.0
dev_dependencies:
  flutter_test:
    sdk: flutter
  mockito: ^5.0.0
""",
        encoding="utf-8",
    )
    (dart_project / "main.dart").write_text("void main() {}\n", encoding="utf-8")
    run_updater(dart_project, mock_ingestor, skip_if_missing=SKIP)

    deps = get_relationships(mock_ingestor, "DEPENDS_ON_EXTERNAL")
    names = {c.args[2][2] for c in deps}
    for pkg in ("http", "provider", "mockito"):
        assert pkg in names, f"missing dependency {pkg}: {names}"
