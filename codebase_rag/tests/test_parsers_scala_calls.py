"""`ScalaCallResolution`: the callee a Scala call binds, the parameterless
method a bare selection (`c.size`) calls, the name an infix or `new` call
carries, and the auxiliary constructors a construction may run."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.scala.calls import ScalaCallResolution
from codebase_rag.tests.collaborator_support import edges, first, index, parse
from evals.recorded_graph import RecordingIngestor

_SRC = "proj.src.main.scala"
_FILES = {
    "src/main/scala/shop/Cart.scala": (
        "package shop\n"
        "class Cart(n: Int) {\n"
        "  def this() = this(0)\n"
        "  def total(): Double = 0\n"
        "  def size: Int = 0\n"
        "  val count: Int = 0\n"
        "}\n"
        "class Bag\n"
    ),
    "src/main/scala/app/App.scala": (
        "package app\nimport shop.{Bag, Cart}\n"
        "object App {\n"
        "  def run(): Int = { val c = new Cart(); println(c.total); c.count; c.size }\n"
        "  def bag(): Bag = new Bag\n"
        "  def unknown(x: Any): Any = x.size\n"
        "}\n"
    ),
}
_RUN = f"{_SRC}.app.App.App.run"


def _calls(temp_repo: Path, mock_ingestor: RecordingIngestor) -> set[tuple[str, str]]:
    updater = index(temp_repo / "proj", mock_ingestor, _FILES)
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.SCALA)
    assert isinstance(strategy, ScalaCallResolution)
    return edges(mock_ingestor, cs.RelationshipType.CALLS)


def test_a_parameterless_selection_on_a_typed_local_calls_the_method(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    calls = _calls(temp_repo, mock_ingestor)
    assert (_RUN, f"{_SRC}.shop.Cart.Cart.size") in calls
    assert (_RUN, f"{_SRC}.shop.Cart.Cart.total") in calls


def test_a_selection_of_a_val_or_on_an_untyped_receiver_calls_nothing(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    calls = _calls(temp_repo, mock_ingestor)
    assert not any(target.endswith(".count") for _, target in calls)
    assert not any(source.endswith(".unknown") for source, _ in calls)


def test_new_runs_the_auxiliary_constructor_where_the_class_declares_one(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    calls = _calls(temp_repo, mock_ingestor)
    instantiates = edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
    assert (_RUN, f"{_SRC}.shop.Cart.Cart") in instantiates
    assert (_RUN, f"{_SRC}.shop.Cart.Cart.this") in calls
    # `Bag` declares no `def this`: the construction only instantiates.
    bag = f"{_SRC}.app.App.App.bag"
    assert (bag, f"{_SRC}.shop.Cart.Bag") in instantiates
    assert not any(source == bag for source, _ in calls)


def test_new_and_infix_calls_are_named_by_their_own_shapes(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "proj", mock_ingestor, {"src/a.scala": "object A\n"})
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.SCALA)
    root = parse(
        cs.SupportedLanguage.SCALA,
        "object U { val a = new Box[Int](1); val b = xs map f; val c = o.done }\n",
    )
    assert (
        strategy.node_call_name(first(root, cs.TS_SCALA_INSTANCE_EXPRESSION)) == "Box"
    )
    assert strategy.node_call_name(first(root, cs.TS_SCALA_INFIX_EXPRESSION)) == "map"
    # A bare selection may be a `val` read: it carries no call name.
    assert strategy.node_call_name(first(root, cs.TS_SCALA_FIELD_EXPRESSION)) is None
