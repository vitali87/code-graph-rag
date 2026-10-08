"""Issue #2873: `Annotated[T, ...]` types a Python name as `T`.

`Annotated[X, metadata]` is `X` to a type checker (PEP 593), and it is the
core FastAPI/Pydantic idiom (`Annotated[Session, Depends(get_db)]`), yet the
annotation was kept verbatim as the type: `t: Annotated[Target, Doc("x")]`
left `t.handler()` with no edge (FastAPI's `APIRouter._contains_router` read
as dead). The metadata's `Doc("x")` was also recorded as a call the method
makes, though an annotation runs, if at all, when the `def` does.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SOURCE = """from typing import Annotated
import typing


class Doc:
    def __init__(self, text):
        self.text = text


class Target:
    def handler(self):
        return 1


def build():
    return 0


REGISTRY = {}


def lookup(key):
    return REGISTRY[key]


class User:
    def by_direct(self, t: Target):
        return t.handler()

    def by_annotated(self, t: Annotated[Target, Doc("x")]):
        return t.handler()

    def by_annotated_str(self, t: Annotated["Target", Doc("x")]):
        return t.handler()

    def by_typing(self, t: typing.Annotated[Target, Doc("x")]):
        return t.handler()

    def by_nested(self, t: Annotated[Annotated[Target, Doc("a")], Doc("b")]):
        return t.handler()

    def by_default(self, t: Annotated[Target, Doc("x")] = None):
        return t.handler()

    def made(self) -> Annotated[Target, Doc("r")]:
        return lookup("target")

    def via_return(self):
        made = self.made()
        return made.handler()

    @staticmethod
    def by_static(t: Annotated[Target, Doc("s")]):
        return t.handler()

    def local(self):
        v: Annotated[Target, Doc("v")] = Target()
        return v.handler()

    def with_default(self, x=build()):
        return x


class Model:
    dep: Annotated[Target, Doc("f")]

    def via_field(self):
        return self.dep.handler()


def fn(t: Annotated[Target, Doc("f")]):
    return t.handler()


def outer():
    def inner(t: Annotated[Target, Doc("n")]):
        return t.handler()

    return inner
"""

# `def __enter__(self: T) -> T`, the pre-3.11 self-type idiom, with both
# annotations wrapped in `Annotated`: the two must still read as one `T`
# (Greptile, PR #2955), for `__aenter__` too.
CONTEXT = """from typing import Annotated, TypeVar

T = TypeVar("T", bound="Session")


class Session:
    def __enter__(self: Annotated[T, "tag"]) -> Annotated[T, "tag"]:
        return self

    def __exit__(self, *exc):
        return None

    async def __aenter__(self: Annotated[T, "tag"]) -> Annotated[T, "tag"]:
        return self

    async def __aexit__(self, *exc):
        return None

    def run(self):
        return 1


def use():
    with Session() as session:
        session.run()


async def use_async():
    async with Session() as session:
        session.run()
"""

HANDLER = "m.Target.handler"
DOC_INIT = "m.Doc.__init__"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("annotated") / "ann"
    _write(root, "m.py", SOURCE)
    _write(root, "ctx.py", CONTEXT)
    return _index(root, MagicMock())


def _edges(graph: RecordedGraph, caller: str, rel: str = "CALLS") -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, kind, dst, props in graph.edges
        if kind == rel and src == f"{prefix}{caller}"
    }


@pytest.mark.parametrize("caller", ["ctx.use", "ctx.use_async"])
def test_an_annotated_self_type_enter_types_the_with_target(
    graph: RecordedGraph, caller: str
) -> None:
    assert _edges(graph, caller).get("ctx.Session.run") == "exact"


@pytest.mark.parametrize(
    "caller",
    [
        "m.User.by_annotated",
        "m.User.by_annotated_str",
        "m.User.by_typing",
        "m.User.by_nested",
        "m.User.by_default",
        "m.User.via_return",
        "m.User.by_static",
        "m.Model.via_field",
        "m.fn",
        "m.outer.inner",
    ],
)
def test_a_call_on_an_annotated_name_binds_the_annotated_type(
    graph: RecordedGraph, caller: str
) -> None:
    assert _edges(graph, caller).get(HANDLER) == "exact"


@pytest.mark.parametrize(
    "caller",
    [
        "m.User.by_annotated",
        "m.User.by_typing",
        "m.User.by_default",
        "m.User.made",
        "m.User.by_static",
        "m.User.local",
        "m.fn",
        "m.outer.inner",
    ],
)
def test_annotation_metadata_is_not_a_call_the_function_makes(
    graph: RecordedGraph, caller: str
) -> None:
    assert DOC_INIT not in _edges(graph, caller)
    assert "m.Doc" not in _edges(graph, caller, "INSTANTIATES")


# Negative: what must not change.


def test_a_plain_annotation_still_binds(graph: RecordedGraph) -> None:
    assert _edges(graph, "m.User.by_direct") == {HANDLER: "exact"}


def test_the_def_time_evaluation_stays_with_the_enclosing_scope(
    graph: RecordedGraph,
) -> None:
    # A signature's annotations run when the `def` runs: at import for a
    # top-level or class-body `def`, and when `outer` runs for `inner`.
    assert DOC_INIT in _edges(graph, "m")
    assert DOC_INIT in _edges(graph, "m.outer")


def test_a_default_value_call_is_left_as_it_was(graph: RecordedGraph) -> None:
    assert "m.build" in _edges(graph, "m.User.with_default")
