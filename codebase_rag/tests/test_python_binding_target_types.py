"""Python binding targets that never typed their target (issue #3340).

Three binding forms left their name untyped in the flat type map, so a
method call through it got no CALLS edge (or only the name-only guess when
the method name was unique):

- `with opened() as t`, where `opened` is a `@contextmanager` /
  `@asynccontextmanager` generator: the entered value is the generator's
  yield type, from the `Iterator[T]` / `Generator[T, ...]` return
  annotation, else from the `yield` expressions. A plain factory returning
  a manager instance keeps today's behaviour: the target is typed by the
  manager's `__enter__`, never by the factory's return type.
- `(t := expr)`: the walrus binds `t` to `expr`'s type, exactly as
  `t = expr` does, including in a comprehension filter, where the walrus
  leaks into the enclosing function (PEP 572) while the loop variable
  does not.
- `case Thing() as t`: the alias is the matched class.

Some assertions read the type map directly: the fallback can pick the
right method by coincidence, which would let an edge-level check pass
either way.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "proj"
THING_GO = f"{PROJECT}.m.Thing.go"
OTHER_GO = f"{PROJECT}.m.Other.go"
THING = f"{PROJECT}.m.Thing"
INNER = f"{PROJECT}.m.Inner"
INNER_GO = f"{PROJECT}.m.Inner.go"

SOURCE = (
    "from contextlib import asynccontextmanager, contextmanager\n"
    "from contextlib import contextmanager as cm_alias\n"
    "import contextlib\n"
    "from helpers import contextmanager as fake_contextmanager\n"
    "from typing import AsyncGenerator, AsyncIterator, Generator, Iterator\n"
    "\n"
    "\n"
    "class Thing:\n"
    "    def go(self):\n"
    "        return 2\n"
    "\n"
    "    def __enter__(self):\n"
    "        return self\n"
    "\n"
    "    def __exit__(self, *exc):\n"
    "        return False\n"
    "\n"
    "\n"
    "class Other:\n"
    "    def go(self):\n"
    "        return 1\n"
    "\n"
    "\n"
    "class Inner:\n"
    "    def go(self):\n"
    "        return 3\n"
    "\n"
    "\n"
    "class CustomCM:\n"
    "    def __enter__(self):\n"
    "        return Inner()\n"
    "\n"
    "    def __exit__(self, *exc):\n"
    "        return False\n"
    "\n"
    "\n"
    "def deco(fn):\n"
    "    return fn\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def opened() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@asynccontextmanager\n"
    "async def aopened() -> AsyncIterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def generated() -> Generator[Thing, None, None]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def unannotated():\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def mixed_yields(flag):\n"
    "    if flag:\n"
    "        yield Thing()\n"
    "    else:\n"
    "        yield Other()\n"
    "\n"
    "\n"
    "@cm_alias\n"
    "def aliased() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@contextlib.contextmanager\n"
    "def qualified() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@deco\n"
    "@contextmanager\n"
    "def stacked() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "def plain_gen() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "def make_cm() -> CustomCM:\n"
    "    return CustomCM()\n"
    "\n"
    "\n"
    "def plain_with():\n"
    "    with Thing() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def cm_with():\n"
    "    with opened() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "async def acm_with():\n"
    "    async with aopened() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def gen_with():\n"
    "    with generated() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def unannotated_with():\n"
    "    with unannotated() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def mixed_with(flag):\n"
    "    with mixed_yields(flag) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def aliased_with():\n"
    "    with aliased() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def qualified_with():\n"
    "    with qualified() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def stacked_with():\n"
    "    with stacked() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def plain_gen_with():\n"
    "    with plain_gen() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def factory_with():\n"
    "    with make_cm() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def walrus():\n"
    "    if (t := Thing()) is not None:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def walrus_while():\n"
    "    while (t := Thing()) is not None:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def walrus_and():\n"
    "    if (t := Thing()) and True:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def walrus_comp():\n"
    "    return [t.go() for x in [Thing()] if (t := x) is not None]\n"
    "\n"
    "\n"
    "def walrus_nested():\n"
    "    def inner():\n"
    "        if (t := Thing()) is not None:\n"
    "            return t.go()\n"
    "    inner()\n"
    "\n"
    "\n"
    "def matched(shape):\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_union(shape):\n"
    "    match shape:\n"
    "        case Thing() | Other() as u:\n"
    "            return u.go()\n"
    "\n"
    "\n"
    "def matched_bare(shape):\n"
    "    match shape:\n"
    "        case t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_wildcard(shape):\n"
    "    match shape:\n"
    "        case _ as w:\n"
    "            return w.go()\n"
    "\n"
    "\n"
    "class Holder:\n"
    "    @contextmanager\n"
    "    def release(self) -> Iterator[Thing]:\n"
    "        yield Thing()\n"
    "\n"
    "    def use_release(self):\n"
    "        with self.release() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def local_yield():\n"
    "    t = Thing()\n"
    "    yield t\n"
    "\n"
    "\n"
    "@contextmanager\n"
    'def quoted() -> Iterator["Thing"]:\n'
    "    yield Thing()\n"
    "\n"
    "\n"
    "@asynccontextmanager\n"
    "async def aopen_unann():\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@asynccontextmanager\n"
    "async def agenerated() -> AsyncGenerator[Thing, None]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@fake_contextmanager\n"
    "def fake_cm() -> Iterator[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "class BaseHolder:\n"
    "    @contextmanager\n"
    "    def release(self) -> Iterator[Thing]:\n"
    "        yield Thing()\n"
    "\n"
    "\n"
    "class SubHolder(BaseHolder):\n"
    "    def use_inherited_release(self):\n"
    "        with self.release() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def make_thing_cm() -> Thing:\n"
    "    return Thing()\n"
    "\n"
    "\n"
    "def local_yield_with():\n"
    "    with local_yield() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def quoted_with():\n"
    "    with quoted() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "async def aopen_unann_with():\n"
    "    async with aopen_unann() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "async def agenerated_with():\n"
    "    async with agenerated() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def fake_cm_with():\n"
    "    with fake_cm() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def walrus_lambda():\n"
    "    f = lambda: (t := Thing())\n"  # noqa: E731 - a lambda is the point here
    "    return f()\n"
    "\n"
    "\n"
    "def walrus_then_assign():\n"
    "    if (t := Thing()) is not None:\n"
    "        pass\n"
    "    t = Other()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "def retry_walrus():\n"
    "    mgr = make_thing_cm()\n"
    "    with mgr as s:\n"
    "        if (w := s) is not None:\n"
    "            return w.go()\n"
    "\n"
    "\n"
    "def matched_then_assign(shape):\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            pass\n"
    "    t = Other()\n"
    "    return t.go()\n"
)

HELPERS = "def contextmanager(fn):\n    return fn\n"


def _build(tmp_path: Path) -> GraphUpdater:
    repo = tmp_path / PROJECT
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "m.py").write_text(SOURCE, encoding="utf-8")
    (repo / "helpers.py").write_text(HELPERS, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    return updater


def _calls(tmp_path: Path) -> dict[str, dict[str, str]]:
    """{caller's simple name: {callee qn: resolution}} for `m.py`'s CALLS."""
    ingestor = _build(tmp_path).ingestor
    assert isinstance(ingestor, _StatefulIngestor)
    found: dict[str, dict[str, str]] = {}
    for edge in ingestor.keyed_edges:
        _src_label, src, rel, _dst_label, dst, _site = edge
        if rel != cs.RelationshipType.CALLS or not str(src).startswith(f"{PROJECT}.m."):
            continue
        resolution = ingestor.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)
        found.setdefault(str(src).rsplit(".", 1)[-1], {})[str(dst)] = str(resolution)
    return found


def _functions(node: Node) -> Iterator[Node]:
    for child in node.children:
        if child.type == cs.TS_PY_FUNCTION_DEFINITION:
            yield child
        yield from _functions(child)


def _local_types(tmp_path: Path, function: str) -> dict[str, str]:
    updater = _build(tmp_path)
    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(SOURCE.encode())
    node = next(
        candidate
        for candidate in _functions(tree.root_node)
        if (candidate.child_by_field_name("name").text or b"").decode() == function
    )
    engine = updater.factory.type_inference.python_type_inference
    return engine.build_local_variable_type_map(node, f"{PROJECT}.m")


EXACT = str(cs.EdgeResolution.EXACT)


# --- the issue's four functions ---------------------------------------------


def test_plain_with_is_the_control(tmp_path: Path) -> None:
    assert _calls(tmp_path)["plain_with"].get(THING_GO) == EXACT


def test_contextmanager_target_gets_the_yielded_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["cm_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_async_contextmanager_target_gets_the_yielded_type(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path)["acm_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_walrus_target_gets_the_assigned_expression_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["walrus"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls
    assert _local_types(tmp_path, "walrus").get("t") == "Thing"


def test_matched_target_gets_the_class(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["matched"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls
    assert _local_types(tmp_path, "matched").get("t") == THING


# --- context-manager factories: annotation forms and decorator spellings ----


def test_generator_annotation_yields_its_first_argument(tmp_path: Path) -> None:
    assert _calls(tmp_path)["gen_with"].get(THING_GO) == EXACT


def test_unannotated_contextmanager_falls_back_to_yield_values(
    tmp_path: Path,
) -> None:
    assert _calls(tmp_path)["unannotated_with"].get(THING_GO) == EXACT


def test_mixed_yield_types_leave_the_target_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["mixed_with"]
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_aliased_contextmanager_decorator(tmp_path: Path) -> None:
    assert _calls(tmp_path)["aliased_with"].get(THING_GO) == EXACT


def test_qualified_contextmanager_decorator(tmp_path: Path) -> None:
    assert _calls(tmp_path)["qualified_with"].get(THING_GO) == EXACT


def test_stacked_decorators_still_find_contextmanager(tmp_path: Path) -> None:
    assert _calls(tmp_path)["stacked_with"].get(THING_GO) == EXACT


def test_plain_generator_without_the_decorator_stays_untyped(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path)["plain_gen_with"]
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_plain_factory_keeps_the_enter_return_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["factory_with"]
    assert calls.get(INNER_GO) == EXACT
    assert _local_types(tmp_path, "factory_with").get("t") == INNER


# --- walrus: positions and scoping -------------------------------------------


def test_walrus_in_while_condition(tmp_path: Path) -> None:
    assert _calls(tmp_path)["walrus_while"].get(THING_GO) == EXACT


def test_walrus_under_a_short_circuit(tmp_path: Path) -> None:
    assert _calls(tmp_path)["walrus_and"].get(THING_GO) == EXACT


def test_walrus_in_a_comprehension_filter_leaks_to_the_enclosing_body(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path).get("walrus_comp", {})
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls
    assert _local_types(tmp_path, "walrus_comp").get("t") == "Thing"


def test_walrus_in_a_nested_def_does_not_leak_into_the_outer_body(
    tmp_path: Path,
) -> None:
    assert "t" not in _local_types(tmp_path, "walrus_nested")


# --- match captures -----------------------------------------------------------


def test_union_capture_with_two_classes_stays_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_union", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_bare_capture_stays_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_bare", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_wildcard_capture_stays_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_wildcard", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_contextmanager_method_on_self(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("use_release", {})
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_unannotated_contextmanager_yielding_a_local(tmp_path: Path) -> None:
    assert _calls(tmp_path)["local_yield_with"].get(THING_GO) == EXACT


def test_quoted_type_argument_in_the_annotation(tmp_path: Path) -> None:
    assert _calls(tmp_path)["quoted_with"].get(THING_GO) == EXACT


def test_unannotated_async_contextmanager(tmp_path: Path) -> None:
    assert _calls(tmp_path)["aopen_unann_with"].get(THING_GO) == EXACT


def test_async_generator_annotation(tmp_path: Path) -> None:
    assert _calls(tmp_path)["agenerated_with"].get(THING_GO) == EXACT


def test_decorator_named_contextmanager_from_another_module_stays_untyped(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path).get("fake_cm_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_inherited_contextmanager_method_on_self(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("use_inherited_release", {})
    assert calls.get(THING_GO) == EXACT


def test_walrus_in_a_lambda_does_not_leak_into_the_enclosing_body(
    tmp_path: Path,
) -> None:
    assert "t" not in _local_types(tmp_path, "walrus_lambda")


def test_walrus_then_assignment_keeps_the_later_binding(tmp_path: Path) -> None:
    assert _local_types(tmp_path, "walrus_then_assign").get("t") == "Other"


def test_walrus_reading_a_retried_with_target(tmp_path: Path) -> None:
    assert _calls(tmp_path)["retry_walrus"].get(THING_GO) == EXACT


def test_match_alias_superseded_by_a_later_assignment(tmp_path: Path) -> None:
    assert _local_types(tmp_path, "matched_then_assign").get("t") == "Other"
