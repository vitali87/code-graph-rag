"""A Field and a Method may share `<owner>.<name>`; the export keeps both.

The protobuf writer used to key its node map by identity alone and keep the
first cross-label writer, so exporting a class with a field and a method both
called `total` retained one node and silently lost the other (local review of
#1899). The read side (`graph_diff._node_key`) identifies a node by payload
kind plus identity, so the writer now does too.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.services.protobuf_service import ProtobufFileIngestor


def _writer(tmp_path: Path) -> ProtobufFileIngestor:
    return ProtobufFileIngestor(output_path=str(tmp_path / "out"))


def _kinds(ingestor: ProtobufFileIngestor) -> list[str]:
    return sorted(
        n.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) for n in ingestor._nodes.values()
    )


def test_a_field_and_a_method_with_one_qualified_name_are_both_exported(
    tmp_path: Path,
) -> None:
    common = {
        cs.KEY_QUALIFIED_NAME: "demo.Acc.total",
        cs.KEY_NAME: "total",
        cs.KEY_PATH: "acc.py",
    }
    for first, second in (
        (cs.NodeLabel.FIELD, cs.NodeLabel.METHOD),
        (cs.NodeLabel.METHOD, cs.NodeLabel.FIELD),
    ):
        writer = _writer(tmp_path)
        writer.ensure_node_batch(first.value, dict(common))
        writer.ensure_node_batch(second.value, dict(common))
        assert _kinds(writer) == [cs.ONEOF_FIELD, "method"], _kinds(writer)


def test_a_repeated_ensure_of_one_node_still_merges(tmp_path: Path) -> None:
    """The control: same label twice is one node with the union of properties."""
    writer = _writer(tmp_path)
    writer.ensure_node_batch(
        cs.NodeLabel.FIELD.value,
        {cs.KEY_QUALIFIED_NAME: "demo.Acc.n", cs.KEY_NAME: "n"},
    )
    writer.ensure_node_batch(
        cs.NodeLabel.FIELD.value,
        {cs.KEY_QUALIFIED_NAME: "demo.Acc.n", cs.KEY_TYPE_NAME: "int"},
    )
    assert len(writer._nodes) == 1
    (node,) = writer._nodes.values()
    assert node.field.name == "n" and node.field.type_name == "int"


def test_field_payload_carries_no_absolute_path(tmp_path: Path) -> None:
    """Like every definition label: the canonical export must not differ per checkout."""
    writer = _writer(tmp_path)
    writer.ensure_node_batch(
        cs.NodeLabel.FIELD.value,
        {
            cs.KEY_QUALIFIED_NAME: "demo.Acc.n",
            cs.KEY_NAME: "n",
            cs.KEY_ABSOLUTE_PATH: "/home/x/acc.py",
        },
    )
    (node,) = writer._nodes.values()
    assert not hasattr(node.field, "absolute_path")
