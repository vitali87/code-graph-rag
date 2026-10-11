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
