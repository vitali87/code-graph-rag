# Dart constructor forms that emitted no outgoing edges, and a chained call
# on a method's return value that lost its second hop (issue #2482):
#   - a `factory` constructor's arrow or block body was never scanned, since
#     factory_constructor_signature has no `name` field and the class walk
#     skipped every member it could not name;
#   - a redirecting `: this(...)` / `: this.named(...)` and a `: super(...)` /
#     `: super.named(...)` initializer call another constructor without any
#     call-expression node, and calls in their arguments fell outside the
#     body-less constructor's slice;
#   - `p.move(1).sum()` typed the receiver as `Point` but never looked at the
#     extensions declared `on Point`, where `sum` lives.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater

SKIP = "dart"

FACTORIES_DART = """\
int audit() => 1;
int helper() => 2;
int compute() => 3;

class Conn {
  final int port;
  Conn() : port = compute();
  Conn._(this.port);
  factory Conn.arrow() => Conn();
  factory Conn.block() {
    audit();
    return Conn();
  }
  factory Conn.fromPort(int p) {
    return Conn._(p);
  }
  Conn.named() : this();
  Conn.other(int x) : this.named();
  Conn.priv() : this._(helper());
  factory Conn.idle() {
    return Conn.named();
  }
}

class Base {
  Base(int x);
  Base.named(int y);
  Base._();
}

class Derived extends Base {
  Derived() : super(1);
  Derived.n() : super.named(helper());
  const Derived.c() : super._();
  Derived.withBody() : super(2) {
    audit();
  }
}

class Plain {}

class Child extends Plain {
  Child() : super();
}

class Svc {
  Svc() {
    audit();
  }
}
"""

SHAPES_DART = """\
class Point {
  int x = 0;
  int y = 0;
  Point move(int dx) {
    return Point();
  }
  int norm() => x;
  make() => Point();
}

class Origin extends Point {}

class Other {
  int sum() => 3;
  int spin() => 4;
}

extension PointOps on Point {
  int sum() => x + y;
  int norm() => 0;
  Point twin() => this;
}

extension OtherOps on Other {
  int only() => 5;
}

int useAll() {
  Point p = Point();
  Point q = Point();
  return p.move(1).sum() + q.sum();
}

int useInstanceFirst() {
  Point p = Point();
  return p.move(1).norm();
}

int useInherited() {
  Origin o = Origin();
  return o.move(2).sum() + o.sum();
}

int useExtensionHop() {
  Point p = Point();
  return p.twin().move(3).norm();
}

int useForeign() {
  Point p = Point();
  return p.move(1).only() + p.move(1).spin();
}

int useUntyped() {
  Point p = Point();
  return p.make().sum();
}
"""

AREA_A_DART = """\
import 'shapes.dart';

extension AreaA on Point {
  int area() => 1;
}
"""

AREA_B_DART = """\
import 'shapes.dart';

extension AreaB on Point {
  int area() => 2;
}
"""

USE_ONE_DART = """\
import 'shapes.dart';
import 'area_a.dart';

int useOneArea() {
  Point p = Point();
  return p.move(1).area() + p.area();
}
"""

USE_BOTH_DART = """\
import 'shapes.dart';
import 'area_a.dart';
import 'area_b.dart';

int useBothAreas() {
  Point p = Point();
  return p.move(1).area() + p.area();
}
"""


@pytest.fixture
def dart_ctor_project(temp_repo: Path) -> Path:
    root = temp_repo / "dctors"
    lib = root / "lib"
    lib.mkdir(parents=True)
    (lib / "factories.dart").write_text(FACTORIES_DART, encoding="utf-8")
    (lib / "shapes.dart").write_text(SHAPES_DART, encoding="utf-8")
    (lib / "area_a.dart").write_text(AREA_A_DART, encoding="utf-8")
    (lib / "area_b.dart").write_text(AREA_B_DART, encoding="utf-8")
    (lib / "use_one.dart").write_text(USE_ONE_DART, encoding="utf-8")
    (lib / "use_both.dart").write_text(USE_BOTH_DART, encoding="utf-8")
    return root


Edge = tuple[str, str, int | None, int | None]


def _edges(mock_ingestor: MagicMock, rel: str) -> list[Edge]:
    out: list[Edge] = []
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if str(c.args[1]) != rel:
            continue
        props = c.kwargs.get("properties") or {}
        out.append(
            (
                str(c.args[0][2]),
                str(c.args[2][2]),
                props.get(cs.KEY_LINE),
                props.get(cs.KEY_COL),
            )
        )
    return out


