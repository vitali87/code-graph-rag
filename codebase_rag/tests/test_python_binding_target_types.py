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
HTHING_GO = f"{PROJECT}.helpers.HThing.go"
PTHING_GO = f"{PROJECT}.pkg.contexts.PThing.go"
THING = f"{PROJECT}.m.Thing"
INNER = f"{PROJECT}.m.Inner"
INNER_GO = f"{PROJECT}.m.Inner.go"

SOURCE = (
    "from contextlib import asynccontextmanager, contextmanager\n"
    "from contextlib import contextmanager as cm_alias\n"
    "import contextlib\n"
    "from helpers import contextmanager as fake_contextmanager\n"
    "import helpers\n"
    "from helpers import HBase\n"
    "from helpers import Iterator as HIter\n"
    "from typing import AsyncGenerator, AsyncIterator, Generator, Iterator\n"
    "from typing import Iterator as It\n"
    "import collections.abc\n"
    "import collections.abc as cabc\n"
    "import typing\n"
    "import pkg.contexts\n"
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
    "\n"
    "\n"
    "@contextmanager\n"
    "def aliased_ann() -> It[Thing]:\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def multi_same():\n"
    "    yield Thing()\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "def aliased_ann_with():\n"
    "    with aliased_ann() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def multi_same_with():\n"
    "    with multi_same() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def matched_qualified(shape):\n"
    "    match shape:\n"
    "        case helpers.HThing() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_bare_typed():\n"
    "    s = Thing()\n"
    "    match s:\n"
    "        case t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "class CrossSub(HBase):\n"
    "    def use_cross_release(self):\n"
    "        with self.release() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_alias_then_unknown(shape):\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            pass\n"
    "        case Unknown() as t:\n"
    "            pass\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "def matched_bare_direct():\n"
    "    match Thing():\n"
    "        case t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def weird_ann(x) -> HIter[Thing]:\n"
    "    yield x\n"
    "\n"
    "\n"
    "def weird_ann_with():\n"
    "    with weird_ann(Thing()) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def rebound_factory():\n"
    "    t = Other()\n"
    "    t = Thing()\n"
    "    yield t\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def stale_rebind_factory():\n"
    "    t = Thing()\n"
    "    t = opaque()\n"
    "    yield t\n"
    "\n"
    "\n"
    "def stale_rebind_with():\n"
    "    with stale_rebind_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def annotated_factory():\n"
    "    t = Thing()\n"
    "    t: Thing\n"
    "    yield t\n"
    "\n"
    "\n"
    "def annotated_factory_with():\n"
    "    with annotated_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def yield_before_binding():\n"
    "    yield t\n"
    "    t = Thing()\n"
    "\n"
    "\n"
    "def rebound_factory_with():\n"
    "    with rebound_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def yield_before_binding_with():\n"
    "    with yield_before_binding() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def comprehension_walrus_shadow():\n"
    "    x = Other()\n"
    "    return [t.go() for x in [Thing()] if (t := x) is not None]\n"
    "\n"
    "\n"
    "def comprehension_walrus_then_rebound():\n"
    "    ys = [t.go() for x in [Thing()] if (t := x) is not None]\n"
    "    t = Other()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "def comprehension_walrus_then_match_alias(shape):\n"
    "    ys = [t.go() for x in [Thing()] if (t := x) is not None]\n"
    "    match shape:\n"
    "        case Other() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_bare_subject_rebound_after_match():\n"
    "    s = Thing()\n"
    "    match s:\n"
    "        case t:\n"
    "            pass\n"
    "    s = Other()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def dotted_abc_iterator(x) -> collections.abc.Iterator[Thing]:\n"
    "    yield x\n"
    "\n"
    "\n"
    "def dotted_abc_iterator_with():\n"
    "    with dotted_abc_iterator(Thing()) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def typing_iterator(x) -> typing.Iterator[Thing]:\n"
    "    yield x\n"
    "\n"
    "\n"
    "def typing_iterator_with():\n"
    "    with typing_iterator(Thing()) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@asynccontextmanager\n"
    "async def dotted_abc_async_iterator(x) -> collections.abc.AsyncIterator[Thing]:\n"
    "    yield x\n"
    "\n"
    "\n"
    "async def dotted_abc_async_iterator_with():\n"
    "    async with dotted_abc_async_iterator(Thing()) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def aliased_abc_iterator(x) -> cabc.Iterator[Thing]:\n"
    "    yield x\n"
    "\n"
    "\n"
    "def aliased_abc_iterator_with():\n"
    "    with aliased_abc_iterator(Thing()) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def branch_rebound_factory(flag):\n"
    "    t = Thing()\n"
    "    if flag:\n"
    "        t = Other()\n"
    "    yield t\n"
    "\n"
    "\n"
    "def branch_rebound_factory_with(flag):\n"
    "    with branch_rebound_factory(flag) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def branch_local_factory(flag):\n"
    "    t = Other()\n"
    "    if flag:\n"
    "        t = Thing()\n"
    "        yield t\n"
    "    else:\n"
    "        yield Thing()\n"
    "\n"
    "\n"
    "def branch_local_factory_with(flag):\n"
    "    with branch_local_factory(flag) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def short_circuit_walrus_factory(flag):\n"
    "    t = Thing()\n"
    "    if flag and (t := Other()):\n"
    "        pass\n"
    "    yield t\n"
    "\n"
    "\n"
    "def short_circuit_walrus_factory_with(flag):\n"
    "    with short_circuit_walrus_factory(flag) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def shadowed_factory():\n"
    "    yield Thing()\n"
    "\n"
    "\n"
    "def shadowed_factory_with():\n"
    "    with shadowed_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def module_factory_with():\n"
    "    with helpers.h_opened() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def locally_defined_factory_with():\n"
    "    @contextmanager\n"
    "    def shadowed_factory():\n"
    "        yield Other()\n"
    "\n"
    "    with shadowed_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def parameter_factory_with(shadowed_factory):\n"
    "    with shadowed_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def shadowed_module_factory_with(helpers):\n"
    "    with helpers.h_opened() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def matched_rebound_class(shape):\n"
    "    Thing = Other\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_parameter_class(shape, Thing):\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def dotted_package_factory_with():\n"
    "    with pkg.contexts.p_opened() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def matched_unresolved_after_binding(shape, Pattern):\n"
    "    t = Thing()\n"
    "    match shape:\n"
    "        case Pattern() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_union_after_binding(shape):\n"
    "    t = Thing()\n"
    "    match shape:\n"
    "        case Thing() | Other() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_capture_after_binding(shape):\n"
    "    t = Thing()\n"
    "    match shape:\n"
    "        case t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def matched_two_aliases(shape):\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            return t.go()\n"
    "        case Other() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def loop_rebound_factory(xs):\n"
    "    t = Thing()\n"
    "    for t in xs:\n"
    "        pass\n"
    "    yield t\n"
    "\n"
    "\n"
    "def loop_rebound_factory_with(xs):\n"
    "    with loop_rebound_factory(xs) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def unpacked_factory(pair):\n"
    "    t = Thing()\n"
    "    t, u = pair\n"
    "    yield t\n"
    "\n"
    "\n"
    "def unpacked_factory_with(pair):\n"
    "    with unpacked_factory(pair) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def loop_then_bound_factory(xs):\n"
    "    for t in xs:\n"
    "        pass\n"
    "    t = Thing()\n"
    "    yield t\n"
    "\n"
    "\n"
    "def loop_then_bound_factory_with(xs):\n"
    "    with loop_then_bound_factory(xs) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def yields_then_returns():\n"
    "    yield Thing()\n"
    "    return Other()\n"
    "\n"
    "\n"
    "def yields_then_returns_with():\n"
    "    with yields_then_returns() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def copied_local_factory():\n"
    "    original = Thing()\n"
    "    resource = original\n"
    "    yield resource\n"
    "\n"
    "\n"
    "def copied_local_factory_with():\n"
    "    with copied_local_factory() as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def nested_capture_of_an_alias(shape):\n"
    "    t = Other()\n"
    "    match shape:\n"
    "        case Thing() as t:\n"
    "            match t:\n"
    "                case u:\n"
    "                    return u.go()\n"
    "\n"
    "\n"
    "def comprehension_walrus_then_unknown(opaque):\n"
    "    ys = [x for x in [Thing()] if (t := x) is not None]\n"
    "    t = opaque()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "def nonlocal_walrus_rebinding():\n"
    "    t = Thing()\n"
    "\n"
    "    def inner():\n"
    "        nonlocal t\n"
    "        if (t := Other()):\n"
    "            pass\n"
    "\n"
    "    inner()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def parameter_branch_factory(t, flag):\n"
    "    if flag:\n"
    "        t = Thing()\n"
    "    yield t\n"
    "\n"
    "\n"
    "def parameter_branch_factory_with(x, flag):\n"
    "    with parameter_branch_factory(x, flag) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def elif_walrus_factory(a):\n"
    "    t = Thing()\n"
    "    if a:\n"
    "        pass\n"
    "    elif (t := Other()):\n"
    "        pass\n"
    "    yield t\n"
    "\n"
    "\n"
    "def elif_walrus_factory_with(a):\n"
    "    with elif_walrus_factory(a) as t:\n"
    "        return t.go()\n"
    "\n"
    "\n"
    "def annotated_then_union_alias(shape):\n"
    "    t: Thing = Thing()\n"
    "    match shape:\n"
    "        case Thing() | Other() as t:\n"
    "            return t.go()\n"
    "\n"
    "\n"
    "def nonlocal_walrus_copying_an_inner_local():\n"
    "    t = Other()\n"
    "    x = Thing()\n"
    "\n"
    "    def inner():\n"
    "        nonlocal t\n"
    "        x = Other()\n"
    "        if (t := x):\n"
    "            pass\n"
    "\n"
    "    inner()\n"
    "    return t.go()\n"
    "\n"
    "\n"
    "def matched_same_alias_twice(shape):\n"
    "    match shape:\n"
    "        case Thing(x=1) as t:\n"
    "            return t.go()\n"
    "        case Thing() as t:\n"
    "            return t.go()\n"
)

