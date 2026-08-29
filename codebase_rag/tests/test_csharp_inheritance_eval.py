"""C# resolved-INHERITS grading against the Roslyn oracle (issue #1190).

Gap 2 of that issue is resolved-edge grading beyond Python. The Roslyn oracle
already emitted base classes as INHERITS and base interfaces as IMPLEMENTS,
both by simple name with the subtype pinned to a location, so this adds the
grading arm rather than any oracle-side work.

Both kinds fold into one scored relation, exactly as the Java arm does: cgr's
own edges are graded as one supertype relation, so splitting them here would
measure something the cgr side does not distinguish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals import constants as ec
from evals.inheritance import (
    CgrResult,
    csharp_oracle_inheritance,
    score_inheritance,
)
from evals.oracles.csharp_oracle import csharp_oracle_available, run_csharp_oracle

_NEEDS_DOTNET = pytest.mark.skipif(
    not csharp_oracle_available(), reason="dotnet not available"
)


def _cs_project(tmp_path: Path, source: str) -> Path:
    (tmp_path / "Shapes.cs").write_text(source, encoding="utf-8")
    return tmp_path


@_NEEDS_DOTNET
class TestTheOracleReadsBaseTypes:
    def test_a_base_class_is_reported_against_the_subtype_location(
        self, tmp_path: Path
    ) -> None:
        project = _cs_project(
            tmp_path,
            "namespace Demo {\n"
            "    public class Shape { }\n"
            "    public class Circle : Shape { }\n"
            "}\n",
        )

        result = csharp_oracle_inheritance(project)

        assert result.inherits == {("Shapes.cs:3", "Shape")}

    def test_a_base_class_and_an_interface_both_count_as_supertypes(
        self, tmp_path: Path
    ) -> None:
        # The oracle emits these as two DIFFERENT rel_types (INHERITS and
        # IMPLEMENTS); the grading arm folds them, so both must survive.
        project = _cs_project(
            tmp_path,
            "namespace Demo {\n"
            "    public interface IDrawable { void Draw(); }\n"
            "    public class Shape { }\n"
            "    public class Circle : Shape, IDrawable {\n"
            "        public void Draw() { }\n"
            "    }\n"
            "}\n",
        )

        result = csharp_oracle_inheritance(project)

        assert result.inherits == {
            ("Shapes.cs:4", "Shape"),
            ("Shapes.cs:4", "IDrawable"),
        }

    def test_the_oracle_emits_both_edge_kinds_for_that_source(
        self, tmp_path: Path
    ) -> None:
        # Pins WHY the rel_type filter is load-bearing here, unlike the C++ arm
        # where the oracle only ever emits INHERITS. If this stops holding, the
        # fold in csharp_oracle_inheritance is measuring something else.
        project = _cs_project(
            tmp_path,
            "namespace Demo {\n"
            "    public interface IDrawable { void Draw(); }\n"
            "    public class Shape { }\n"
            "    public class Circle : Shape, IDrawable {\n"
            "        public void Draw() { }\n"
            "    }\n"
            "}\n",
        )

        kinds = {edge.rel_type for edge in run_csharp_oracle(project).name_edges}

        assert kinds == {"INHERITS", "IMPLEMENTS"}

    def test_a_type_with_no_supertype_contributes_no_edge(self, tmp_path: Path) -> None:
        project = _cs_project(
            tmp_path, "namespace Demo {\n    public class Alone { }\n}\n"
        )

        result = csharp_oracle_inheritance(project)

        assert result.inherits == set()
        assert result.top_classes == frozenset()

    def test_the_oracle_adjudicates_no_overrides(self, tmp_path: Path) -> None:
        project = _cs_project(
            tmp_path,
            "namespace Demo {\n"
            "    public class B { public virtual void F() { } }\n"
            "    public class D : B { public override void F() { } }\n"
            "}\n",
        )

        result = csharp_oracle_inheritance(project)

        assert result.overrides == set()
        assert result.override_scope == frozenset()


@_NEEDS_DOTNET
def test_the_scored_row_carries_the_csharp_label(tmp_path: Path) -> None:
    # C# folds base classes with interfaces, so it shares Java's wording rather
    # than C++'s bases-only one -- but the row must still say which unit it is
    # measuring rather than letting it read as the Python resolved-qn number.
    project = _cs_project(
        tmp_path,
        "namespace Demo {\n"
        "    public class Shape { }\n"
        "    public class Circle : Shape { }\n"
        "}\n",
    )
    oracle = csharp_oracle_inheritance(project)

    row = score_inheritance(
        CgrResult(inherits=oracle.inherits, overrides=set()),
        oracle,
        inherits_label=ec.CSHARP_SUPERTYPES_LABEL,
    ).rows[0]

    assert row["label"] == ec.CSHARP_SUPERTYPES_LABEL
    assert row["label"] != ec.INHERITS_LABEL
    assert row["precision"] == 1.0
