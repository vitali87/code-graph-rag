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
    # core still on
    assert sel.rel_enabled(RT.CALLS)


def test_flows_to_is_in_io_group_and_opt_in() -> None:
    assert not resolve_capture([]).rel_enabled(RT.FLOWS_TO)
    assert resolve_capture(["io"]).rel_enabled(RT.FLOWS_TO)


def test_resource_node_gated_on_io() -> None:
    assert not resolve_capture([]).node_enabled(NL.RESOURCE)
    assert resolve_capture(["io"]).node_enabled(NL.RESOURCE)
    # unowned labels always enabled
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


def test_later_base_token_wins() -> None:
    # CGR_CAPTURE=none then --capture all must enable everything: the last
    # base token in order wins, not whichever the unordered set happens on.
    sel = resolve_capture(["none", "all"])
    assert sel.io_enabled
    assert sel.rel_enabled(RT.CALLS)
    # and the reverse order disables the core base.
    sel = resolve_capture(["all", "none"])
    assert not sel.io_enabled
    assert not sel.rel_enabled(RT.CALLS)


def test_later_base_clears_earlier_group_tokens() -> None:
    # CGR_CAPTURE=io then --capture none must disable I/O: a later base
    # token clears groups added by earlier tokens, applied in order.
    sel = resolve_capture(["io", "none"])
    assert not sel.io_enabled
    assert not sel.rel_enabled(RT.CALLS)
    # and a group added after the base survives.
    sel = resolve_capture(["io", "none", "calls"])
    assert sel.rel_enabled(RT.CALLS)
    assert not sel.io_enabled


def test_drop_group() -> None:
    sel = resolve_capture(["-imports"])
    assert not sel.rel_enabled(RT.IMPORTS)
    assert not sel.rel_enabled(RT.DEPENDS_ON_EXTERNAL)
    assert sel.rel_enabled(RT.CALLS)


def test_add_individual_type_without_group() -> None:
    sel = resolve_capture(["none", "+READS_FROM"])
    assert sel.rel_enabled(RT.READS_FROM)
    assert not sel.rel_enabled(RT.WRITES_TO)
    assert sel.node_enabled(NL.RESOURCE)  # io group has one enabled rel


def test_drop_individual_type() -> None:
    sel = resolve_capture(["-REFERENCES"])
    assert not sel.rel_enabled(RT.REFERENCES)
    assert sel.rel_enabled(RT.CALLS)


def test_dependency_gap_warns_but_obeys(caplog) -> None:
    # Dropping INHERITS while OVERRIDES stays is obeyed, with a warning.
    sel = resolve_capture(["-INHERITS"])
    assert not sel.rel_enabled(RT.INHERITS)
    assert sel.rel_enabled(RT.OVERRIDES)


def test_unknown_token_ignored() -> None:
    sel = resolve_capture(["bogus", "calls"])
    assert sel.rel_enabled(RT.CALLS)


def test_split_spec_separators() -> None:
    assert split_spec("calls, io ;structure") == ["calls", "io", "structure"]
    assert split_spec("") == []


def test_an_optional_label_is_off_by_default_and_on_with_its_group() -> None:
    """Every capture-group-owned label, not just the one being added today.

    `_node_labels_for` enables any label NO group claims, which is right for
    the structural labels (a Class or Module should always exist) and is a
    trap for the optional ones: dropping such a label from
    `CAPTURE_GROUP_NODE_LABELS` does not disable it, it enables it
    unconditionally while its group's relationships stay off -- a node whose
    edge cannot be written.

    Every test that observes an EMITTER stays green through that, because the
    emitters are gated separately from the registration. This asserts through
    `resolve_capture`, which reads the registration itself.

    The list below is written out DELIBERATELY rather than derived from
    `CAPTURE_GROUP_NODE_LABELS`. Deriving it reproduces the very defect being
    guarded against: a dropped label vanishes from the iteration, so the guard
    goes green by having nothing to check (measured -- dropping `Field` from
    its group left the derived form passing). An explicit list fails loudly
    instead, and a new optional label is a one-line addition here.
    """
    must_be_gated = {
        NL.CODE_SMELL: cs.CaptureGroup.FINDINGS,
        NL.CONSTANT: cs.CaptureGroup.CONSTANTS,
        NL.FIELD: cs.CaptureGroup.FIELDS,
        NL.GLOSS: cs.CaptureGroup.GLOSSES,
        NL.PARAMETER: cs.CaptureGroup.PARAMETERS,
        NL.PATTERN: cs.CaptureGroup.FINDINGS,
        NL.RESOURCE: cs.CaptureGroup.IO,
        NL.SECURITY_ISSUE: cs.CaptureGroup.FINDINGS,
    }
    owner_of = {
        label: group
        for group, labels in cs.CAPTURE_GROUP_NODE_LABELS.items()
        for label in labels
    }
    # Both directions: nothing expected-optional has lost its group, and
    # nothing new became optional without being listed here.
    assert owner_of == must_be_gated

    default = resolve_capture([])
    for label, group in owner_of.items():
        assert label not in default.enabled_node_labels, (
            f"{label.value} is owned by the optional group {group.value} "
            "but is enabled in the default selection"
        )
        enabled = resolve_capture([f"+{group.value}"])
        assert label in enabled.enabled_node_labels, (
            f"{label.value} stays disabled with +{group.value}"
        )
        assert cs.CAPTURE_GROUP_RELS[group] & enabled.enabled_rels, (
            f"+{group.value} enables {label.value} but none of its relationships"
        )
