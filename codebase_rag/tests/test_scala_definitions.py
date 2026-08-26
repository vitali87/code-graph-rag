# Scala baseline tree-sitter parity (issues #105, #1186 stage 1).
#
# Before this work Scala was the emptiest slot in the language matrix: the
# constants, the LanguageSpec node-type tuples and the grammar module all
# existed, but the spec carried no function_query/class_query/call_query, so a
# .scala file produced Project/Folder/File and nothing else. Measured on a
# fixture with a trait, a class, an object and a case class: zero Module, zero
# Class, zero Function, zero Method, zero CALLS.
#
# That is why these tests assert node KINDS and specific qualified names
# rather than "some nodes were produced" -- the pre-existing behaviour already
# produced nodes, just never the ones that matter.
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

    The pre-existing behaviour produced no CALLS at all for Scala, so this is
    the edge that makes the graph useful rather than merely populated.
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

    Without it the file is seen but never parsed -- which is exactly the
    pre-existing state, where Scala produced Project/Folder/File only.
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
