# Issue #1138: the protobuf index is canonical (two exports of one source
# state are byte-identical) and the provenance manifest binds the artifact to
# the state that produced it; verify_index must fail on every tampered part
# and on a manifest whose coverage claims disagree with the graph itself.
from __future__ import annotations

import json
from pathlib import Path

from codebase_rag.capture import ALL_ENABLED, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.services.provenance import (
    MANIFEST_FILE,
    build_manifest,
    capture_description,
    source_state,
    verify_index,
    write_manifest,
)

_INDEX_FILE = "index.bin"


def _write_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        'import os\n\n\ndef leak():\n    v = os.getenv("TOKEN")\n    print(v)\n',
        encoding="utf-8",
    )
    (root / "util.lua").write_text(
        "local function f() return 1 end\n", encoding="utf-8"
    )


def _export(repo: Path, out: Path) -> None:
    parsers, queries = load_parsers()
    ingestor = ProtobufFileIngestor(output_path=str(out), repo_path=str(repo))
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    ).run(force=True)
    ingestor.flush_all()


def test_double_export_is_byte_identical(tmp_path: Path) -> None:
    repo_a = tmp_path / "a" / "proj"
    repo_b = tmp_path / "b" / "proj"
    _write_repo(repo_a)
    _write_repo(repo_b)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _export(repo_a, out_a)
    _export(repo_b, out_b)
    assert (out_a / _INDEX_FILE).read_bytes() == (out_b / _INDEX_FILE).read_bytes()


def test_manifest_coverage_agrees_and_verify_passes(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    write_manifest(
        out, source_state(repo), capture_description(resolve_capture(["io"]))
    )
    manifest = json.loads((out / MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["coverage"]["python"]["modules"] == 1
    assert manifest["coverage"]["lua"]["modules"] == 1
    assert "READS_FROM" in manifest["capture"]["relationships"]
    assert manifest["artifacts"][_INDEX_FILE]["sha256"]
    assert verify_index(out) == []


def test_verify_fails_on_tampered_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    artifact = out / _INDEX_FILE
    blob = bytearray(artifact.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    artifact.write_bytes(bytes(blob))
    problems = verify_index(out)
    assert any("hash mismatch" in p for p in problems)


def test_verify_fails_on_tampered_manifest_claims(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    manifest_path = out / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["coverage"]["python"]["flow_covered"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = verify_index(out)
    assert any("coverage summary disagrees" in p for p in problems)


def test_verify_fails_on_missing_manifest_and_uncovered_artifact(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    assert any("manifest missing" in p for p in verify_index(out))
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    manifest_path = out / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["artifacts"][_INDEX_FILE]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any("not covered by manifest" in p for p in verify_index(out))


def test_manifest_records_source_state(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    manifest = build_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    # tmp_path is not a git repo: the state is unknowable, never "clean".
    assert manifest["source"]["commit"] is None
    assert manifest["source"]["dirty"] is None
    assert manifest["analyzer_version"]
    assert manifest["codec_schema_sha256"]


def test_verify_rejects_foreign_artifact_names(tmp_path: Path) -> None:
    # A crafted manifest must never steer hashing at files outside the index
    # directory: unknown names (traversal, absolute paths) are rejected
    # before any file access.
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cr3t", encoding="utf-8")
    manifest_path = out / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["../secret.txt"] = {"sha256": "0" * 64}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = verify_index(out)
    assert any("unknown artifact name" in p for p in problems)
    assert not any("s3cr3t" in p or "secret" in p and "hash" in p for p in problems)


def test_flush_layouts_are_mutually_exclusive(tmp_path: Path) -> None:
    # Reusing one output directory across joint and split runs must never
    # leave both layouts behind (the manifest would double-count coverage).
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    assert (out / _INDEX_FILE).is_file()
    parsers, queries = load_parsers()
    ingestor = ProtobufFileIngestor(
        output_path=str(out), split_index=True, repo_path=str(repo)
    )
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=ALL_ENABLED,
    ).run(force=True)
    ingestor.flush_all()
    assert not (out / _INDEX_FILE).exists()
    assert (out / "nodes.bin").is_file()
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    assert verify_index(out) == []


def test_trusted_manifest_digest_anchors_verification(tmp_path: Path) -> None:
    import hashlib

    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    manifest_path = write_manifest(
        out, source_state(repo), capture_description(ALL_ENABLED)
    )
    good = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert verify_index(out, good) == []
    assert verify_index(out, good.upper()) == []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["analyzer_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = verify_index(out, good)
    assert any("trusted digest" in p for p in problems)


def test_verify_rejects_symlinked_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    out = tmp_path / "out"
    _export(repo, out)
    write_manifest(out, source_state(repo), capture_description(ALL_ENABLED))
    outside = tmp_path / "outside.bin"
    outside.write_bytes((out / _INDEX_FILE).read_bytes())
    (out / _INDEX_FILE).unlink()
    (out / _INDEX_FILE).symlink_to(outside)
    problems = verify_index(out)
    assert any("artifact missing" in p for p in problems)
