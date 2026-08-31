"""TypeScript resolved-INHERITS grading against the tsc oracle (issue #1190).

Gap 2 of that issue is resolved-edge grading beyond Python, which names TS as
one of the four languages that should have the precision/recall rows Python
has. The TypeScript oracle already emitted `extends` as INHERITS and
`implements` as IMPLEMENTS, both by simple name with the subtype pinned to a
location, so this adds the grading arm rather than any oracle-side work.

Both kinds fold into one scored relation, exactly as the Java and C# arms do:
cgr's own edges are graded as one supertype relation, so splitting them here
would measure something the cgr side does not distinguish.

An interface extending an interface is INHERITS on the oracle side too, since
cgr models superinterfaces as inheritance; that is a deliberate choice of the
oracle rather than an accident, and `test_an_interface_extending_an_interface`
pins it so a future oracle edit cannot quietly change the unit being graded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import constants as ec
from evals.inheritance import (
    CgrResult,
    score_inheritance,
    ts_oracle_inheritance,
)
from evals.oracles.typescript_oracle import (
    run_typescript_oracle,
    typescript_available,
)

pytestmark = pytest.mark.skipif(
    not typescript_available(),
    reason="the TypeScript oracle needs node and its vendored typescript",
)


def _write(root: Path, name: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body, encoding="utf-8")


def test_the_oracle_without_cgrignore_sees_both(tmp_path: Path) -> None:
    """The control for the pair below.

    Without it, the exclusion test passes on a fixture that produced no
    rows at all: two empty sets are indistinguishable from a working
    exclusion (issue #1520's own first attempt failed exactly that way).
    """
    _write(tmp_path, "visible.ts", "class Base {}\nclass Vis extends Base {}\n")
    _write(tmp_path / "ignored", "hidden.ts", "class B3 {}\nclass Hid extends B3 {}\n")

    inherits = ts_oracle_inheritance(tmp_path).inherits

    assert ("visible.ts:2", "Base") in inherits, inherits
    assert ("ignored/hidden.ts:2", "B3") in inherits, inherits


def test_the_oracle_honours_cgrignore(tmp_path: Path) -> None:
    """The oracle must grade the file set indexing covers.

    `ts_cgr_inheritance` goes through `_capture`, which passes the merged
    ignore rules to `GraphUpdater` (#1520). The oracle traverses the target
    itself, so without this it graded a WIDER set than the graph contains
    and a matching pair inside an excluded directory scored as a hit on
    both sides (Greptile P1, PR #1519).
    """
    _write(tmp_path, "visible.ts", "class Base {}\nclass Vis extends Base {}\n")
    _write(tmp_path / "ignored", "hidden.ts", "class B3 {}\nclass Hid extends B3 {}\n")
    (tmp_path / ".cgrignore").write_text("ignored/\n", encoding="utf-8")

    inherits = ts_oracle_inheritance(tmp_path).inherits

    assert ("visible.ts:2", "Base") in inherits, "the fixture stopped producing rows"
    assert ("ignored/hidden.ts:2", "B3") not in inherits, inherits


def test_oracle_reports_extends_and_implements(tmp_path: Path) -> None:
    """A class's `extends` and `implements` both reach the graded set."""
    _write(
        tmp_path,
        "a.ts",
        "class Base {}\n"
        "interface Shape { area(): number; }\n"
        "class Derived extends Base implements Shape {\n"
        "  area(): number { return 1; }\n"
        "}\n",
    )
    result = ts_oracle_inheritance(tmp_path)
    assert ("a.ts:3", "Base") in result.inherits
    assert ("a.ts:3", "Shape") in result.inherits


def test_an_interface_extending_an_interface(tmp_path: Path) -> None:
    """Superinterfaces are inheritance, matching how cgr models them.

    Pinned rather than assumed: the oracle emits INHERITS (not IMPLEMENTS) for
    an interface's `extends`, and grading would silently lose these rows if
    that ever changed.
    """
    _write(
        tmp_path,
        "b.ts",
        "interface Shape { area(): number; }\ninterface Sub extends Shape {}\n",
    )
    result = ts_oracle_inheritance(tmp_path)
    assert ("b.ts:2", "Shape") in result.inherits


def test_a_qualified_base_is_compared_by_simple_name(tmp_path: Path) -> None:
    """`extends ns.Base` grades as `Base`, the unit cgr's qn reduces to."""
    _write(
        tmp_path,
        "c.ts",
        "namespace ns { export class Base {} }\nclass D extends ns.Base {}\n",
    )
    result = ts_oracle_inheritance(tmp_path)
    assert ("c.ts:2", "Base") in result.inherits


def test_a_class_with_no_supertype_is_not_a_row(tmp_path: Path) -> None:
    """A plain class contributes no INHERITS row, so precision is not diluted.

    The empty case is where a grader that keys on "is a class" rather than
    "has a supertype" comes apart from the property it means.
    """
    _write(tmp_path, "d.ts", "class Lonely {\n  go(): void {}\n}\n")
    result = ts_oracle_inheritance(tmp_path)
    assert result.inherits == set()


def test_score_is_perfect_when_cgr_matches_the_oracle(tmp_path: Path) -> None:
    """The graded unit lines up end to end, not merely the oracle half."""
    _write(
        tmp_path,
        "e.ts",
        "class Base {}\nclass Derived extends Base {}\n",
    )
    oracle = ts_oracle_inheritance(tmp_path)
    cgr = CgrResult(inherits=set(oracle.inherits), overrides=set())
    row = score_inheritance(cgr, oracle, inherits_label=ec.TS_SUPERTYPES_LABEL).rows[0]
    assert row["label"] == ec.TS_SUPERTYPES_LABEL
    assert row["precision"] == 1.0
    assert row["recall"] == 1.0


def test_a_missing_cgr_edge_costs_recall(tmp_path: Path) -> None:
    """A dropped supertype must show up as lost recall, not as a silent pass."""
    _write(
        tmp_path,
        "f.ts",
        "class Base {}\nclass A extends Base {}\nclass B extends Base {}\n",
    )
    oracle = ts_oracle_inheritance(tmp_path)
    assert len(oracle.inherits) == 2
    kept = {next(iter(sorted(oracle.inherits)))}
    row = score_inheritance(
        CgrResult(inherits=kept, overrides=set()),
        oracle,
        inherits_label=ec.TS_SUPERTYPES_LABEL,
    ).rows[0]
    assert row["recall"] == 0.5
    assert row["precision"] == 1.0
    assert row["fn"] == 1


def test_the_oracle_emits_only_supertype_edges(tmp_path: Path) -> None:
    """Pin the assumption the rel_type filter rests on.

    Removing `if edge.rel_type not in (_INHERITS, _IMPLEMENTS)` from
    `ts_oracle_inheritance` passes every behavioural test above, and that
    survival is CORRECT rather than a coverage gap: `ts_ast.js` has a single
    `emitNameEdge` call site and it can only produce INHERITS or IMPLEMENTS,
    so no fixture can distinguish the filter's removal.

    The filter still earns its place, because the oracle gaining a third edge
    kind must not silently start scoring that kind as inheritance. That is an
    assumption about the oracle rather than a behaviour of the grader, so it
    is pinned here: this fails the day the oracle emits something else,
    turning "unreachable today" into a loud failure instead of a quiet
    mis-grade.
    """
    _write(
        tmp_path,
        "g.ts",
        "class Base {}\n"
        "interface Shape { area(): number; }\n"
        "class Derived extends Base implements Shape {\n"
        "  area(): number { return 1; }\n"
        "}\n"
        "function free(): number { return 1; }\n"
        "const value = 2;\n",
    )
    graph = run_typescript_oracle(tmp_path)
    assert graph.name_edges, "a fixture with no edges would pass vacuously"
    assert {e.rel_type for e in graph.name_edges} <= {"INHERITS", "IMPLEMENTS"}
