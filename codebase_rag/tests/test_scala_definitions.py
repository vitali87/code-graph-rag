# Scala baseline tree-sitter parity (issues #105, #1186 stage 1).
#
# Only ONE behaviour here is new: INHERITS from `extends ... with ...`.
# Classes, objects, traits, case classes, methods, modules and CALLS were all
# already extracted before this change -- their node-type tuples are wired
# into the Scala LanguageSpec on the merge base and are untouched by this PR.
# Those four tests are REGRESSION COVERAGE for behaviour that already worked,
# not proof of work done here.
#
# An earlier version of this comment claimed Scala emitted Project/Folder/File
# and nothing else. That came from a probe run against a bare directory with
# no project marker, so the file was never treated as source at all -- the
# measurement was of my harness, not of Scala support. Recorded because a
# false baseline in a test file is worse than none: it is exactly where a
# later reader looks to learn what the language could already do, and #1186
# carries the same overstatement.
#
# The assertions are on node KINDS and specific qualified names rather than
# "some nodes were produced", because Scala already produced nodes -- a test
# that only counted them would pass against both the working and the broken
# implementation.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import (
    get_node_names,
    get_relationships,
    run_updater,
)
from codebase_rag.types_defs import NodeType

SKIP = "scala"


@pytest.fixture
def scala_project(temp_repo: Path) -> Path:
    project = temp_repo / "scala_defs"
    project.mkdir()
    return project


def _endswith_any(names: set[str], suffix: str) -> bool:
    return any(n.endswith(suffix) for n in names)


def test_type_declaration_kinds(scala_project: Path, mock_ingestor: MagicMock) -> None:
    """class, object, trait and case class all become Class nodes.

    Scala's four type forms are distinct grammar productions, so a spec that
    names only `class_definition` silently drops the other three -- and an
    object is how Scala spells a singleton/static holder, which means missing
    it loses the entry point of most programs.
    """
    (scala_project / "types.scala").write_text(
        """
package com.example

trait Greeter {
  def greet(name: String): String
}

class EnglishGreeter extends Greeter {
  def greet(name: String): String = "hello " + name
}

object Registry {
  def lookup(key: String): String = key
}

case class Point(x: Int, y: Int)
"""
    )

    run_updater(scala_project, mock_ingestor, skip_if_missing=SKIP)

    classes = get_node_names(mock_ingestor, NodeType.CLASS)
    for expected in ("Greeter", "EnglishGreeter", "Registry", "Point"):
        assert _endswith_any(classes, expected), (expected, sorted(classes))


def test_methods_are_attached_to_their_type(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """A `def` inside a type is a Method qualified by that type.

    Asserting the qualified name rather than the bare method name: a Method
    node named `greet` attached to the wrong owner is the failure that a
    name-only assertion cannot see.
    """
    (scala_project / "svc.scala").write_text(
        """
package com.example

class Service {
  def handle(x: Int): Int = x
}

object Helper {
  def assist(y: Int): Int = y
}
"""
    )

    run_updater(scala_project, mock_ingestor, skip_if_missing=SKIP)

    methods = get_node_names(mock_ingestor, NodeType.METHOD)
    assert _endswith_any(methods, "Service.handle"), sorted(methods)
    assert _endswith_any(methods, "Helper.assist"), sorted(methods)


def test_inheritance_edges_from_extends_and_with(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """`extends A with B` records the linearization, not just the first base.

    Scala mixes in traits with `with`, so reading only the `extends` clause
    captures one base and silently drops every mixin -- which is most of the
    interesting structure in idiomatic Scala.
    """
    (scala_project / "mixins.scala").write_text(
        """
package com.example

trait Named {
  def name: String
}

trait Aged {
  def age: Int
}

class Person extends Named with Aged {
  def name: String = "x"
  def age: Int = 1
}
"""
    )

    run_updater(scala_project, mock_ingestor, skip_if_missing=SKIP)

    targets = {str(c.args[2][2]) for c in get_relationships(mock_ingestor, "INHERITS")}
    assert _endswith_any(targets, "Named"), sorted(targets)
    assert _endswith_any(targets, "Aged"), sorted(targets)


def test_calls_between_methods_are_recorded(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """A method calling a sibling produces a CALLS edge.

    This already worked before this change; the test is regression coverage,
    not new behaviour. It earns its place because CALLS is the edge that makes
    the graph useful rather than merely populated, and the call node types it
    depends on sit in the same LanguageSpec this PR's sibling commit edits.
    """
    (scala_project / "calls.scala").write_text(
        """
package com.example

class Calc {
  def outer(x: Int): Int = inner(x)
  def inner(x: Int): Int = x * 2
}
"""
    )

    run_updater(scala_project, mock_ingestor, skip_if_missing=SKIP)

    pairs = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, "CALLS")
    }
    assert any(
        src.endswith("Calc.outer") and dst.endswith("Calc.inner") for src, dst in pairs
    ), sorted(pairs)


def test_a_module_node_exists_for_the_file(
    scala_project: Path, mock_ingestor: MagicMock
) -> None:
    """Every parsed source file gets a Module node.

    Already worked before this change -- regression coverage. Worth pinning
    because a missing Module node means the file was seen but never parsed,
    and every other assertion in this file depends on it: they would all fail
    together, so this one names the reason.
    """
    (scala_project / "mod.scala").write_text(
        """
package com.example

object Solo {
  def run(): Int = 1
}
"""
    )

    run_updater(scala_project, mock_ingestor, skip_if_missing=SKIP)

    modules = get_node_names(mock_ingestor, NodeType.MODULE)
    assert _endswith_any(modules, "mod"), sorted(modules)
