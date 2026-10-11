# Every hop of a fluent chain binds in TypeScript and Python (issue #2468).
#
# C#, Rust and Java type a chain hop from the previous hop's declared return
# (#1334): `FBox.Make().With(1).Bump()` binds all three. TypeScript typed no
# hop from a call's return at all, so `newBox().with(1).bump()` bound only
# `newBox`. Python typed a free function's return but not a method's, so
# `new_box().with_(1).with_(2).bump()` stopped after the first `with_`, and
# `QBox().w_str().bump()` lost `bump` whether `w_str` was annotated `-> "QBox"`,
# `-> QBox` under PEP 563, or `-> Self`.
#
# The negative tests pin what must not move: a hop whose return is unknown
# ends the chain instead of guessing, a same-named method on an unrelated
# class (or a same-named class in another module) is never bound, and the
# single-hop calls that already resolved still resolve the same way.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_EXACT = cs.EdgeResolution.EXACT

_TS_BOX = """\
export class Decoy {
  with(n: number): Decoy { return this; }
  self(): this { return this; }
  plain(n: number) { return this; }
  bump(): number { return 0; }
  finish(): number { return 0; }
}

export class Child {
  finish(): number { return 1; }
}

export class TBox {
  n = 0;
  static create(): TBox { return new TBox(); }
  with(n: number): TBox { this.n = n; return this; }
  self(): this { return this; }
  plain(n: number) { this.n = n; return this; }
  maybe(): TBox | undefined { return this; }
  child(): Child { return new Child(); }
  opaque(): any { return this; }
  loose(n: number) { if (n) { return this; } return null; }
  bump(): number { return this.n + 1; }
}

export function newBox(): TBox { return new TBox(); }
export function mystery(): any { return new TBox(); }
"""

# A second class named `TBox` that callers of box.ts never see: a return
# type resolved by bare name instead of in the declaring module lands here.
_TS_OTHER = """\
export class TBox {
  with(n: number): TBox { return this; }
  bump(): number { return 2; }
}
"""

_TS_USE = """\
import { TBox, newBox, mystery } from './box';

export function builderNew(): number { return new TBox().with(1).with(2).bump(); }
export function builderStatic(): number { return TBox.create().with(1).bump(); }
export function viaThisType(): number { return new TBox().self().self().bump(); }
export function viaPlainThis(): number { return new TBox().plain(1).bump(); }
export function viaOptional(): number { return newBox().maybe()!.bump(); }
export function viaFactory(): number { return newBox().with(1).with(2).bump(); }
export function viaLocal(): number { const b = new TBox(); return b.with(1).bump(); }
export function crossType(): number { return newBox().child().finish(); }
export function unknownHop(): number { return newBox().opaque().bump(); }
export function mixedReturns(): number { return newBox().loose(1).bump(); }
export function unknownBase(): number { return mystery().with(1).bump(); }
export function singleNew(): number { return new TBox().bump(); }
export function singleLocal(): number { const b = new TBox(); return b.bump(); }
export function singleFactory(): TBox { return newBox(); }
"""

# Imports only the factory: the class it returns is not in scope here, so
# every hop's type has to come from box.ts, not from this file's names.
_TS_FACTORY_ONLY = """\
import { newBox } from './box';

export function factoryOnly(): number { return newBox().with(1).bump(); }
"""


def _calls(mock_ingestor: MagicMock) -> dict[tuple[str, str], str | None]:
    edges: dict[tuple[str, str], str | None] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        edges[(c.args[0][2], c.args[2][2])] = props.get(cs.KEY_RESOLUTION)
    return edges


def _callees(
    edges: dict[tuple[str, str], str | None], caller: str
) -> dict[str, str | None]:
    return {callee: res for (src, callee), res in edges.items() if src == caller}


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


