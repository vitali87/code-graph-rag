from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture, split_spec

RT = cs.RelationshipType
NL = cs.NodeLabel


def test_default_is_core_without_io() -> None:
    sel = resolve_capture([])
    assert sel.rel_enabled(RT.CALLS)
    assert sel.rel_enabled(RT.INHERITS)
    assert sel.rel_enabled(RT.IMPORTS)
    assert not sel.rel_enabled(RT.READS_FROM)
    assert not sel.rel_enabled(RT.WRITES_TO)
    assert not sel.io_enabled


def test_io_is_opt_in() -> None:
    sel = resolve_capture(["io"])
    assert sel.io_enabled
    assert sel.rel_enabled(RT.READS_FROM)
    assert sel.rel_enabled(RT.WRITES_TO)
    # (H) core still on
    assert sel.rel_enabled(RT.CALLS)


def test_resource_node_gated_on_io() -> None:
    assert not resolve_capture([]).node_enabled(NL.RESOURCE)
    assert resolve_capture(["io"]).node_enabled(NL.RESOURCE)
    # (H) unowned labels always enabled
    assert resolve_capture([]).node_enabled(NL.FUNCTION)
    assert resolve_capture(["none"]).node_enabled(NL.FUNCTION)


def test_none_base_then_add_group() -> None:
    sel = resolve_capture(["none", "calls"])
    assert sel.rel_enabled(RT.CALLS)
    assert sel.rel_enabled(RT.REFERENCES)
    assert not sel.rel_enabled(RT.INHERITS)
    assert not sel.rel_enabled(RT.IMPORTS)


def test_all_base_includes_io() -> None:
    sel = resolve_capture(["all"])
    assert sel.io_enabled
    assert sel.rel_enabled(RT.CALLS)


def test_drop_group() -> None:
    sel = resolve_capture(["-imports"])
    assert not sel.rel_enabled(RT.IMPORTS)
    assert not sel.rel_enabled(RT.DEPENDS_ON_EXTERNAL)
    assert sel.rel_enabled(RT.CALLS)


def test_add_individual_type_without_group() -> None:
    sel = resolve_capture(["none", "+READS_FROM"])
    assert sel.rel_enabled(RT.READS_FROM)
    assert not sel.rel_enabled(RT.WRITES_TO)
    assert sel.node_enabled(NL.RESOURCE)  # (H) io group has one enabled rel


def test_drop_individual_type() -> None:
    sel = resolve_capture(["-REFERENCES"])
    assert not sel.rel_enabled(RT.REFERENCES)
    assert sel.rel_enabled(RT.CALLS)


def test_dependency_gap_warns_but_obeys(caplog) -> None:
    # (H) Dropping INHERITS while OVERRIDES stays is obeyed, with a warning.
    sel = resolve_capture(["-INHERITS"])
    assert not sel.rel_enabled(RT.INHERITS)
    assert sel.rel_enabled(RT.OVERRIDES)


def test_unknown_token_ignored() -> None:
    sel = resolve_capture(["bogus", "calls"])
    assert sel.rel_enabled(RT.CALLS)


def test_split_spec_separators() -> None:
    assert split_spec("calls, io ;structure") == ["calls", "io", "structure"]
    assert split_spec("") == []
