# Issue #1139: the structural diff between two canonical snapshots. Identical
# states diff to nothing; a single added call site reports as exactly one new
# CALLS edge; a flow_covered flip lands in the coverage delta; artifacts from
# different codec schemas refuse to diff.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag.capture import ALL_ENABLED
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_diff import DiffError, diff_indexes, diff_is_empty
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.services.provenance import (
    MANIFEST_FILE,
    capture_description,
    source_state,
    write_manifest,
)


def _export(repo: Path, out: Path, capture=ALL_ENABLED) -> None:
    parsers, queries = load_parsers()
    ingestor = ProtobufFileIngestor(output_path=str(out), repo_path=str(repo))
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run(force=True)
    ingestor.flush_all()
    write_manifest(out, source_state(repo), capture_description(capture))


def _write(repo: Path, body: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text(body, encoding="utf-8")


_BASE = "def helper():\n    return 1\n\n\ndef use():\n    return 2\n"
_WITH_CALL = "def helper():\n    return 1\n\n\ndef use():\n    return helper()\n"


def test_identical_snapshots_diff_to_nothing(tmp_path: Path) -> None:
    repo_a = tmp_path / "a" / "proj"
    repo_b = tmp_path / "b" / "proj"
    _write(repo_a, _BASE)
    _write(repo_b, _BASE)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _export(repo_a, out_a)
    _export(repo_b, out_b)
    diff = diff_indexes(out_a, out_b)
    assert diff_is_empty(diff), diff


def test_one_new_call_reports_one_calls_edge(tmp_path: Path) -> None:
    repo_old = tmp_path / "old" / "proj"
    repo_new = tmp_path / "new" / "proj"
    _write(repo_old, _BASE)
    _write(repo_new, _WITH_CALL)
    out_old = tmp_path / "out_old"
    out_new = tmp_path / "out_new"
    _export(repo_old, out_old)
    _export(repo_new, out_new)
    diff = diff_indexes(out_old, out_new)
    calls = diff["relationships"].get("CALLS")
    assert calls is not None
    assert calls["added"] == ["proj.app.use -> proj.app.helper"]
    assert calls["removed"] == []
    assert diff["nodes"]["added"] == []
    assert diff["nodes"]["removed"] == []


def test_flow_covered_flip_lands_in_coverage_delta(tmp_path: Path) -> None:
    from codebase_rag.capture import resolve_capture

    repo_old = tmp_path / "old" / "proj"
    repo_new = tmp_path / "new" / "proj"
    _write(repo_old, _BASE)
    _write(repo_new, _BASE)
    out_old = tmp_path / "out_old"
    out_new = tmp_path / "out_new"
    _export(repo_old, out_old, capture=resolve_capture(["defaults"]))
    _export(repo_new, out_new, capture=ALL_ENABLED)
    diff = diff_indexes(out_old, out_new)
    flips = diff["coverage"]["flow_covered_flips"]
    assert any("proj.app" in key for key in flips), diff["coverage"]
    for delta in flips.values():
        assert delta == {"old": False, "new": True}


def test_cross_schema_artifacts_refuse_to_diff(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write(repo, _BASE)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _export(repo, out_a)
    _export(repo, out_b)
    manifest_path = out_b / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["codec_schema_sha256"] = "f" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DiffError, match="schema mismatch"):
        diff_indexes(out_a, out_b)


def test_missing_manifest_refuses_to_diff(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write(repo, _BASE)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _export(repo, out_a)
    _export(repo, out_b)
    (out_b / MANIFEST_FILE).unlink()
    with pytest.raises(DiffError, match="schema metadata missing"):
        diff_indexes(out_a, out_b)


def test_missing_schema_hash_refuses_to_diff(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write(repo, _BASE)
    out_a = tmp_path / "out_a"
    out_b = tmp_path / "out_b"
    _export(repo, out_a)
    _export(repo, out_b)
    manifest_path = out_a / MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["codec_schema_sha256"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DiffError, match="schema metadata missing"):
        diff_indexes(out_a, out_b)


def test_bool_flip_reports_declared_defaults_not_null(tmp_path: Path) -> None:
    from codebase_rag.capture import resolve_capture

    repo_old = tmp_path / "old" / "proj"
    repo_new = tmp_path / "new" / "proj"
    _write(repo_old, _BASE)
    _write(repo_new, _BASE)
    out_old = tmp_path / "out_old"
    out_new = tmp_path / "out_new"
    _export(repo_old, out_old, capture=resolve_capture(["defaults"]))
    _export(repo_new, out_new, capture=ALL_ENABLED)
    diff = diff_indexes(out_old, out_new)
    changed = diff["nodes"]["changed"]
    flips = [
        delta["flow_covered"] for delta in changed.values() if "flow_covered" in delta
    ]
    assert flips
    for flip in flips:
        assert flip == {"old": False, "new": True}
