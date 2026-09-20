"""`self.Inner()` constructs a nested class; it is not a virtual method call (#1650).

`self.M()` fans out to every concrete override of `M`, so a method reached only
polymorphically is not reported dead. That fan-out resolved `self.Inner()` to
the nested CLASS `Outer.Inner` and emitted a `CALLS` edge to it, alongside the
correct `CALLS -> __init__` and `INSTANTIATES`.

`RELATIONSHIP_SCHEMAS` allows `CALLS` to target a Function, Method, Enum or Type
-- never a Class; construction is what `INSTANTIATES` records. So the extra edge
was both a duplicate and undocumented, and the suite's own `_audit_recorded_graph`
rejected it, which is why no existing test could exercise this shape: any test
that did aborted before it could assert anything.

The controls matter here. The fan-out is load-bearing for real overrides, so a
fix phrased as "drop the self-dispatch edges" would satisfy the assertion below
and silently break dead-code reachability for every polymorphic method.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.constants import RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def _source_target_pairs(
    mock_ingestor: MagicMock, rel: RelationshipType
) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2])
        for call in get_relationships(mock_ingestor, rel.value)
    }


def _labelled_calls(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    """(source qn, TARGET LABEL, target qn) for every CALLS edge."""
    return {
        (call[0][0][2], call[0][2][0], call[0][2][2])
        for call in get_relationships(mock_ingestor, RelationshipType.CALLS.value)
    }


@pytest.fixture
def nested_project(temp_repo: Path) -> Path:
    project = temp_repo / "selfnested"
    project.mkdir()
    (project / "m.py").write_text(
        encoding="utf-8",
        data="""
class Outer:
    class Inner:
        def __init__(self, v):
            self.v = v

    def make(self):
        return self.Inner(1)

    def make_optional(o: Outer | None): return o.Inner(1)
class Base:
    def render(self):
        return "base"

    def draw(self):
        return self.render()


class Derived(Base):
    def render(self):
        return "derived"
""",
    )
    return project


class TestSelfNestedClassConstruction:
    def test_no_calls_edge_targets_a_class(
        self, nested_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # Asserted over EVERY CALLS edge rather than the one pair, because the
        # schema forbids the shape outright: a second construction elsewhere in
        # the fixture would otherwise slip past a narrower assertion.
        create_and_run_updater(nested_project, mock_ingestor)
        to_classes = {
            edge for edge in _labelled_calls(mock_ingestor) if edge[1] == "Class"
        }
        assert not to_classes, f"CALLS may not target a Class: {sorted(to_classes)}"

    def test_the_construction_still_reaches_the_graph(
        self, nested_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The other half: removing the bad edge must not remove the good ones.
        create_and_run_updater(nested_project, mock_ingestor)
        assert (
            "selfnested.m.Outer.make",
            "selfnested.m.Outer.Inner",
        ) in _source_target_pairs(mock_ingestor, RelationshipType.INSTANTIATES)
        edges = _source_target_pairs(mock_ingestor, RelationshipType.INSTANTIATES)
        assert ("selfnested.m.Outer.make_optional", "selfnested.m.Outer.Inner") in edges
        assert (
            "selfnested.m.Outer.make",
            "selfnested.m.Outer.Inner.__init__",
        ) in _source_target_pairs(mock_ingestor, RelationshipType.CALLS)

    def test_self_dispatch_to_a_real_override_survives(
        self, nested_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The control. `self.render()` inside Base.draw must still fan out to
        # Derived.render, or a method reached only polymorphically reads as
        # dead. A fix that dropped self-dispatch edges wholesale would pass the
        # first test and break this.
        create_and_run_updater(nested_project, mock_ingestor)
        calls = _source_target_pairs(mock_ingestor, RelationshipType.CALLS)
        assert ("selfnested.m.Base.draw", "selfnested.m.Base.render") in calls
        assert ("selfnested.m.Base.draw", "selfnested.m.Derived.render") in calls
