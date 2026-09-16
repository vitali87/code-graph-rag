import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import codec.schema_pb2 as pb
from codebase_rag.cli import app
from codebase_rag.constants import (
    PROTOBUF_INDEX_FILE,
    PROTOBUF_NODES_FILE,
    PROTOBUF_RELS_FILE,
)
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.services.provenance import (
    MANIFEST_FILE,
    build_manifest,
    verify_index,
    write_manifest,
)

SPLIT_FILES = (PROTOBUF_NODES_FILE, PROTOBUF_RELS_FILE)


def _export_layout_graph(out: Path, *, split: bool, populated: bool = True) -> None:
    ingestor = ProtobufFileIngestor(str(out), split_index=split)
    if populated:
        ingestor.ensure_node_batch(
            "Module", {"qualified_name": "proj.app", "path": "app.py"}
        )
        ingestor.ensure_node_batch("Function", {"qualified_name": "proj.app.run"})
        ingestor.ensure_relationship_batch(
            ("Module", "qualified_name", "proj.app"),
            "DEFINES",
            ("Function", "qualified_name", "proj.app.run"),
        )
    ingestor.flush_all()
    artifact_names = SPLIT_FILES if split else (PROTOBUF_INDEX_FILE,)
    indexes = [
        pb.GraphCodeIndex.FromString((out / name).read_bytes())
        for name in artifact_names
    ]
    assert sum(len(index.nodes) for index in indexes) == (2 if populated else 0)
    assert sum(len(index.relationships) for index in indexes) == (1 if populated else 0)


@pytest.mark.parametrize("missing", SPLIT_FILES)
def test_verify_command_rejects_a_self_consistent_half_split_index(
    tmp_path: Path, missing: str
) -> None:
    out = tmp_path / "index"
    _export_layout_graph(out, split=True)
    manifest_path = write_manifest(out, {}, {})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (out / missing).unlink()
    del manifest["artifacts"][missing]
    if missing == PROTOBUF_NODES_FILE:
        manifest["coverage"] = {}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    trusted_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    result = CliRunner().invoke(app, ["verify-index", "-i", str(out)])

    assert result.exit_code == 1, result.output
    assert "incomplete split index" in result.output
    assert missing in result.output
    assert "Index verified" not in result.output
    assert any("incomplete split index" in p for p in verify_index(out))
    assert any("incomplete split index" in p for p in verify_index(out, trusted_digest))


@pytest.mark.parametrize("missing", SPLIT_FILES)
def test_build_manifest_rejects_a_half_split_index(
    tmp_path: Path, missing: str
) -> None:
    out = tmp_path / "index"
    _export_layout_graph(out, split=True)
    (out / missing).unlink()

    with pytest.raises(ValueError, match="incomplete split index"):
        build_manifest(out, {}, {})

    assert not (out / MANIFEST_FILE).exists()


@pytest.mark.parametrize(
    "split_files", [(PROTOBUF_NODES_FILE,), (PROTOBUF_RELS_FILE,), SPLIT_FILES]
)
def test_verify_command_rejects_joint_with_any_split_artifact(
    tmp_path: Path, split_files: tuple[str, ...]
) -> None:
    out = tmp_path / "index"
    _export_layout_graph(out, split=True)
    split_contents = {name: (out / name).read_bytes() for name in split_files}
    _export_layout_graph(out, split=False)
    manifest_path = write_manifest(out, {}, {})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, content in split_contents.items():
        (out / name).write_bytes(content)
        manifest["artifacts"][name] = {"sha256": hashlib.sha256(content).hexdigest()}
    if PROTOBUF_NODES_FILE in split_files:
        manifest["coverage"]["python"]["modules"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = CliRunner().invoke(app, ["verify-index", "-i", str(out)])

    assert result.exit_code == 1, result.output
    assert "mixed index layouts" in result.output
    assert "Index verified" not in result.output
    assert any("mixed index layouts" in p for p in verify_index(out))


@pytest.mark.parametrize(
    "split_files", [(PROTOBUF_NODES_FILE,), (PROTOBUF_RELS_FILE,), SPLIT_FILES]
)
def test_build_manifest_rejects_joint_with_any_split_artifact(
    tmp_path: Path, split_files: tuple[str, ...]
) -> None:
    out = tmp_path / "index"
    _export_layout_graph(out, split=True)
    split_contents = {name: (out / name).read_bytes() for name in split_files}
    _export_layout_graph(out, split=False)
    for name, content in split_contents.items():
        (out / name).write_bytes(content)

    with pytest.raises(ValueError, match="mixed index layouts"):
        build_manifest(out, {}, {})


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("populated", [False, True])
def test_complete_layout_verifies_even_for_an_empty_graph(
    tmp_path: Path, split: bool, populated: bool
) -> None:
    out = tmp_path / "index"
    _export_layout_graph(out, split=split, populated=populated)
    manifest_path = write_manifest(out, {}, {})
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_files = set(SPLIT_FILES if split else (PROTOBUF_INDEX_FILE,))
    assert set(manifest["artifacts"]) == expected_files
    assert manifest["coverage"] == (
        {"python": {"modules": 1, "flow_covered": 0}} if populated else {}
    )

    result = CliRunner().invoke(app, ["verify-index", "-i", str(out)])

    assert result.exit_code == 0, result.output
    assert "Index verified" in result.output
    assert verify_index(out) == []
