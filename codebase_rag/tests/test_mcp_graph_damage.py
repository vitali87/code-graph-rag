"""The in-process record of damage to the graph an MCP server serves: which
reads it refuses, and which marker clears may retire it."""

from __future__ import annotations

from codebase_rag.mcp.graph_damage import GraphDamage


def test_undamaged_graph_blocks_no_project() -> None:
    damage = GraphDamage()
    assert not damage.blocks("alpha")


def test_damage_named_for_one_project_blocks_only_that_project() -> None:
    damage = GraphDamage(incomplete=True, project="alpha")
    assert damage.blocks("alpha")
    assert not damage.blocks("beta")


def test_unattributed_damage_blocks_every_project() -> None:
    damage = GraphDamage(incomplete=True)
    assert damage.blocks("alpha")
    assert damage.blocks("beta")


def test_note_outstanding_adds_names_and_skips_none() -> None:
    damage = GraphDamage()
    damage.note_outstanding("alpha", None)
    damage.note_outstanding("beta")
    assert damage.projects == {"alpha", "beta"}


def test_settling_the_named_project_retires_the_flag() -> None:
    damage = GraphDamage(incomplete=True, project="alpha", from_failed_clear="alpha")
    damage.settle("alpha")
    assert damage == GraphDamage()
    assert not damage.blocks("alpha")


def test_settling_another_project_leaves_the_flag_up() -> None:
    damage = GraphDamage(incomplete=True, project="alpha")
    damage.settle("beta")
    assert damage.incomplete
    assert damage.project == "alpha"
    assert damage.blocks("alpha")


def test_unattributed_flag_waits_for_the_last_outstanding_project() -> None:
    damage = GraphDamage(incomplete=True)
    damage.note_outstanding("alpha", "beta")

    damage.settle("alpha")
    assert damage.incomplete
    assert damage.projects == {"beta"}

    damage.settle("beta")
    assert not damage.incomplete
    assert damage.projects == set()


def test_a_wipe_is_not_retired_by_any_marker_clear() -> None:
    damage = GraphDamage(incomplete=True, unbounded=True)
    damage.settle("alpha")
    assert damage.incomplete
    assert damage.blocks("alpha")


def test_settling_the_failed_clear_drops_only_its_provenance() -> None:
    # The flag is another project's; this project's stranded marker is gone,
    # so it no longer explains the flag.
    damage = GraphDamage(incomplete=True, project="alpha", from_failed_clear="beta")
    damage.settle("beta")
    assert damage.incomplete
    assert damage.project == "alpha"
    assert damage.from_failed_clear is None