PKG_CONTEXTS = (
    "from contextlib import contextmanager\n"
    "\n"
    "\n"
    "class PThing:\n"
    "    def go(self):\n"
    "        return 4\n"
    "\n"
    "\n"
    "@contextmanager\n"
    "def p_opened():\n"
    "    yield PThing()\n"
)

CHILD = (
    "from helpers import HBase\n"
    "\n"
    "\n"
    "class ChildSub(HBase):\n"
    "    def use_child_release(self):\n"
    "        with self.release() as t:\n"
    "            return t.go()\n"
)

HELPERS = (
    "from contextlib import contextmanager as cm\n"
    "from typing import Iterator\n"
    "\n"
    "\n"
    "def contextmanager(fn):\n"
    "    return fn\n"
    "\n"
    "\n"
    "class Iterator:\n"
    "    pass\n"
    "\n"
    "\n"
    "class HThing:\n"
    "    def go(self):\n"
    "        return 1\n"
    "\n"
    "\n"
    "class HBase:\n"
    "    @cm\n"
    "    def release(self) -> Iterator[HThing]:\n"
    "        yield HThing()\n"
    "\n"
    "\n"
    "@cm\n"
    "def h_opened():\n"
    "    yield HThing()\n"
)


def _build(tmp_path: Path) -> GraphUpdater:
    repo = tmp_path / PROJECT
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "m.py").write_text(SOURCE, encoding="utf-8")
    (repo / "helpers.py").write_text(HELPERS, encoding="utf-8")
    (repo / "child.py").write_text(CHILD, encoding="utf-8")
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "contexts.py").write_text(PKG_CONTEXTS, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=_StatefulIngestor(), repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run(force=True)
    return updater


def _calls(tmp_path: Path) -> dict[str, dict[str, str]]:
    """{caller's simple name: {callee qn: resolution}} for the CALLS from
    `m.py` and the `child.py` cross-module fixture."""
    ingestor = _build(tmp_path).ingestor
    assert isinstance(ingestor, _StatefulIngestor)
    found: dict[str, dict[str, str]] = {}
    for edge in ingestor.keyed_edges:
        _src_label, src, rel, _dst_label, dst, _site = edge
        if rel != cs.RelationshipType.CALLS or not str(src).startswith(
            (f"{PROJECT}.m.", f"{PROJECT}.child.")
        ):
            continue
        resolution = ingestor.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)
        found.setdefault(str(src).rsplit(".", 1)[-1], {})[str(dst)] = str(resolution)
    return found