def _pairs(edges: list[Edge]) -> set[tuple[str, str]]:
    return {(src, dst) for src, dst, _line, _col in edges}


def _has(edges: list[Edge], src_suffix: str, dst_suffix: str) -> bool:
    return any(
        src.endswith(src_suffix) and dst.endswith(dst_suffix)
        for src, dst in _pairs(edges)
    )


def _targets(edges: list[Edge], src_suffix: str) -> set[str]:
    return {dst for src, dst in _pairs(edges) if src.endswith(src_suffix)}


def _sites(edges: list[Edge], src_suffix: str, dst_suffix: str) -> set[int | None]:
    return {
        line
        for src, dst, line, _col in edges
        if src.endswith(src_suffix) and dst.endswith(dst_suffix)
    }


@pytest.fixture
def graph(dart_ctor_project: Path, mock_ingestor: MagicMock) -> MagicMock:
    run_updater(dart_ctor_project, mock_ingestor, skip_if_missing=SKIP)
    return mock_ingestor


class TestFactoryConstructorBodies:
    def test_arrow_factory_instantiates_and_calls_the_constructor(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        inst = _edges(graph, cs.RelationshipType.INSTANTIATES.value)
        assert _has(inst, ".factories.Conn.arrow", ".factories.Conn"), sorted(
            _pairs(inst)
        )
        assert _has(calls, ".factories.Conn.arrow", ".factories.Conn.Conn"), sorted(
            _pairs(calls)
        )

    def test_block_factory_scans_every_call_in_its_body(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        inst = _edges(graph, cs.RelationshipType.INSTANTIATES.value)
        assert _has(calls, ".factories.Conn.block", ".factories.audit"), sorted(
            _pairs(calls)
        )
        assert _has(calls, ".factories.Conn.block", ".factories.Conn.Conn"), sorted(
            _pairs(calls)
        )
        assert _has(inst, ".factories.Conn.block", ".factories.Conn"), sorted(
            _pairs(inst)
        )

    def test_factory_reaches_a_private_named_constructor(
        self, graph: MagicMock
    ) -> None:
        # dart-lang/http: `return CupertinoClient._(session);` inside a
        # factory left the private constructor reported dead.
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.fromPort", ".factories.Conn._"), sorted(
            _pairs(calls)
        )

    def test_factory_reaches_a_redirecting_constructor(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.idle", ".factories.Conn.named"), sorted(
            _pairs(calls)
        )


class TestRedirectingConstructors:
    def test_this_redirect_calls_the_default_constructor(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.named", ".factories.Conn.Conn"), sorted(
            _pairs(calls)
        )
        # The edge sits on the `this(...)` redirect's own line.
        assert _sites(calls, ".factories.Conn.named", ".factories.Conn.Conn") == {17}

    def test_this_named_redirect_calls_the_named_constructor(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.other", ".factories.Conn.named"), sorted(
            _pairs(calls)
        )

    def test_redirect_to_private_constructor_and_its_argument_call(
        self, graph: MagicMock
    ) -> None:
        # retry.dart: `RetryClient.withDelays(...) : this._withDelays(...)`.
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.priv", ".factories.Conn._"), sorted(
            _pairs(calls)
        )
        assert _has(calls, ".factories.Conn.priv", ".factories.helper"), sorted(
            _pairs(calls)
        )

    def test_super_initializer_calls_the_superclass_constructor(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Derived.Derived", ".factories.Base.Base"), (
            sorted(_pairs(calls))
        )

    def test_super_named_initializer_and_its_argument_call(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Derived.n", ".factories.Base.named"), sorted(
            _pairs(calls)
        )
        assert _has(calls, ".factories.Derived.n", ".factories.helper"), sorted(
            _pairs(calls)
        )

    def test_const_constructor_super_to_private_constructor(
        self, graph: MagicMock
    ) -> None:
        # cronet_client.dart: `CronetClientWithProfile._(...) : super._()`.
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Derived.c", ".factories.Base._"), sorted(
            _pairs(calls)
        )

    def test_field_initializer_call_belongs_to_the_constructor(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Conn.Conn", ".factories.compute"), sorted(
            _pairs(calls)
        )


