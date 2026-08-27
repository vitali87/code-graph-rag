# Guards node properties in NODE_SCHEMAS against codec/schema.proto (#1490).
#
# The sibling `test_protobuf_relationship_parity.py` guards the relationship
# ENUM. Nothing guarded node PROPERTIES, and the failure mode is the same
# shape: a property added to `NODE_SCHEMAS` is written at ingestion and read by
# queries, but silently dropped on protobuf export. No error, no warning -- the
# exported index is simply missing a field the schema says exists.
#
# Found while adding `Module.front_matter` (#1488): review flagged that it
# would not survive export, and checking whether an existing guard would have
# caught it turned up dozens of other properties in the same position. So the
# specific finding was an instance of a general gap.
#
# DEFAULT-DENY, deliberately. An inventory of "properties to check" cannot fail
# for the property nobody remembered to list -- the same inversion the MCP lock
# guard needed in #1475. Here every declared property must either have a proto
# field or be named in `_NOT_EXPORTED` below, so forgetting fails.
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from codebase_rag.types_defs import NODE_SCHEMAS
from codec import schema_pb2 as pb

# Properties that exist in NODE_SCHEMAS and deliberately have no proto field.
#
# This is a RECORD OF THE CURRENT STATE, not an endorsement of it. Every entry
# was measured against the generated descriptors when this test was written;
# none has been reviewed for whether it SHOULD be exported. #1490 carries that
# question.
#
# The value of listing them is that the set is now closed: a new absence fails
# this test and has to be argued for explicitly, rather than joining the pile
# unnoticed. Removing an entry (by adding the proto field) is always safe;
# adding one should be a deliberate decision with a reason.
_NOT_EXPORTED: dict[str, frozenset[str]] = {
    "Project": frozenset({"root_path"}),
    "Package": frozenset({"absolute_path"}),
    "Folder": frozenset({"absolute_path"}),
    "File": frozenset({"absolute_path"}),
    "Module": frozenset({"absolute_path", "end_line", "front_matter", "start_line"}),
    "Class": frozenset({"absolute_path", "modifiers", "path", "start_col"}),
    "Function": frozenset(
        {
            "absolute_path",
            "is_macro",
            "modifiers",
            "name_start_col",
            "name_start_line",
            "path",
            "start_col",
        }
    ),
    "Method": frozenset(
        {
            "absolute_path",
            "is_exported",
            "is_property",
            "modifiers",
            "name_start_col",
            "name_start_line",
            "overrides_external",
            "path",
            "start_col",
        }
    ),
    "Interface": frozenset(
        {
            "decorators",
            "docstring",
            "end_line",
            "is_exported",
            "modifiers",
            "start_col",
            "start_line",
        }
    ),
    "Enum": frozenset(
        {
            "decorators",
            "docstring",
            "end_line",
            "is_exported",
            "modifiers",
            "start_col",
            "start_line",
        }
    ),
    "Type": frozenset(
        {
            "absolute_path",
            "decorators",
            "docstring",
            "end_line",
            "is_exported",
            "modifiers",
            "path",
            "start_col",
            "start_line",
        }
    ),
    "Union": frozenset(
        {
            "absolute_path",
            "decorators",
            "docstring",
            "end_line",
            "is_exported",
            "modifiers",
            "path",
            "start_col",
            "start_line",
        }
    ),
    "ModuleInterface": frozenset({"absolute_path", "module_type"}),
    "ModuleImplementation": frozenset({"absolute_path", "module_type"}),
}

# `{name: type, other: type?}` -> {"name", "other"}
_PROPERTY_NAME = re.compile(r"(\w+):")


def _declared_properties(schema_text: str) -> set[str]:
    return set(_PROPERTY_NAME.findall(schema_text))


def _proto_fields(label: str) -> set[str] | None:
    """Field names of the proto message for `label`, or None if absent."""
    message = pb.DESCRIPTOR.message_types_by_name.get(label)
    return None if message is None else {field.name for field in message.fields}