def _calls_at(tmp_path: Path, caller: str, statement: str) -> dict[str, str]:
    """{callee qn: resolution} for the CALLS `caller` in `m.py` makes on
    the first line after its `def` that reads exactly `statement`: the
    call at one site, where `_calls` merges every call the caller makes
    (CodeRabbit review on #3585)."""
    lines = SOURCE.splitlines()
    start = next(i for i, text in enumerate(lines) if text.startswith(f"def {caller}("))
    line = next(i for i in range(start, len(lines)) if lines[i].strip() == statement)
    ingestor = _build(tmp_path).ingestor
    assert isinstance(ingestor, _StatefulIngestor)
    found: dict[str, str] = {}
    for edge in ingestor.keyed_edges:
        _src_label, src, rel, _dst_label, dst, site = edge
        if (
            rel != cs.RelationshipType.CALLS
            or str(src) != f"{PROJECT}.m.{caller}"
            or dict(site or ()).get("line") != line + 1
        ):
            continue
        resolution = ingestor.edge_props.get(edge, {}).get(cs.KEY_RESOLUTION)
        found[str(dst)] = str(resolution)
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


def test_aliased_generator_annotation(tmp_path: Path) -> None:
    assert _calls(tmp_path)["aliased_ann_with"].get(THING_GO) == EXACT


