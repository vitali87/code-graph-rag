"""The `Gloss` node: agent-authored notes ABOUT code (issue #1808).

These pin the decisions the issue argues for, so a later change that
quietly reverses one is a red test rather than a silent regression.
"""

from __future__ import annotations

from codebase_rag.constants.graph import (
    CAPTURE_GROUP_NODE_LABELS,
    CAPTURE_GROUP_RELS,
    DEFAULT_CAPTURE_GROUPS,
    NODE_UNIQUE_CONSTRAINTS,
    CaptureGroup,
    NodeLabel,
    RelationshipType,
)


class TestTheNodeExists:
    def test_the_label_is_registered(self) -> None:
        assert NodeLabel.GLOSS.value == "Gloss"

    def test_it_has_a_unique_key(self) -> None:
        # Without one, `ensure_node_batch` warns and drops every gloss:
        # the ingestor keys its MERGE off this map.
        assert NODE_UNIQUE_CONSTRAINTS["Gloss"] == "qualified_name"


class TestItsEdges:
    def test_annotates_and_mentions_exist(self) -> None:
        assert RelationshipType.ANNOTATES.value == "ANNOTATES"
        assert RelationshipType.MENTIONS.value == "MENTIONS"

    def test_they_are_distinct(self) -> None:
        """The subject of a note is not the same as a symbol it refers to.

        "mirrors `parse_header`" ANNOTATES the function it is filed under
        and MENTIONS `parse_header`. Collapsing them would make every
        mentioned symbol look like a subject, which is what makes the
        node worth having over a text field.
        """
        assert RelationshipType.ANNOTATES is not RelationshipType.MENTIONS


class TestCaptureContract:
    def test_both_edges_belong_to_the_glosses_group(self) -> None:
        assert CAPTURE_GROUP_RELS[CaptureGroup.GLOSSES] == frozenset(
            {RelationshipType.ANNOTATES, RelationshipType.MENTIONS}
        )

    def test_the_group_owns_the_label(self) -> None:
        # An owned label is captured only while its group is enabled;
        # unowned labels are always captured.
        assert CAPTURE_GROUP_NODE_LABELS[CaptureGroup.GLOSSES] == frozenset(
            {NodeLabel.GLOSS}
        )

    def test_it_is_off_by_default(self) -> None:
        # Glosses are written, not parsed. Indexing a repo must not
        # start inventing them.
        assert CaptureGroup.GLOSSES not in DEFAULT_CAPTURE_GROUPS

    def test_every_relationship_still_has_exactly_one_group(self) -> None:
        """The module-level guard, restated as a test.

        `graph.py` refuses to import if a RelationshipType belongs to no
        capture group -- so this cannot fail while the import succeeds.
        It is here so the reason is discoverable from the test suite
        rather than only from a RuntimeError at import time.
        """
        seen: list[RelationshipType] = []
        for rels in CAPTURE_GROUP_RELS.values():
            seen.extend(rels)
        assert len(seen) == len(set(seen)), "a relationship is in two groups"
        assert set(seen) == set(RelationshipType)