def _unexported_properties(schemas: Iterable[Any]) -> list[str]:
    """Declared properties with no proto field and no exemption.

    Extracted so a test can run the REAL comparison over a stand-in schema.
    An inline copy inside a test would be a mirror -- mutating this function
    would leave the copy passing, which is what the first attempt at covering
    the missing-message branch actually did (#1491).
    """
    undeclared: list[str] = []
    for schema in schemas:
        label = schema.label.value
        fields = _proto_fields(label)
        if fields is None:
            # A node label with NO proto message at all exports nothing, which
            # is a larger loss than a single missing property. Skipping it here
            # would let a whole label slip through the check written to catch
            # exactly this -- the inventory-fails-open shape (#1490).
            #
            # Unreachable today: every label has a message. That is a fact
            # about the current proto, not a promise, so the branch is
            # reported rather than assumed away.
            undeclared.append(f"{label} (no proto message at all)")
            continue
        allowed = _NOT_EXPORTED.get(label, frozenset())
        for prop in sorted(_declared_properties(schema.properties)):
            if prop not in fields and prop not in allowed:
                undeclared.append(f"{label}.{prop}")
    return undeclared


def test_every_declared_property_is_exported_or_declared_unexported() -> None:
    """Default-deny: a new property must be exported or explicitly excluded.

    Reading the generated DESCRIPTORS rather than the `.proto` text, because a
    text search answers "does this name appear in the file" -- which a comment
    or an unrelated message satisfies -- and the question is "does this message
    carry this field".
    """
    undeclared = _unexported_properties(NODE_SCHEMAS)

    assert not undeclared, (
        f"{len(undeclared)} node propert(ies) are declared in NODE_SCHEMAS but "
        "have no field in codec/schema.proto and are not listed in "
        "_NOT_EXPORTED:\n"
        + "\n".join(undeclared)
        + "\n\nAdd the proto field and regenerate the bindings, or add the "
        "property to _NOT_EXPORTED with a reason (issue #1490)."
    )


def test_a_label_without_a_proto_message_is_reported() -> None:
    """A whole missing message must fail, not be skipped.

    The guard originally did `if fields is None: continue`, so a node label
    with no proto message at all passed silently -- exporting nothing, which
    is a larger loss than any single absent property. That is the
    inventory-fails-open shape the test exists to prevent, reproduced inside
    the test itself (reported on #1491).

    Exercised through `_proto_fields` on a label the proto does not carry,
    since every real label currently has a message. Unreachable today is a
    fact about the current proto, not a promise.
    """
    assert _proto_fields("NoSuchNodeLabel") is None, (
        "a label absent from the proto must report None, so the caller can "
        "distinguish it from a message that exists with no matching fields"
    )

    # The paired positive: a real label resolves, so the None above is the
    # absence rather than the helper being inert.
    assert _proto_fields("Module") is not None

    # And the COMPARISON must act on that None rather than skipping. Asserting
    # on `_proto_fields` alone left the silent-skip revert PASSING, because no
    # real label lacks a message -- so that assertion cannot reach the branch
    # it is about. Calls the real function; an inline copy would be a mirror.
    class _StandInSchema:
        class label:  # noqa: N801 - mimics the NodeSchema attribute shape
            value = "NoSuchNodeLabel"

        properties = "{qualified_name: string}"

    assert _unexported_properties([_StandInSchema()]) == [
        "NoSuchNodeLabel (no proto message at all)"
    ], "a label with no proto message must be reported, not skipped"


def test_the_unexported_list_names_only_real_absences() -> None:
    """A control: an entry that IS exported must be removed from the list.

    Without this the allow-list only grows. A property gains a proto field,
    its entry here becomes a lie, and the next reader cannot tell which
    entries are current -- the same staleness that makes an unmaintained
    exclusion list worse than none.
    """
    stale: list[str] = []
    for label, properties in _NOT_EXPORTED.items():
        fields = _proto_fields(label)
        if fields is None:
            continue
        stale.extend(f"{label}.{prop}" for prop in sorted(properties & fields))

    assert not stale, (
        f"{len(stale)} entr(ies) in _NOT_EXPORTED now HAVE a proto field and "
        "should be removed:\n" + "\n".join(stale)
    )


def test_the_unexported_list_names_only_declared_properties() -> None:
    """A second control: the list may not name properties that do not exist.

    A renamed or removed property would leave an entry that suppresses
    nothing, and a future property reusing that name would be silently
    exempted from the check above.
    """
    declared_by_label = {
        schema.label.value: _declared_properties(schema.properties)
        for schema in NODE_SCHEMAS
    }

    unknown: list[str] = []
    for label, properties in _NOT_EXPORTED.items():
        declared = declared_by_label.get(label)
        if declared is None:
            unknown.append(f"{label} (no such node schema)")
            continue
        unknown.extend(f"{label}.{prop}" for prop in sorted(properties - declared))

    assert not unknown, (
        f"{len(unknown)} entr(ies) in _NOT_EXPORTED do not name a declared "
        "property:\n" + "\n".join(unknown)
    )
