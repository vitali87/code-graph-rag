# Guards the protobuf relationship enum against the Python one (issue #1447).
#
# `ProtobufFileIngestor.ensure_relationship_batch` resolves a relationship type
# by name against the generated enum and falls back to
# RELATIONSHIP_TYPE_UNSPECIFIED when the name is absent, logging a warning. The
# edge is still written, so the failure is silent in normal use: the index
# gains an untyped relationship rather than an error.
#
# That fallback is the right runtime behaviour — a stale index should degrade
# rather than crash — but it means enum drift can only be caught here. Nine
# types had already drifted when this was written.
from __future__ import annotations

from codebase_rag import constants as cs
from codec import schema_pb2 as pb

# The zero value is protobuf's required unset sentinel, not a relationship.
_UNSPECIFIED = "RELATIONSHIP_TYPE_UNSPECIFIED"


def _proto_relationship_names() -> set[str]:
    return {
        name for name in pb.Relationship.RelationshipType.keys() if name != _UNSPECIFIED
    }


def test_every_python_relationship_type_exists_in_the_proto_schema() -> None:
    """A type missing here exports as UNSPECIFIED and loses its identity.

    This is the assertion that makes the drift class unrepresentable: adding a
    member to `RelationshipType` without adding it to `codec/schema.proto` is
    otherwise a one-line change with no signal attached.
    """
    python_names = {member.name for member in cs.RelationshipType}
    missing = sorted(python_names - _proto_relationship_names())

    assert not missing, (
        f"relationship types absent from codec/schema.proto: {missing}. "
        "Add them to the RelationshipType enum, regenerate the bindings, and "
        "keep the numbering append-only."
    )


def test_proto_schema_has_no_relationship_the_python_enum_lacks() -> None:
    """The paired direction, so parity is asserted rather than containment.

    Without this, deleting a Python member would leave an orphan in the wire
    format that nothing can ever emit, and the test above would still pass.
    """
    python_names = {member.name for member in cs.RelationshipType}
    orphaned = sorted(_proto_relationship_names() - python_names)

    assert not orphaned, (
        f"proto relationship types with no RelationshipType member: {orphaned}"
    )


def test_the_unspecified_sentinel_is_still_zero() -> None:
    """The fallback in ensure_relationship_batch depends on this.

    A control for the two tests above: if the generated module stopped
    exposing the enum in the expected shape, `keys()` could return something
    empty or unexpected and both parity assertions would pass vacuously.
    """
    assert pb.Relationship.RelationshipType.Value(_UNSPECIFIED) == 0
    assert _proto_relationship_names(), "proto relationship enum read as empty"
