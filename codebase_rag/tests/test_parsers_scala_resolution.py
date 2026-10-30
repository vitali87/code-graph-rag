"""`ScalaResolver`: the class a type name written in a Scala file names, the
method a parameterless selection runs, and the callee the imports in scope at
a call bind."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.scala.resolution import ScalaResolver
from codebase_rag.tests.collaborator_support import index
from evals.recorded_graph import RecordingIngestor

_SRC = "proj.src.main.scala"
_CART = f"{_SRC}.shop.Cart.Cart"
_APP = f"{_SRC}.app.App"
_FILES = {
    "src/main/scala/shop/Cart.scala": (
        "package shop\n"
        "class Cart { def size: Int = 0; val count: Int = 0 }\n"
        "object Pricing { def rate(): Double = 1 }\n"
    ),
    "src/main/scala/app/App.scala": (
        "package app\nimport shop.Cart\nimport shop.Pricing\n"
        "object App { def run(): Int = new Cart().size }\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> ScalaResolver:
    updater = index(temp_repo / "proj", mock_ingestor, _FILES)
    scala = updater.factory.call_processor._resolver.scala
    assert isinstance(scala, ScalaResolver)
    return scala


def test_a_written_type_names_the_class_its_import_binds(
    resolver: ScalaResolver,
) -> None:
    assert resolver.scala_class_qn("Cart", _APP) == _CART
    assert resolver.scala_class_qn("Missing", _APP) is None
    # An object is registered as a class, so a type name may name one.
    assert resolver.scala_class_qn("Pricing", _APP) == f"{_SRC}.shop.Cart.Pricing"


def test_a_selection_binds_only_a_method_of_the_known_class(
    resolver: ScalaResolver,
) -> None:
    method = resolver.resolve_scala_selection("Cart", "size", _APP, None, 0)
    assert method == (cs.NodeLabel.METHOD, f"{_CART}.size")
    # A `val` read is spelled like a call, and is no edge.
    assert resolver.resolve_scala_selection("Cart", "count", _APP, None, 0) is None
    assert resolver.resolve_scala_selection("Nope", "size", _APP, None, 0) is None


def test_this_selects_on_the_enclosing_class(resolver: ScalaResolver) -> None:
    module = f"{_SRC}.shop.Cart"
    assert resolver.resolve_scala_selection("this", "size", module, _CART, 0) == (
        cs.NodeLabel.METHOD,
        f"{_CART}.size",
    )


def test_a_construction_binds_the_imported_class(resolver: ScalaResolver) -> None:
    assert resolver.resolve_scala_callee("Cart", _APP, 0, constructing=True) == (
        True,
        (cs.NodeLabel.CLASS, _CART),
    )


def test_a_name_no_block_import_binds_is_left_undecided(
    resolver: ScalaResolver,
) -> None:
    assert resolver.resolve_scala_callee("helper", _APP, 0, constructing=False) == (
        False,
        None,
    )