def test_unannotated_contextmanager_yielding_the_same_class_twice(
    tmp_path: Path,
) -> None:
    assert _calls(tmp_path)["multi_same_with"].get(THING_GO) == EXACT


def test_qualified_class_pattern(tmp_path: Path) -> None:
    assert _calls(tmp_path)["matched_qualified"].get(HTHING_GO) == EXACT


def test_bare_capture_takes_the_subject_type(tmp_path: Path) -> None:
    assert _calls(tmp_path)["matched_bare_typed"].get(THING_GO) == EXACT


def test_cross_module_inherited_contextmanager_method(tmp_path: Path) -> None:
    assert _calls(tmp_path)["use_cross_release"].get(HTHING_GO) == EXACT


def test_later_untypable_alias_supersedes_an_earlier_typed_one(
    tmp_path: Path,
) -> None:
    assert "t" not in _local_types(tmp_path, "matched_alias_then_unknown")
    assert _calls(tmp_path).get("matched_alias_then_unknown", {}).get(THING_GO) != EXACT


def test_bare_capture_on_a_direct_constructor_subject(
    tmp_path: Path,
) -> None:
    assert _calls(tmp_path)["matched_bare_direct"].get(THING_GO) == EXACT


def test_annotation_from_another_module_is_not_a_generator_annotation(
    tmp_path: Path,
) -> None:
    assert _calls(tmp_path)["weird_ann_with"].get(THING_GO) != EXACT