@pytest.fixture
def ts_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "tsproj"
    _write(
        root,
        {
            "box.ts": _TS_BOX,
            "alt_box.ts": _TS_OTHER,
            "use.ts": _TS_USE,
            "factory_only.ts": _TS_FACTORY_ONLY,
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


def _ts(name: str) -> str:
    return f"tsproj.box.{name}"


def _ts_use(edges: dict[tuple[str, str], str | None], fn: str) -> dict[str, str | None]:
    return _callees(edges, f"tsproj.use.{fn}")


# ---------------------------------------------------------------------------
# TypeScript: every hop binds (red on main)
# ---------------------------------------------------------------------------


def test_ts_builder_chain_from_new_binds_every_hop(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "builderNew")
    assert callees.get(_ts("TBox.with")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_builder_chain_from_static_factory_binds_every_hop(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "builderStatic")
    assert callees.get(_ts("TBox.create")) == _EXACT
    assert callees.get(_ts("TBox.with")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_this_typed_method_keeps_the_receiver_class(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "viaThisType")
    assert callees.get(_ts("TBox.self")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_unannotated_method_returning_this_keeps_the_receiver_class(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "viaPlainThis")
    assert callees.get(_ts("TBox.plain")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_optional_return_names_its_one_class(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "viaOptional")
    assert callees.get(_ts("TBox.maybe")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_chain_through_module_level_function_binds_every_hop(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    # The issue's `fluent`: `newBox(): TBox`, `with(n): TBox`.
    callees = _ts_use(ts_edges, "viaFactory")
    assert callees.get(_ts("newBox")) == _EXACT
    assert callees.get(_ts("TBox.with")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_chain_from_typed_local_binds_every_hop(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "viaLocal")
    assert callees.get(_ts("TBox.with")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT


def test_ts_hop_moves_to_the_declared_return_class(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "crossType")
    assert callees.get(_ts("TBox.child")) == _EXACT
    assert callees.get(_ts("Child.finish")) == _EXACT
    assert _ts("Decoy.finish") not in callees


def test_ts_return_type_resolves_in_the_declaring_module(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _callees(ts_edges, "tsproj.factory_only.factoryOnly")
    assert callees.get(_ts("TBox.with")) == _EXACT
    assert callees.get(_ts("TBox.bump")) == _EXACT
    assert not {c for c in callees if c.startswith("tsproj.alt_box.")}


# ---------------------------------------------------------------------------
# TypeScript negatives: no guess, no unrelated class, single hops unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ["unknownHop", "mixedReturns"])
def test_ts_hop_with_unknown_return_ends_the_chain(
    ts_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # `opaque(): any` and a method returning `this` or `null` name no class:
    # the `bump` after them is not bound, to TBox or anything else.
    callees = _ts_use(ts_edges, fn)
    assert not {c for c in callees if c.endswith(".bump")}


def test_ts_factory_with_unknown_return_types_no_hop(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _ts_use(ts_edges, "unknownBase")
    assert callees.get(_ts("mystery")) == _EXACT
    assert not {c for c in callees if c.endswith((".with", ".bump"))}


def test_ts_same_named_methods_on_unrelated_classes_stay_unbound(
    ts_edges: dict[tuple[str, str], str | None],
) -> None:
    bound = {callee for (_src, callee) in ts_edges}
    assert not {c for c in bound if c.startswith(_ts("Decoy."))}
    assert not {c for c in bound if c.startswith("tsproj.alt_box.")}


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        ("singleNew", {_ts("TBox.bump"): _EXACT}),
        ("singleLocal", {_ts("TBox.bump"): _EXACT}),
        ("singleFactory", {_ts("newBox"): _EXACT}),
    ],
)
def test_ts_single_hop_calls_are_unchanged(
    ts_edges: dict[tuple[str, str], str | None],
    fn: str,
    expected: dict[str, str],
) -> None:
    callees = {
        callee: res
        for callee, res in _ts_use(ts_edges, fn).items()
        if not callee.endswith(".constructor")
    }
    assert callees == expected


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_PY_BOX = """\
from typing import Self


class Decoy:
    def with_(self, n) -> "Decoy":
        return self

    def bump(self):
        return 0

    def finish(self):
        return 0


class Child:
    def finish(self):
        return 1


class Box:
    def __init__(self):
        self.n = 0

    @classmethod
    def create(cls) -> "Box":
        return cls()

    @classmethod
    def make(cls):
        return cls()

    def with_(self, n) -> "Box":
        self.n = n
        return self

    def me(self) -> Self:
        return self

    def plain(self):
        return self

    def child(self) -> Child:
        return Child()

    def opaque(self):
        return lookup(self)

    def bump(self):
        return self.n


def new_box() -> Box:
    return Box()
"""

# PEP 563: `-> QBox` is legal before the class body ends only because every
# annotation is a string.
_PY_FUTURE = """\
from __future__ import annotations


class QBox:
    def w_future(self) -> QBox:
        return self

    def bump(self):
        return 1
"""

# Same-named class the callers never import.
_PY_OTHER = """\
class Box:
    def with_(self, n) -> "Box":
        return self

    def bump(self):
        return 2
"""

_PY_USE = """\
from app.box import Box, new_box
from app.future_box import QBox


def builder():
    return Box().with_(1).with_(2).bump()


def via_create():
    return Box.create().with_(1).bump()


def via_make():
    return Box.make().with_(1).bump()


def via_self_type():
    return Box().me().me().bump()


def via_future():
    return QBox().w_future().bump()


def via_plain():
    return Box().plain().bump()


def via_factory():
    return new_box().with_(1).with_(2).bump()


def via_local():
    b = Box()
    return b.with_(1).with_(2).bump()


def cross_type():
    return new_box().child().finish()


def unknown_hop():
    return new_box().opaque().bump()


def single_ctor():
    return Box().bump()


def single_factory():
    return new_box().bump()


def single_local():
    b = Box()
    return b.bump()
"""

_PY_FACTORY_ONLY = """\
from app.box import new_box


def factory_only():
    return new_box().with_(1).bump()
"""


@pytest.fixture
def py_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "pyproj"
    _write(
        root,
        {
            "app/__init__.py": "",
            "app/box.py": _PY_BOX,
            "app/future_box.py": _PY_FUTURE,
            "app/alt_box.py": _PY_OTHER,
            "app/use.py": _PY_USE,
            "app/factory_only.py": _PY_FACTORY_ONLY,
        },
    )
    create_and_run_updater(root, mock_ingestor)
    return _calls(mock_ingestor)


def _py(name: str) -> str:
    return f"pyproj.app.box.{name}"


def _py_use(edges: dict[tuple[str, str], str | None], fn: str) -> dict[str, str | None]:
    return _callees(edges, f"pyproj.app.use.{fn}")


def test_py_builder_chain_binds_every_hop(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "builder")
    assert callees.get(_py("Box.with_")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


@pytest.mark.parametrize(
    ("fn", "ctor"), [("via_create", "create"), ("via_make", "make")]
)
def test_py_classmethod_constructor_chain_binds_every_hop(
    py_edges: dict[tuple[str, str], str | None], fn: str, ctor: str
) -> None:
    callees = _py_use(py_edges, fn)
    assert callees.get(_py(f"Box.{ctor}")) == _EXACT
    assert callees.get(_py("Box.with_")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


def test_py_self_typed_method_keeps_the_receiver_class(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "via_self_type")
    assert callees.get(_py("Box.me")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


def test_py_pep563_annotation_types_the_next_hop(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "via_future")
    assert callees.get("pyproj.app.future_box.QBox.w_future") == _EXACT
    assert callees.get("pyproj.app.future_box.QBox.bump") == _EXACT


def test_py_unannotated_method_returning_self_keeps_the_receiver_class(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "via_plain")
    assert callees.get(_py("Box.plain")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


def test_py_chain_through_module_level_function_binds_every_hop(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    # The issue's `fluent`: the second `with_` and `bump` were missing.
    callees = _py_use(py_edges, "via_factory")
    assert callees.get(_py("new_box")) == _EXACT
    assert callees.get(_py("Box.with_")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


def test_py_chain_from_typed_local_binds_every_hop(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "via_local")
    assert callees.get(_py("Box.with_")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT


def test_py_hop_moves_to_the_declared_return_class(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "cross_type")
    assert callees.get(_py("Box.child")) == _EXACT
    assert callees.get(_py("Child.finish")) == _EXACT
    assert _py("Decoy.finish") not in callees


def test_py_return_type_resolves_in_the_declaring_module(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _callees(py_edges, "pyproj.app.factory_only.factory_only")
    assert callees.get(_py("Box.with_")) == _EXACT
    assert callees.get(_py("Box.bump")) == _EXACT
    assert not {c for c in callees if c.startswith("pyproj.app.alt_box.")}


# ---------------------------------------------------------------------------
# Python negatives
# ---------------------------------------------------------------------------


def test_py_hop_with_unknown_return_ends_the_chain(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _py_use(py_edges, "unknown_hop")
    assert callees.get(_py("Box.opaque")) == _EXACT
    assert not {c for c in callees if c.endswith(".bump")}


def test_py_same_named_methods_on_unrelated_classes_stay_unbound(
    py_edges: dict[tuple[str, str], str | None],
) -> None:
    bound = {callee for (_src, callee) in py_edges}
    assert not {c for c in bound if c.startswith(_py("Decoy."))}
    assert not {c for c in bound if c.startswith("pyproj.app.alt_box.")}


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        ("single_ctor", {_py("Box.bump"): _EXACT}),
        ("single_factory", {_py("new_box"): _EXACT, _py("Box.bump"): _EXACT}),
        ("single_local", {_py("Box.bump"): _EXACT}),
    ],
)
def test_py_single_hop_calls_are_unchanged(
    py_edges: dict[tuple[str, str], str | None],
    fn: str,
    expected: dict[str, str],
) -> None:
    callees = {
        callee: res
        for callee, res in _py_use(py_edges, fn).items()
        if not callee.endswith(".__init__")
    }
    assert callees == expected


# ---------------------------------------------------------------------------
# Python: one level of inference per hop, bounded like every receiver chain
# ---------------------------------------------------------------------------

_PY_LONG_CHAIN = """\
class Box:
    def with_(self, n) -> "Box":
        return self

    def bump(self):
        return 1


def under_cap():
    return Box(){hops}.bump()


def assigned(b: Box):
    x = b{long_hops}
    return x
"""


def _long_chain_source(hops: int, long_hops: int) -> str:
    return _PY_LONG_CHAIN.replace("{hops}", ".with_(1)" * hops).replace(
        "{long_hops}", ".with_(1)" * long_hops
    )


def test_py_chain_under_the_receiver_cap_binds_its_last_hop(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    root = temp_repo / "longproj"
    _write(root, {"m.py": _long_chain_source(cs.MAX_RECEIVER_CHAIN_HOPS - 4, 1)})
    create_and_run_updater(root, mock_ingestor)
    callees = _callees(_calls(mock_ingestor), "longproj.m.under_cap")
    assert callees.get("longproj.m.Box.with_") == _EXACT
    assert callees.get("longproj.m.Box.bump") == _EXACT


def test_py_generated_chain_past_the_cap_does_not_recurse_per_hop(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Typing `b.with_(1)....` a hop at a time recursed once per hop. Past the
    # receiver cap (#2262) that overflowed the stack on every assignment's
    # inference, and the swallowed RecursionError cost a deep unwind each time.
    from codebase_rag.parsers.py.expression_analyzer import (
        PythonExpressionAnalyzerMixin,
    )

    overflows: list[str] = []
    original = PythonExpressionAnalyzerMixin._infer_chained_call_return_type_fixed

    def watching(
        self: PythonExpressionAnalyzerMixin,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        try:
            return original(self, call_name, module_qn, local_var_types)
        except RecursionError:
            overflows.append(module_qn)
            raise

    root = temp_repo / "longproj"
    _write(root, {"m.py": _long_chain_source(1, 3_000)})
    with patch.object(
        PythonExpressionAnalyzerMixin,
        "_infer_chained_call_return_type_fixed",
        watching,
    ):
        create_and_run_updater(root, mock_ingestor)
    assert not overflows


# ---------------------------------------------------------------------------
# TypeScript overloads: a caller sees the overload signatures, never the
# implementation's own, so the hop types only when every signature agrees
# ---------------------------------------------------------------------------

_TS_OVERLOADS = """\
export class A { finish(): number { return 1; } }
export class B extends A { finish(): number { return 2; } }

export class C {
  pick(s: string): A;
  pick(n: number): B;
  pick(x: any): A | B { return x ? new A() : new B(); }

  wide(s: string): A;
  wide(n: number): B;
  wide(x: any): A { return x ? new A() : new B(); }

  body(s: string): A;
  body(n: number): B;
  body(x: any) { return new A(); }

  same(s: string): this;
  same(n: number): this;
  same(x: any): this { return this; }

  both(s: string): A;
  both(n: number): A;
  both(x: any): A { return new A(); }

  done(): number { return 0; }

  async plainAsync() { return this; }
  async buildAsync() { return new A(); }
}

export function make(s: string): A;
export function make(n: number): B;
export function make(x: any): A { return x ? new A() : new B(); }

export function makeA(s: string): A;
export function makeA(n: number): A;
export function makeA(x: any): A { return new A(); }

export async function makeAsync() { return new A(); }
export const makeArrowAsync = async () => new A();

export class P { finish(): number { return 1; } }
export class Q { done(): number { return 2; } }

// A static and an instance method may share a name; each has its own
// overload set. D declares the static one first, E the instance one.
export class D {
  static make(s: string): P;
  static make(n: number): P;
  static make(x: any): P { return new P(); }
  make(s: string): Q;
  make(x: any): Q { return new Q(); }
}

export class E {
  build(s: string): Q;
  build(x: any): Q { return new Q(); }
  static build(s: string): P;
  static build(n: number): Q;
  static build(x: any): any { return new P(); }
}
"""

_TS_OVERLOAD_USE = """\
import { C, D, E, make, makeA, makeAsync, makeArrowAsync } from './over';

export function pickUnion(): number { return new C().pick(1).finish(); }
export function pickWide(): number { return new C().wide(1).finish(); }
export function pickBody(): number { return new C().body(1).finish(); }
export function fnOverload(): number { return make(1).finish(); }
export function sameThis(): number { return new C().same(1).done(); }
export function sameClass(): number { return new C().both(1).finish(); }
export function fnSameClass(): number { return makeA(1).finish(); }
export function asyncThis(): number { return new C().plainAsync().done(); }
export function asyncNew(): number { return new C().buildAsync().finish(); }
export function asyncFn(): number { return makeAsync().finish(); }
export function asyncArrow(): number { return makeArrowAsync().finish(); }
export function staticAgree(): number { return D.make("x").finish(); }
export function instanceBesideStatic(): number { return new D().make("x").done(); }
export function instanceFirst(): number { return new E().build("x").done(); }
export function staticDisagreeP(): number { return E.build("x").finish(); }
export function staticDisagreeQ(): number { return E.build("x").done(); }
"""


@pytest.fixture
def ts_overload_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "tsover"
    _write(root, {"over.ts": _TS_OVERLOADS, "use_over.ts": _TS_OVERLOAD_USE})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


def _over_use(
    edges: dict[tuple[str, str], str | None], fn: str
) -> dict[str, str | None]:
    return _callees(edges, f"tsover.use_over.{fn}")


@pytest.mark.parametrize("fn", ["pickUnion", "pickWide", "pickBody", "fnOverload"])
def test_ts_overloads_that_disagree_type_no_hop(
    ts_overload_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # `pick(1)` is a B by its second signature; the implementation's `A`
    # (or `A | B`, or a body building an A) is not what the caller gets.
    callees = _over_use(ts_overload_edges, fn)
    assert not {c for c in callees if c.endswith(".finish")}


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        ("sameThis", "tsover.over.C.done"),
        ("sameClass", "tsover.over.A.finish"),
        ("fnSameClass", "tsover.over.A.finish"),
    ],
)
def test_ts_overloads_that_agree_still_chain(
    ts_overload_edges: dict[tuple[str, str], str | None], fn: str, expected: str
) -> None:
    assert _over_use(ts_overload_edges, fn).get(expected) == _EXACT


@pytest.mark.parametrize(
    ("fn", "expected"),
    [
        ("staticAgree", "tsover.over.P.finish"),
        ("instanceBesideStatic", "tsover.over.Q.done"),
        ("instanceFirst", "tsover.over.Q.done"),
    ],
)
def test_ts_static_and_instance_overloads_are_separate_sets(
    ts_overload_edges: dict[tuple[str, str], str | None], fn: str, expected: str
) -> None:
    # `D.make` reaches the static set, `new D().make` the instance one; mixed
    # into one set they disagreed and typed nothing, or the static body typed
    # an instance call.
    callees = _over_use(ts_overload_edges, fn)
    assert callees.get(expected) == _EXACT
    assert {c for c in callees if c.endswith((".finish", ".done"))} == {expected}


@pytest.mark.parametrize("fn", ["staticDisagreeP", "staticDisagreeQ"])
def test_ts_static_overloads_that_disagree_type_no_hop(
    ts_overload_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # E's static set says P for a string and Q for a number; the instance set
    # beside it agreeing on Q does not decide the static call.
    callees = _over_use(ts_overload_edges, fn)
    assert not {c for c in callees if c.endswith((".finish", ".done"))}


@pytest.mark.parametrize("fn", ["asyncThis", "asyncNew", "asyncFn", "asyncArrow"])
def test_ts_async_callable_types_no_hop(
    ts_overload_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # An async call hands back a Promise, whatever its body returns.
    callees = _over_use(ts_overload_edges, fn)
    assert not {c for c in callees if c.endswith((".done", ".finish"))}


# ---------------------------------------------------------------------------
# Python coroutines: an unawaited call to an `async def` is a coroutine, so
# its declared return types no hop
# ---------------------------------------------------------------------------

_PY_ASYNC = """\
from typing import AsyncIterator


class Box:
    async def step(self) -> "Box":
        return self

    async def stream(self) -> AsyncIterator["Box"]:
        yield self

    def step_sync(self) -> "Box":
        return self

    async def items(self):
        yield self

    @classmethod
    async def acreate(cls) -> "Box":
        return cls()

    def with_(self, n) -> "Box":
        return self

    def close(self):
        return 0


async def make_box() -> Box:
    return Box()


def coroutine_hop():
    return Box().step().close()


def coroutine_mid_chain():
    return Box().with_(1).step().close()


def async_generator_hop():
    return Box().items().close()


def annotated_async_generator_hop():
    return Box().stream().close()


async def async_for_over_generator():
    async for box in Box().with_(1).stream():
        box.close()


def async_classmethod_base():
    return Box.acreate().with_(1).close()


def async_factory_base():
    return make_box().with_(1).close()


def sync_hop():
    return Box().step_sync().close()


async def awaited_hop():
    return (await Box().step()).close()
"""


@pytest.fixture
def py_async_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "pyaio"
    _write(root, {"app/__init__.py": "", "app/aio.py": _PY_ASYNC})
    create_and_run_updater(root, mock_ingestor)
    return _calls(mock_ingestor)


@pytest.mark.parametrize(
    "fn",
    [
        "coroutine_hop",
        "coroutine_mid_chain",
        "async_generator_hop",
        "annotated_async_generator_hop",
        "async_classmethod_base",
        "async_factory_base",
    ],
)
def test_py_coroutine_call_types_no_hop(
    py_async_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # Python runs the coroutine's (or async generator's) own `close`, not
    # Box.close.
    callees = _callees(py_async_edges, f"pyaio.app.aio.{fn}")
    assert "pyaio.app.aio.Box.close" not in callees


def test_py_sync_twin_of_a_coroutine_still_chains(
    py_async_edges: dict[tuple[str, str], str | None],
) -> None:
    callees = _callees(py_async_edges, "pyaio.app.aio.sync_hop")
    assert callees.get("pyaio.app.aio.Box.step_sync") == _EXACT
    assert callees.get("pyaio.app.aio.Box.close") == _EXACT


def test_py_awaited_coroutine_hop_stays_unbound(
    py_async_edges: dict[tuple[str, str], str | None],
) -> None:
    # `(await x.step()).close()` is left as main leaves it: the text path
    # does not see through `await`, and no guess replaces it.
    callees = _callees(py_async_edges, "pyaio.app.aio.awaited_hop")
    assert callees.get("pyaio.app.aio.Box.step") == _EXACT
    assert "pyaio.app.aio.Box.close" not in callees


def test_py_async_generator_still_types_its_async_for_target(
    py_async_edges: dict[tuple[str, str], str | None],
) -> None:
    # An async generator's call is the iterator its annotation names, so the
    # loop target keeps the element type the coroutine rule must not erase.
    callees = _callees(py_async_edges, "pyaio.app.aio.async_for_over_generator")
    assert callees.get("pyaio.app.aio.Box.close") == _EXACT


# ---------------------------------------------------------------------------
# TypeScript `this` / `super` bases: inside a static member they are the
# class, so the first hop reaches its static method
# ---------------------------------------------------------------------------

_TS_STATIC_THIS = """\
export class A {
  static make(): A { return new A(); }
  done(): number { return 1; }
  static run(): number { return this.make().done(); }
  static runArrow(): number { const f = () => this.make().done(); return f(); }
  static runNested(): number {
    const f = function () { return this.make().done(); };
    return f();
  }
}

export class Base {
  static make(): Base { return new Base(); }
  done(): number { return 2; }
}

export class Sub extends Base {
  static run(): number { return super.make().done(); }
}

export class PS { finish(): number { return 1; } }
export class QS { finish(): number { return 2; } }

export class M {
  static make(s: string): PS;
  static make(x?: any): PS { return new PS(); }
  make(s: string): QS;
  make(x?: any): QS { return new QS(); }
  static viaStatic(): number { return this.make("s").finish(); }
  viaInstance(): number { return this.make("s").finish(); }
}
"""


@pytest.fixture
def ts_static_this_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "tsthis"
    _write(root, {"st.ts": _TS_STATIC_THIS})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


def _from_callers(
    edges: dict[tuple[str, str], str | None], caller_prefix: str
) -> dict[str, str | None]:
    # An arrow's calls belong to the arrow, which is registered under the
    # method that holds it.
    return {
        callee: res
        for (src, callee), res in edges.items()
        if src == caller_prefix or src.startswith(f"{caller_prefix}.")
    }


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("tsthis.st.A.run", "tsthis.st.A.done"),
        ("tsthis.st.A.runArrow", "tsthis.st.A.done"),
        ("tsthis.st.Sub.run", "tsthis.st.Base.done"),
    ],
)
def test_ts_this_or_super_in_a_static_method_reaches_the_static_member(
    ts_static_this_edges: dict[tuple[str, str], str | None],
    caller: str,
    expected: str,
) -> None:
    callees = _from_callers(ts_static_this_edges, caller)
    assert callees.get(expected) == _EXACT


def test_ts_this_in_a_nested_function_types_no_hop(
    ts_static_this_edges: dict[tuple[str, str], str | None],
) -> None:
    # A plain `function` has its own `this`, which is neither the class nor
    # an instance of it.
    callees = _from_callers(ts_static_this_edges, "tsthis.st.A.runNested")
    assert "tsthis.st.A.done" not in callees


@pytest.mark.parametrize(
    ("caller", "expected", "other"),
    [
        ("tsthis.st.M.viaStatic", "tsthis.st.PS.finish", "tsthis.st.QS.finish"),
        ("tsthis.st.M.viaInstance", "tsthis.st.QS.finish", "tsthis.st.PS.finish"),
    ],
)
def test_ts_this_picks_the_make_of_its_own_kind_of_method(
    ts_static_this_edges: dict[tuple[str, str], str | None],
    caller: str,
    expected: str,
    other: str,
) -> None:
    callees = _from_callers(ts_static_this_edges, caller)
    assert callees.get(expected) == _EXACT
    assert other not in callees


# ---------------------------------------------------------------------------
# Object literals: a method of an object literal (shorthand, getter, or a
# `function` property) binds `this` to that object, never to the class whose
# method builds it. Its chains must not reach the class around it
# ---------------------------------------------------------------------------

_OBJECT_LITERAL_THIS = """\
export class Other { done() { return 1; } }
export class Maker { make() { return new Other(); } }

export class Outer {
  make() { return this; }
  done() { return 2; }
  static smake() { return new Outer(); }

  arrowInLiteral() {
    const o = {
      make() { return new Other(); },
      go() { const f = () => this.make().done(); return f(); },
    };
    return o.go();
  }

  shorthand() {
    const o = { go() { return this.make().done(); } };
    Object.setPrototypeOf(o, new Maker());
    return o.go();
  }

  arrowInShorthand() {
    const o = { go() { const f = () => this.make().done(); return f(); } };
    Object.setPrototypeOf(o, new Maker());
    return o.go();
  }

  getter() {
    const o = { get value() { return this.make().done(); } };
    Object.setPrototypeOf(o, new Maker());
    return o.value;
  }

  functionProperty() {
    const o = { go: function () { return this.make().done(); } };
    Object.setPrototypeOf(o, new Maker());
    return o.go();
  }

  direct() { return this.make().done(); }

  arrowInMethod() { const f = () => this.make().done(); return f(); }

  static viaStatic() { return this.smake().done(); }

  classInLiteral() {
    const o = {
      K: class {
        make() { return new Other(); }
        go() { return this.make().done(); }
      },
    };
    return new o.K().go();
  }
}
"""


@pytest.fixture
def object_literal_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "objlit"
    _write(
        root,
        {"lit.ts": _OBJECT_LITERAL_THIS, "lit_js.js": _OBJECT_LITERAL_THIS},
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


_LIT_MODULES = ["objlit.lit", "objlit.lit_js"]


@pytest.mark.parametrize("module", _LIT_MODULES)
@pytest.mark.parametrize(
    "method",
    [
        "arrowInLiteral",
        "shorthand",
        "arrowInShorthand",
        "getter",
        "functionProperty",
    ],
)
def test_object_literal_this_chain_does_not_reach_the_outer_class(
    object_literal_edges: dict[tuple[str, str], str | None],
    module: str,
    method: str,
) -> None:
    # Node runs Other.done here, never Outer.done.
    callees = _from_callers(object_literal_edges, f"{module}.Outer.{method}")
    assert f"{module}.Outer.done" not in callees


@pytest.mark.parametrize("module", _LIT_MODULES)
@pytest.mark.parametrize("method", ["direct", "arrowInMethod", "viaStatic"])
def test_class_this_chain_still_reaches_the_class(
    object_literal_edges: dict[tuple[str, str], str | None],
    module: str,
    method: str,
) -> None:
    callees = _from_callers(object_literal_edges, f"{module}.Outer.{method}")
    assert callees.get(f"{module}.Outer.done") == _EXACT


@pytest.mark.parametrize("module", _LIT_MODULES)
def test_class_expression_inside_an_object_literal_keeps_its_own_this(
    object_literal_edges: dict[tuple[str, str], str | None], module: str
) -> None:
    # The class expression's methods bind `this` to that class, not to the
    # object holding it and not to Outer. Its anonymous members have no
    # module-level qn to read a return from, so the hop stays untyped, as
    # before.
    callees = _from_callers(object_literal_edges, f"{module}.Outer.classInLiteral")
    assert f"{module}.Outer.done" not in callees
    assert f"{module}.Other.done" not in callees


# ---------------------------------------------------------------------------
# A static method returning `this` hands back the class itself (the receiver
# class, a subclass when called through one): the next hop reaches only its
# static members, never an instance method
# ---------------------------------------------------------------------------

_CLASS_VALUE = """\
export class C {
  static self() { return this; }
  static other() { return 1; }
  instMethod() { return 2; }
  inst() { return this; }
}

export class D extends C {
  static other() { return 3; }
}

export class E extends C {}

export function oneHop() { return C.self().other(); }
export function twoHop() { return C.self().self().other(); }
export function subOwn() { return D.self().other(); }
export function subInherited() { return E.self().other(); }
export function staticToInstance() { return C.self().instMethod(); }
export function instanceSelf() { const c = new C(); return c.inst().instMethod(); }
export function objectLiteral() {
  const o = { me() { return this; }, instMethod() { return 0; } };
  return o.me().instMethod();
}
"""


@pytest.fixture
def class_value_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "clsval"
    _write(root, {"cv.ts": _CLASS_VALUE, "cv_js.js": _CLASS_VALUE})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


_CV_MODULES = ["clsval.cv", "clsval.cv_js"]


@pytest.mark.parametrize("module", _CV_MODULES)
@pytest.mark.parametrize(
    ("fn", "expected", "absent"),
    [
        ("oneHop", "C.other", "D.other"),
        ("twoHop", "C.other", "D.other"),
        # `this` in C's inherited static `self` is D, whose own `other` runs.
        ("subOwn", "D.other", "C.other"),
        ("subInherited", "C.other", "D.other"),
    ],
)
def test_static_this_return_reaches_the_receiver_class_statics(
    class_value_edges: dict[tuple[str, str], str | None],
    module: str,
    fn: str,
    expected: str,
    absent: str,
) -> None:
    callees = _callees(class_value_edges, f"{module}.{fn}")
    assert callees.get(f"{module}.{expected}") == _EXACT
    assert f"{module}.{absent}" not in callees


@pytest.mark.parametrize("module", _CV_MODULES)
def test_class_value_never_reaches_an_instance_method(
    class_value_edges: dict[tuple[str, str], str | None], module: str
) -> None:
    # The constructor has no `instMethod`; only an instance does.
    callees = _callees(class_value_edges, f"{module}.staticToInstance")
    assert f"{module}.C.instMethod" not in callees


@pytest.mark.parametrize("module", _CV_MODULES)
def test_instance_this_return_still_types_an_instance(
    class_value_edges: dict[tuple[str, str], str | None], module: str
) -> None:
    callees = _callees(class_value_edges, f"{module}.instanceSelf")
    assert callees.get(f"{module}.C.instMethod") == _EXACT


@pytest.mark.parametrize("module", _CV_MODULES)
def test_object_literal_this_return_types_no_hop(
    class_value_edges: dict[tuple[str, str], str | None], module: str
) -> None:
    callees = _callees(class_value_edges, f"{module}.objectLiteral")
    assert f"{module}.C.instMethod" not in callees


# ---------------------------------------------------------------------------
# A class-name base types the chain only when that name is the class at the
# call: declared in the module or imported, and not rebound by a parameter
# or local. A same-named class elsewhere in the project never answers
# ---------------------------------------------------------------------------

_SCOPE_FILES = {
    "prod": (
        "export class Product { finish() { return 1; } }\n"
        "export class Builder { static make() { return new Product(); } }\n"
    ),
    "imp": (
        "import { Builder } from './prod';\n"
        "export function importedBuilder() { return Builder.make().finish(); }\n"
    ),
    "local": (
        "export class Item { finish() { return 2; } }\n"
        "export class Builder { static make() { return new Item(); } }\n"
        "export function sameModule() { return Builder.make().finish(); }\n"
    ),
    "scope": (
        "export function paramBuilder(Builder) { return Builder.make().finish(); }\n"
        "export function localBuilder(other) {\n"
        "  const Builder = other;\n"
        "  return Builder.make().finish();\n"
        "}\n"
        "export function unimported() { return Builder.make().finish(); }\n"
    ),
}


@pytest.fixture(params=[".ts", ".js"])
def scope_edges(
    request: pytest.FixtureRequest, temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "bscope"
    _write(root, {f"{name}{request.param}": src for name, src in _SCOPE_FILES.items()})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


@pytest.mark.parametrize("fn", ["paramBuilder", "localBuilder", "unimported"])
def test_class_name_base_not_bound_at_the_call_types_no_hop(
    scope_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # A parameter or local named `Builder`, or a name nothing here binds, is
    # not prod's or local's Builder.
    callees = _callees(scope_edges, f"bscope.scope.{fn}")
    assert not {c for c in callees if c.endswith(".finish")}


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("bscope.imp.importedBuilder", "bscope.prod.Product.finish"),
        ("bscope.local.sameModule", "bscope.local.Item.finish"),
    ],
)
def test_visible_class_name_base_still_chains(
    scope_edges: dict[tuple[str, str], str | None], caller: str, expected: str
) -> None:
    callees = _callees(scope_edges, caller)
    assert callees.get(expected) == _EXACT
    assert {c for c in callees if c.endswith(".finish")} == {expected}


# ---------------------------------------------------------------------------
# A static member a subclass declares itself, method or not, shadows the
# parent's: the walk up `extends` stops at the first class declaring the name
# ---------------------------------------------------------------------------

_STATIC_SHADOW = """\
export class X { other() { return 0; } }
export function two() { return 2; }
export function makeX() { return new X(); }

export class C {
  static self() { return this; }
  static other() { return 1; }
}

export class D extends C { static other = OTHER_VALUE; }
export class G extends C { static get other() { return () => 3; } }
export class H extends C {}
export class K extends C { static other() { return 4; } }
export class S extends C { static self = SELF_VALUE; }

export function viaField() { return D.self().other(); }
export function viaAccessor() { return G.self().other(); }
export function viaInherited() { return H.self().other(); }
export function viaOwn() { return K.self().other(); }
export function viaShadowedFirstHop() { return S.self().other(); }
"""

# Greptile's arrow-valued static field, in JS. The same field in TS leaves a
# dangling REFERENCES edge on main already, which the graph audit rejects, so
# the TS module shadows with a function-valued field instead.
_STATIC_SHADOW_VALUES = {
    ".js": {"OTHER_VALUE": "() => 2", "SELF_VALUE": "() => new X()"},
    ".ts": {"OTHER_VALUE": "two", "SELF_VALUE": "makeX"},
}


@pytest.fixture(params=[".ts", ".js"])
def shadow_edges(
    request: pytest.FixtureRequest, temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "sshadow"
    source = _STATIC_SHADOW
    for placeholder, value in _STATIC_SHADOW_VALUES[request.param].items():
        source = source.replace(placeholder, value)
    _write(root, {f"sh{request.param}": source})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


@pytest.mark.parametrize("fn", ["viaField", "viaAccessor", "viaShadowedFirstHop"])
def test_subclass_static_field_or_accessor_shadows_the_parent_method(
    shadow_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # JavaScript calls the subclass's own `other` (a field, or what its getter
    # returns), or its own `self` field; the parent's method never runs.
    # An own member that is not a plain static method leaves the call unbound
    # rather than picking the shadowed parent's.
    callees = _callees(shadow_edges, f"sshadow.sh.{fn}")
    assert not {c for c in callees if c.endswith(".other")}


@pytest.mark.parametrize(
    ("fn", "expected"),
    [("viaInherited", "sshadow.sh.C.other"), ("viaOwn", "sshadow.sh.K.other")],
)
def test_subclass_static_lookup_binds_the_nearest_method(
    shadow_edges: dict[tuple[str, str], str | None], fn: str, expected: str
) -> None:
    callees = _callees(shadow_edges, f"sshadow.sh.{fn}")
    assert callees.get(expected) == _EXACT
    assert {c for c in callees if c.endswith(".other")} == {expected}


# ---------------------------------------------------------------------------
# A hop reads the class its qn names: a same-named class declared earlier in
# the file, inside a function or another class's method, is a different
# class and must not answer for the module-level one, nor the reverse
# ---------------------------------------------------------------------------

_NESTED_FN_FIRST = """\
export class Wrong { finish() { return 0; } }
export class Right { finish() { return 1; } }
export function early() {
  class Box {
    make() { return new Wrong(); }
    selfChain() { return this.make().finish(); }
  }
  return new Box();
}
export class Box { make() { return new Right(); } }
export function useTop() { return new Box().make().finish(); }
"""

_NESTED_METHOD_FIRST = """\
export class Wrong { finish() { return 0; } }
export class Right { finish() { return 1; } }
export class Holder {
  m() {
    class Box { make() { return new Wrong(); } }
    return new Box();
  }
}
export class Box { make() { return new Right(); } }
export function useTop() { return new Box().make().finish(); }
"""

_LONE_AND_DEFAULT = """\
class Right { finish() { return 1; } }
class Lone { make() { return new Right(); } }
export default class Dflt { make() { return new Right(); } }
export function useLone() { return new Lone().make().finish(); }
export function useDefault() { return new Dflt().make().finish(); }
"""

_NAMESPACED = """\
export class Right { finish() { return 1; } }
export class Wrong { finish() { return 0; } }
export function holder() {
  class Inner { make() { return new Wrong(); } }
  return new Inner();
}
export namespace N {
  export class Inner { make() { return new Right(); } }
  export function useInside() { return new Inner().make().finish(); }
}
"""


@pytest.fixture(params=[".ts", ".js"])
def nested_class_edges(
    request: pytest.FixtureRequest, temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    ext = request.param
    files = {
        f"nfn{ext}": _NESTED_FN_FIRST,
        f"nmeth{ext}": _NESTED_METHOD_FIRST,
        f"lone{ext}": _LONE_AND_DEFAULT,
    }
    root = temp_repo / "nested"
    _write(root, files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


@pytest.mark.parametrize("module", ["nested.nfn", "nested.nmeth"])
def test_top_level_class_hop_is_not_read_from_an_earlier_nested_class(
    nested_class_edges: dict[tuple[str, str], str | None], module: str
) -> None:
    callees = _callees(nested_class_edges, f"{module}.useTop")
    assert callees.get(f"{module}.Right.finish") == _EXACT
    assert f"{module}.Wrong.finish" not in callees


def test_nested_class_hop_reads_its_own_class(
    nested_class_edges: dict[tuple[str, str], str | None],
) -> None:
    # Reached through its own qn (`early.Box`), the nested class answers for
    # itself, not the module-level Box declared after it.
    callees = _callees(nested_class_edges, "nested.nfn.early.Box.selfChain")
    assert callees.get("nested.nfn.Wrong.finish") == _EXACT
    assert "nested.nfn.Right.finish" not in callees


@pytest.mark.parametrize("fn", ["useLone", "useDefault"])
def test_module_level_and_default_export_classes_still_chain(
    nested_class_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    callees = _callees(nested_class_edges, f"nested.lone.{fn}")
    assert callees.get("nested.lone.Right.finish") == _EXACT


def test_namespace_class_hop_reads_the_class_in_its_namespace(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # TypeScript only: JS has no namespaces. `N.Inner` is read inside N, not
    # from the function-local Inner declared before it.
    root = temp_repo / "nsproj"
    _write(root, {"ns.ts": _NAMESPACED})
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    callees = _callees(_calls(mock_ingestor), "nsproj.ns.N.useInside")
    assert callees.get("nsproj.ns.Right.finish") == _EXACT
    assert "nsproj.ns.Wrong.finish" not in callees


# ---------------------------------------------------------------------------
# A nested class's return names the class its own scopes see: a local or
# namespace `Product` shadows the module-level one, for an annotation and
# for a `return new Product()` alike
# ---------------------------------------------------------------------------

_SCOPED_TS = """\
export class Product { finish() { return 0; } }
export class Shared { finish() { return 2; } }
export function f() {
  class Product { finish() { return 1; } }
  class Builder {
    make(): Product { return new Product(); }
    use() { return this.make().finish(); }
    makeShared(): Shared { return new Shared(); }
    useShared() { return this.makeShared().finish(); }
  }
  class Inferred {
    make() { return new Product(); }
    use() { return this.make().finish(); }
    makeShared() { return new Shared(); }
    useShared() { return this.makeShared().finish(); }
  }
  return [new Builder(), new Inferred()];
}
export class Generic<Product> {
  make(): Product { return this.make(); }
  use() { return this.make().finish(); }
}
"""

_SCOPED_JS = """\
export class Product { finish() { return 0; } }
export class Shared { finish() { return 2; } }
export function f() {
  class Product { finish() { return 1; } }
  class Inferred {
    make() { return new Product(); }
    use() { return this.make().finish(); }
    makeShared() { return new Shared(); }
    useShared() { return this.makeShared().finish(); }
  }
  return new Inferred();
}
"""

_SCOPED_NS = """\
export class Product { finish() { return 0; } }
export namespace N {
  export class Product { finish() { return 1; } }
  export class Builder {
    make(): Product { return new Product(); }
    use() { return this.make().finish(); }
  }
  export class Inferred {
    make() { return new Product(); }
    use() { return this.make().finish(); }
  }
}
"""


@pytest.fixture
def scoped_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "scp"
    _write(
        root,
        {"fn.ts": _SCOPED_TS, "fn_js.js": _SCOPED_JS, "ns.ts": _SCOPED_NS},
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


@pytest.mark.parametrize(
    ("caller", "expected", "module_level"),
    [
        ("scp.fn.f.Builder.use", "scp.fn.f.Product.finish", "scp.fn.Product.finish"),
        ("scp.fn.f.Inferred.use", "scp.fn.f.Product.finish", "scp.fn.Product.finish"),
        (
            "scp.fn_js.f.Inferred.use",
            "scp.fn_js.f.Product.finish",
            "scp.fn_js.Product.finish",
        ),
        ("scp.ns.N.Builder.use", "scp.ns.N.Product.finish", "scp.ns.Product.finish"),
        ("scp.ns.N.Inferred.use", "scp.ns.N.Product.finish", "scp.ns.Product.finish"),
    ],
)
def test_nested_return_names_the_class_its_scope_sees(
    scoped_edges: dict[tuple[str, str], str | None],
    caller: str,
    expected: str,
    module_level: str,
) -> None:
    # The local or namespace `Product` runs, never the module-level one.
    callees = _callees(scoped_edges, caller)
    assert module_level not in callees
    assert callees.get(expected) == _EXACT


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("scp.fn.f.Builder.useShared", "scp.fn.Shared.finish"),
        ("scp.fn.f.Inferred.useShared", "scp.fn.Shared.finish"),
        ("scp.fn_js.f.Inferred.useShared", "scp.fn_js.Shared.finish"),
    ],
)
def test_nested_return_of_an_unshadowed_module_class_still_binds_it(
    scoped_edges: dict[tuple[str, str], str | None], caller: str, expected: str
) -> None:
    assert _callees(scoped_edges, caller).get(expected) == _EXACT


def test_type_parameter_named_like_a_class_types_no_hop(
    scoped_edges: dict[tuple[str, str], str | None],
) -> None:
    # `Generic<Product>`'s `Product` is its type parameter, not the class.
    callees = _callees(scoped_edges, "scp.fn.Generic.use")
    assert not {c for c in callees if c.endswith(".finish")}


# ---------------------------------------------------------------------------
# Types and values are separate namespaces. In a `new X()` (a value), the
# nearest value binding wins, a parameter included. In an annotation (a
# type), only type declarations shadow: a `const X` or `function X` does not
# hide the class type X
# ---------------------------------------------------------------------------

_VALUE_PARAM_SHADOW = """\
export class Product { finish() { return 0; } }
export function f() {
  class Product { finish() { return 1; } }
  class Other { finish() { return 2; } }
  class Builder {
    make(Product) { return new Product(); }
    use() { return this.make(Other).finish(); }
  }
  return new Builder();
}
export class Top {
  make(Product) { return new Product(); }
  use() { return this.make(Top).finish(); }
}
"""

_TYPE_VALUE_SPLIT_TS = """\
export class Product { finish() { return 0; } }
export function withConst() {
  const Product = 0;
  class Builder {
    make(): Product { return undefined as any; }
    use() { return this.make().finish(); }
  }
  return [Product, new Builder()];
}
export function withFunction() {
  function Product() { return 0; }
  class Builder {
    make(): Product { return undefined as any; }
    use() { return this.make().finish(); }
  }
  return [Product, new Builder()];
}
"""


@pytest.fixture
def namespace_split_edges(
    temp_repo: Path, mock_ingestor: MagicMock
) -> dict[tuple[str, str], str | None]:
    root = temp_repo / "nsplit"
    _write(
        root,
        {
            "vp.ts": _VALUE_PARAM_SHADOW.replace("make(Product)", "make(Product: any)"),
            "vp_js.js": _VALUE_PARAM_SHADOW,
            "tv.ts": _TYPE_VALUE_SPLIT_TS,
        },
    )
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return _calls(mock_ingestor)


@pytest.mark.parametrize("module", ["nsplit.vp", "nsplit.vp_js"])
@pytest.mark.parametrize("caller", ["f.Builder.use", "Top.use"])
def test_parameter_named_like_a_class_shadows_it_in_new(
    namespace_split_edges: dict[tuple[str, str], str | None],
    module: str,
    caller: str,
) -> None:
    # `make(Product)` builds whatever it is handed (`Other` here), never the
    # local or module-level class named Product.
    callees = _callees(namespace_split_edges, f"{module}.{caller}")
    assert not {c for c in callees if c.endswith(".finish")}


@pytest.mark.parametrize("fn", ["withConst", "withFunction"])
def test_value_declaration_does_not_hide_a_class_type(
    namespace_split_edges: dict[tuple[str, str], str | None], fn: str
) -> None:
    # `make(): Product` names the class type; the enclosing `const Product`
    # or `function Product` is a value, in the other namespace.
    callees = _callees(namespace_split_edges, f"nsplit.tv.{fn}.Builder.use")
    assert callees.get("nsplit.tv.Product.finish") == _EXACT