class TestChainedExtensionMembers:
    def test_chained_call_reaches_the_extension_member(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        # Both `p.move(1).sum()` and `q.sum()` on line 31, one edge each.
        cols = {
            col
            for src, dst, line, col in calls
            if src.endswith(".shapes.useAll")
            and dst.endswith(".shapes.PointOps.sum")
            and line == 31
        }
        assert len(cols) == 2, sorted(calls)

    def test_extension_on_a_base_class_serves_the_subclass(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".shapes.useInherited", ".shapes.PointOps.sum"), sorted(
            _pairs(calls)
        )

    def test_extension_member_types_the_next_hop(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".shapes.useExtensionHop", ".shapes.PointOps.twin"), sorted(
            _pairs(calls)
        )
        assert _has(calls, ".shapes.useExtensionHop", ".shapes.Point.move"), sorted(
            _pairs(calls)
        )


# Negative: neighbouring behaviour that must not change.


class TestConstructorNegatives:
    def test_generative_constructor_body_still_scanned(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".factories.Svc.Svc", ".factories.audit"), sorted(
            _pairs(calls)
        )

    def test_a_redirect_instantiates_nothing(self, graph: MagicMock) -> None:
        # A redirecting or super-delegating constructor runs another
        # constructor on the SAME object; it constructs no new instance.
        inst = _edges(graph, cs.RelationshipType.INSTANTIATES.value)
        for caller in (
            ".factories.Conn.named",
            ".factories.Conn.other",
            ".factories.Conn.priv",
            ".factories.Derived.Derived",
            ".factories.Derived.n",
            ".factories.Derived.c",
        ):
            assert not _targets(inst, caller), (caller, sorted(_pairs(inst)))

    def test_super_without_a_declared_constructor_emits_nothing(
        self, graph: MagicMock
    ) -> None:
        # `Plain` has only its implicit constructor: there is no node to call.
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert not _targets(calls, ".factories.Child.Child"), sorted(_pairs(calls))

    def test_super_delegation_does_not_bind_to_the_own_class(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _targets(calls, ".factories.Derived.Derived") == {
            "dctors.lib.factories.Base.Base"
        }, sorted(_pairs(calls))

    def test_bodied_constructor_keeps_body_and_gains_super_edge(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _targets(calls, ".factories.Derived.withBody") == {
            "dctors.lib.factories.Base.Base",
            "dctors.lib.factories.audit",
        }, sorted(_pairs(calls))

    def test_initializer_calls_leave_the_module_caller(self, graph: MagicMock) -> None:
        # Before the fix `compute()` and `helper()` in a body-less
        # constructor's clauses were attributed to the FILE.
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        module_targets = {
            dst for src, dst in _pairs(calls) if src == "dctors.lib.factories"
        }
        assert not module_targets, sorted(module_targets)
        helper_callers = {
            src for src, dst in _pairs(calls) if dst == "dctors.lib.factories.helper"
        }
        assert helper_callers == {
            "dctors.lib.factories.Conn.priv",
            "dctors.lib.factories.Derived.n",
        }, sorted(helper_callers)


class TestExtensionNegatives:
    def test_instance_member_wins_over_an_extension_member(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _has(calls, ".shapes.useInstanceFirst", ".shapes.Point.norm"), sorted(
            _pairs(calls)
        )
        assert not _has(calls, ".shapes.useInstanceFirst", ".shapes.PointOps.norm"), (
            sorted(_pairs(calls))
        )

    def test_extension_on_another_type_does_not_apply(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert not _has(calls, ".shapes.useForeign", ".shapes.OtherOps.only"), sorted(
            _pairs(calls)
        )
        assert not _has(calls, ".shapes.useForeign", ".shapes.Other.spin"), sorted(
            _pairs(calls)
        )

    def test_untyped_hop_still_drops_the_chain(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert _targets(calls, ".shapes.useUntyped") == {
            "dctors.lib.shapes.Point.make"
        }, sorted(_pairs(calls))

    def test_extension_member_wins_over_a_same_named_member_elsewhere(
        self, graph: MagicMock
    ) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        assert not _has(calls, ".shapes.useAll", ".shapes.Other.sum"), sorted(
            _pairs(calls)
        )

    def test_only_the_imported_extension_applies(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        targets = _targets(calls, ".use_one.useOneArea")
        assert "dctors.lib.area_a.AreaA.area" in targets, sorted(targets)
        assert "dctors.lib.area_b.AreaB.area" not in targets, sorted(targets)
        assert (
            len(
                [
                    e
                    for e in calls
                    if e[0].endswith(".use_one.useOneArea")
                    and e[1].endswith(".AreaA.area")
                ]
            )
            == 2
        ), sorted(calls)

    def test_two_visible_extensions_are_ambiguous(self, graph: MagicMock) -> None:
        calls = _edges(graph, cs.RelationshipType.CALLS.value)
        targets = _targets(calls, ".use_both.useBothAreas")
        assert not any(t.endswith(".area") for t in targets), sorted(targets)
