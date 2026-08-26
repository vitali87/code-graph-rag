# Guards the protobuf node payload messages against NodeLabel (issue #1452).
#
# The node-side twin of test_protobuf_relationship_parity. The failure mode is
# worse here: an unknown relationship type still writes an edge, untyped, but
# `ProtobufFileIngestor.ensure_node_batch` returns early when a label has no
# oneof mapping, so the node is dropped from the index entirely with only a
# logger.warning. Three labels had drifted when this was written.
from __future__ import annotations

from codebase_rag import constants as cs
from codec import schema_pb2 as pb

# NodeLabel members with no protobuf payload, by deliberate decision rather
# than drift. Empty today; an entry here needs a comment saying why the label
# is not representable in an index.
_NOT_IN_PROTO: frozenset[str] = frozenset()


def _proto_payload_names() -> set[str]:
    """The message name behind each `Node.payload` oneof field."""
    oneof = pb.Node.DESCRIPTOR.oneofs_by_name[cs.PROTOBUF_PAYLOAD_ONEOF]
    return {field.message_type.name for field in oneof.fields}


def test_every_node_label_has_a_proto_payload() -> None:
    """A label with no payload message is dropped, not merely mistyped.

    `ensure_node_batch` looks the label up in LABEL_TO_ONEOF_FIELD and returns
    when it is absent, so the node never reaches the index. Nothing downstream
    can tell that from a repository that genuinely contains no such node.
    """
    labels = {member.value for member in cs.NodeLabel}
    missing = sorted(labels - _proto_payload_names() - _NOT_IN_PROTO)

    assert not missing, (
        f"node labels absent from codec/schema.proto: {missing}. Add a payload "
        "message and a LABEL_TO_ONEOF_FIELD mapping, or record the label in "
        "_NOT_IN_PROTO with the reason it cannot be represented."
    )


def test_proto_has_no_payload_without_a_node_label() -> None:
    """The paired direction, so this asserts parity rather than containment.

    An orphaned payload is a message nothing can ever populate, and the test
    above would still pass with one present.
    """
    labels = {member.value for member in cs.NodeLabel}
    orphaned = sorted(_proto_payload_names() - labels)

    assert not orphaned, f"proto payload messages with no NodeLabel: {orphaned}"


def test_every_node_label_maps_to_a_oneof_field() -> None:
    """Parity with the proto is not enough; the ingestor needs the mapping.

    A payload message that exists but is unmapped fails exactly as an absent
    one does, silently, so the mapping is asserted separately from the schema.
    """
    from codebase_rag.services.protobuf_service import LABEL_TO_ONEOF_FIELD

    unmapped = sorted(
        member.value
        for member in cs.NodeLabel
        if member not in LABEL_TO_ONEOF_FIELD and member.value not in _NOT_IN_PROTO
    )

    assert not unmapped, f"node labels with no oneof mapping: {unmapped}"


def test_the_payload_oneof_reads_non_empty() -> None:
    """A control for the three tests above.

    Each compares against `_proto_payload_names()`; if the descriptor lookup
    stopped returning fields, the set comparisons would pass vacuously in the
    directions that matter.
    """
    assert _proto_payload_names(), "proto Node payload oneof read as empty"