def test_factory_rebinding_uses_the_last_binding(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["rebound_factory_with"]
    assert calls.get(THING_GO) == EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_yield_before_its_binding_stays_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("yield_before_binding_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_comprehension_walrus_reads_the_loop_variable(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("comprehension_walrus_shadow", {})
    assert calls.get(THING_GO) == EXACT
    assert calls.get(OTHER_GO) != EXACT
    assert _local_types(tmp_path, "comprehension_walrus_shadow").get("t") == "Thing"


def test_child_module_inherited_contextmanager_without_its_own_import(
    tmp_path: Path,
) -> None:
    assert _calls(tmp_path)["use_child_release"].get(HTHING_GO) == EXACT


def test_comprehension_walrus_yielded_to_a_later_rebinding(tmp_path: Path) -> None:
    """A loop-derived walrus write must not clobber a later rebinding of
    the same name: the comprehension pass runs after the assignment
    passes, so `t = Other()` is already in the map when the deferred
    write happens (CodeRabbit follow-up on #3528)."""
    calls = _calls_at(tmp_path, "comprehension_walrus_then_rebound", "return t.go()")
    assert calls.get(OTHER_GO) == EXACT
    assert THING_GO not in calls
    assert (
        _local_types(tmp_path, "comprehension_walrus_then_rebound").get("t") == "Other"
    )


def test_unreadable_rebinding_clears_the_factory_local_type(
    tmp_path: Path,
) -> None:
    """`t = Thing(); t = opaque(); yield t` yields an unknown value, not
    Thing: the unreadable rebinding must clear the stale type instead
    of leaving it in the factory's local map (CodeRabbit follow-up on
    #3528)."""
    calls = _calls(tmp_path).get("stale_rebind_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT
    assert _local_types(tmp_path, "stale_rebind_with").get("t") is None


def test_bare_annotation_does_not_clear_the_factory_local_type(
    tmp_path: Path,
) -> None:
    """`t: Thing` declares a type without rebinding: it must not clear
    the `Thing` a factory bound before it (Antigravity review of the
    stale-rebinding fix)."""
    calls = _calls(tmp_path).get("annotated_factory_with", {})
    assert calls.get(THING_GO) == EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_comprehension_walrus_yielded_to_a_later_match_alias(
    tmp_path: Path,
) -> None:
    """A loop-derived walrus write must not clobber a later `case ... as`
    alias either: pattern aliases are absent from `_bindings_in`, so
    the last-binding census has to merge them (Antigravity review)."""
    calls = _calls_at(
        tmp_path, "comprehension_walrus_then_match_alias", "return t.go()"
    )
    assert calls.get(OTHER_GO) == EXACT
    assert THING_GO not in calls
    assert (
        _local_types(tmp_path, "comprehension_walrus_then_match_alias").get("t")
        == f"{PROJECT}.m.Other"
    )


def test_bare_capture_subject_rebound_after_match_stays_untyped(
    tmp_path: Path,
) -> None:
    """Greptile P1 on #3528: `s` is rebound after the `match`, so the
    flat map already holds the later type when the capture is typed.
    The capture stays untyped rather than taking the later type: the
    map holds one type per name and this pass runs after all the
    assignment passes, so the type at the `match` cannot be recovered
    from it, and a wrong exact edge is worse than none."""
    calls = _calls(tmp_path).get("matched_bare_subject_rebound_after_match", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT
    assert (
        _local_types(tmp_path, "matched_bare_subject_rebound_after_match").get("t")
        is None
    )


def test_dotted_collections_abc_iterator_annotation(tmp_path: Path) -> None:
    """Greptile P1 on #3528: `import collections.abc` maps `collections`
    to `collections.abc`, so `collections.abc.Iterator[Thing]` must
    resolve to the generator annotation, not `collections.abc.abc.Iterator`.
    The factory yields a parameter, so the annotation is the only type
    source: rejecting it leaves the target untyped."""
    calls = _calls(tmp_path)["dotted_abc_iterator_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_qualified_typing_iterator_annotation(tmp_path: Path) -> None:
    """`typing.Iterator[Thing]` with `import typing` keeps resolving:
    the dotted-import normalization must not break the plain qualified
    spelling."""
    calls = _calls(tmp_path)["typing_iterator_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_dotted_collections_abc_async_iterator_annotation(
    tmp_path: Path,
) -> None:
    """`collections.abc.AsyncIterator[Thing]` resolves the same way as
    the `Iterator` spelling."""
    calls = _calls(tmp_path)["dotted_abc_async_iterator_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_aliased_collections_abc_iterator_annotation(tmp_path: Path) -> None:
    """`import collections.abc as cabc` plus `cabc.Iterator[Thing]`
    keeps resolving: the alias maps straight to the module, with no
    duplicated segment to strip."""
    calls = _calls(tmp_path)["aliased_abc_iterator_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


# --- reviews on #3585 --------------------------------------------------------


def test_factory_branch_rebinding_leaves_the_target_untyped(
    tmp_path: Path,
) -> None:
    """Greptile P1 on #3585: `t = Thing(); if flag: t = Other(); yield t`
    yields `Thing` or `Other` depending on `flag`. A binding inside a
    branch that does not hold the yield may not run before it, so it
    adds a possible type instead of replacing the earlier one, and two
    possible types name no single class."""
    calls = _calls(tmp_path).get("branch_rebound_factory_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_factory_binding_in_the_yields_own_branch_still_types(
    tmp_path: Path,
) -> None:
    """A binding in the same block as the yield always runs before it,
    so it still replaces the earlier `t = Other()`: both yields give
    `Thing`."""
    calls = _calls(tmp_path).get("branch_local_factory_with", {})
    assert calls.get(THING_GO) == EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_factory_short_circuit_walrus_leaves_the_target_untyped(
    tmp_path: Path,
) -> None:
    """A walrus behind `and` runs only when the left operand is true, so
    it may not rebind `t` before the yield either."""
    calls = _calls(tmp_path).get("short_circuit_walrus_factory_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_module_level_factory_is_the_control(tmp_path: Path) -> None:
    calls = _calls(tmp_path)
    assert calls["shadowed_factory_with"].get(THING_GO) == EXACT
    assert calls["module_factory_with"].get(HTHING_GO) == EXACT


def test_local_factory_does_not_resolve_to_the_module_one(tmp_path: Path) -> None:
    """Greptile P1 on #3585: a factory the caller defines itself shadows
    the module's same-named one. Local factories are not followed, so
    the target stays untyped rather than taking the module factory's
    yield type."""
    calls = _calls(tmp_path).get("locally_defined_factory_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_parameter_named_like_a_module_factory_stays_untyped(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path).get("parameter_factory_with", {})
    assert calls.get(THING_GO) != EXACT


def test_parameter_named_like_an_imported_module_stays_untyped(
    tmp_path: Path,
) -> None:
    calls = _calls(tmp_path).get("shadowed_module_factory_with", {})
    assert calls.get(HTHING_GO) != EXACT


def test_match_class_rebound_in_the_function_stays_untyped(
    tmp_path: Path,
) -> None:
    """Greptile P1 on #3585: `Thing = Other` in the function makes
    `case Thing()` match `Other`, so the alias must not take the
    module's `Thing`."""
    calls = _calls(tmp_path).get("matched_rebound_class", {})
    assert calls.get(THING_GO) != EXACT
    assert _local_types(tmp_path, "matched_rebound_class").get("t") != THING


def test_match_class_parameter_stays_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_parameter_class", {})
    assert calls.get(THING_GO) != EXACT
    assert _local_types(tmp_path, "matched_parameter_class").get("t") != THING


def test_dotted_package_import_factory(tmp_path: Path) -> None:
    """Codex review on #3585: `import pkg.contexts` records `pkg` as
    `proj.pkg.contexts`, so appending `contexts.p_opened` to it doubled
    the segment and missed the factory."""
    assert _calls(tmp_path)["dotted_package_factory_with"].get(PTHING_GO) == EXACT


def test_unresolved_alias_drops_the_earlier_type(tmp_path: Path) -> None:
    """Codex review on #3585: `t = Thing()` then `case Pattern() as t`
    with an unresolvable `Pattern` must not leave `t` typed as `Thing`
    for the case body."""
    calls = _calls(tmp_path).get("matched_unresolved_after_binding", {})
    assert calls.get(THING_GO) != EXACT
    assert "t" not in _local_types(tmp_path, "matched_unresolved_after_binding")


def test_union_alias_drops_the_earlier_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_union_after_binding", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_untyped_capture_drops_the_earlier_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("matched_capture_after_binding", {})
    assert calls.get(THING_GO) != EXACT
    assert "t" not in _local_types(tmp_path, "matched_capture_after_binding")


def test_aliases_of_different_classes_leave_the_name_untyped(
    tmp_path: Path,
) -> None:
    """The first case body reads the flat map too, so typing `t` as the
    last alias's `Other` would give it a wrong exact edge."""
    calls = _calls(tmp_path).get("matched_two_aliases", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_aliases_of_the_same_class_still_type(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["matched_same_alias_twice"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_factory_loop_rebinding_leaves_the_target_untyped(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: `for t in xs` rebinds the factory's
    `t` after `t = Thing()`, so the yield is not known to be a Thing."""
    calls = _calls(tmp_path).get("loop_rebound_factory_with", {})
    assert calls.get(THING_GO) != EXACT


def test_factory_unpacking_leaves_the_target_untyped(tmp_path: Path) -> None:
    calls = _calls(tmp_path).get("unpacked_factory_with", {})
    assert calls.get(THING_GO) != EXACT


def test_factory_binding_after_a_loop_still_types(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["loop_then_bound_factory_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_decorated_factory_types_by_yield_not_return(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: a value a `@contextmanager` generator
    returns after its yield is not what `with` hands the target."""
    calls = _calls(tmp_path)["yields_then_returns_with"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_factory_yielding_a_copied_local(tmp_path: Path) -> None:
    calls = _calls(tmp_path)["copied_local_factory_with"]
    assert calls.get(THING_GO) == EXACT


def test_nested_capture_reads_the_alias_type(tmp_path: Path) -> None:
    """Greptile P1 on #3585: a nested `match t: case u:` in a
    `case Thing() as t` body must read `t` as `Thing`, not as the
    `Other` an earlier binding left in the map."""
    calls = _calls(tmp_path)["nested_capture_of_an_alias"]
    assert calls.get(THING_GO) == EXACT
    assert OTHER_GO not in calls


def test_comprehension_walrus_then_unknown_rebinding(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: `t = opaque()` after the comprehension
    supersedes its `(t := x)`, so the final `t.go()` must not take the
    loop variable's `Thing`."""
    calls = _calls(tmp_path).get("comprehension_walrus_then_unknown", {})
    assert calls.get(THING_GO) != EXACT
    assert "t" not in _local_types(tmp_path, "comprehension_walrus_then_unknown")


def test_nonlocal_walrus_rebinds_the_enclosing_name(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: a walrus in a nested def that declares
    `t` nonlocal rebinds the enclosing `t`, the way `t = Other()` there
    does, so the later `t.go()` is `Other.go`."""
    calls = _calls(tmp_path)["nonlocal_walrus_rebinding"]
    assert calls.get(OTHER_GO) == EXACT
    assert THING_GO not in calls


def test_factory_parameter_bound_in_a_branch_stays_untyped(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: `if flag: t = Thing()` may be skipped,
    and then the factory yields its parameter `t`, whose type is unknown,
    so the target must not be typed as `Thing`."""
    calls = _calls(tmp_path).get("parameter_branch_factory_with", {})
    assert calls.get(THING_GO) != EXACT


def test_factory_elif_walrus_may_not_run(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: an `elif` condition runs only when
    the `if` before it fails, so its walrus may not rebind `t`."""
    calls = _calls(tmp_path).get("elif_walrus_factory_with", {})
    assert calls.get(THING_GO) != EXACT
    assert calls.get(OTHER_GO) != EXACT


def test_annotation_does_not_restore_a_dropped_alias_type(tmp_path: Path) -> None:
    """CodeRabbit review on #3585: `t: Thing = ...` then a union alias
    `case Thing() | Other() as t` leaves `t` untyped; the annotation pass
    must not type it back as `Thing`."""
    calls = _calls(tmp_path).get("annotated_then_union_alias", {})
    assert calls.get(THING_GO) != EXACT
    assert "t" not in _local_types(tmp_path, "annotated_then_union_alias")


def test_nonlocal_walrus_does_not_read_the_outer_scope(tmp_path: Path) -> None:
    """Greptile P1 on #3585: `(t := x)` in a nested def reads the nested
    def's own `x` (an `Other`), so the enclosing map must not copy the
    enclosing `x`'s `Thing` into `t`."""
    calls = _calls(tmp_path).get("nonlocal_walrus_copying_an_inner_local", {})
    assert calls.get(THING_GO) != EXACT
