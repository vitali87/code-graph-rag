"""Provenance manifest for the protobuf index (issue #1138).

The canonical artifact (sorted nodes/relationships, deterministic protobuf
serialization) is a byte-stable fingerprint of one analyzed source state. The
manifest binds it to the state that produced it: source commit (plus a
dirty-tree flag), analyzer and codec versions, the capture configuration, and
a per-language coverage summary computed FROM the artifact itself, so the
claims can never drift from the content. `verify_index` re-derives the
hashes and the coverage summary and reports every mismatch; CI attestation
of the manifest then extends the trust chain to a signer identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import codec.schema_pb2 as pb

from .. import constants as cs
from ..language_spec import get_language_spec

type JsonDict = dict[str, object]
_DIST_NAME = "code-graph-rag"

MANIFEST_FILE = "manifest.json"
_MANIFEST_VERSION = 1
_HASH_ALGORITHM = "sha256"
_SCHEMA_FILE = Path(pb.__file__).parent / "schema.proto"
_ARTIFACT_FILES = (
    cs.PROTOBUF_INDEX_FILE,
    cs.PROTOBUF_NODES_FILE,
    cs.PROTOBUF_RELS_FILE,
)


def _open_nofollow(path: Path):
    # O_NOFOLLOW makes refusing a symlink and opening the file ONE atomic
    # operation, so a link swapped in between a check and the read still
    # fails (ELOOP) instead of leaking an external file's content. Platforms
    # without the flag (Windows) degrade to a plain open behind the explicit
    # is_symlink() checks.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags), "rb")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _open_nofollow(path) as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_line(repo_path: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _source_state(repo_path: Path) -> JsonDict:
    commit = _git_line(repo_path, "rev-parse", "HEAD")
    status = _git_line(repo_path, "status", "--porcelain")
    return {
        "commit": commit,
        # None when the state is unknowable (not a git repo): a verifier must
        # not read "unknown" as "clean".
        "dirty": bool(status) if status is not None else None,
    }


def _module_language(path: str) -> str:
    suffix = Path(path).suffix
    spec = get_language_spec(suffix) if suffix else None
    return str(spec.language) if spec is not None else "unknown"


def _coverage_from_nodes(nodes) -> JsonDict:
    per_language: dict[str, dict[str, int]] = {}
    for node in nodes:
        if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) != cs.ONEOF_MODULE:
            continue
        module = node.module
        language = _module_language(module.path)
        row = per_language.setdefault(language, {"modules": 0, "flow_covered": 0})
        row["modules"] += 1
        if module.flow_covered:
            row["flow_covered"] += 1
    return {lang: per_language[lang] for lang in sorted(per_language)}


def _coverage_summary(index_dir: Path) -> JsonDict:
    nodes = []
    for name in (cs.PROTOBUF_INDEX_FILE, cs.PROTOBUF_NODES_FILE):
        artifact = index_dir / name
        if not artifact.is_file():
            continue
        index = pb.GraphCodeIndex()
        try:
            with _open_nofollow(artifact) as handle:
                index.ParseFromString(handle.read())
        except OSError:
            continue
        nodes.extend(index.nodes)
    return _coverage_from_nodes(nodes)


def _analyzer_version() -> str:
    try:
        return metadata.version(_DIST_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def capture_description(selection) -> JsonDict:
    # The EFFECTIVE configuration, not the raw CLI tokens: CGR_CAPTURE and
    # order-dependent overrides all funnel into the resolved selection, so two
    # identical selections always record identically and an environment-only
    # configuration is never recorded as null.
    return {
        "relationships": sorted(str(rel) for rel in selection.enabled_rels),
        "node_labels": sorted(str(label) for label in selection.enabled_node_labels),
    }


def source_state(repo_path: Path) -> JsonDict:
    """Snapshot the git state; call BEFORE indexing so artifacts written into
    the tree (or edits racing the run) cannot skew the recorded state."""
    return _source_state(repo_path)


def build_manifest(
    index_dir: Path,
    source: JsonDict,
    capture: JsonDict,
) -> JsonDict:
    joint = (index_dir / cs.PROTOBUF_INDEX_FILE).is_file()
    split = (index_dir / cs.PROTOBUF_NODES_FILE).is_file()
    if joint and split:
        raise ValueError(
            "mixed index layouts: both the joint and the split artifacts exist"
        )
    artifacts = {
        name: {_HASH_ALGORITHM: _sha256(index_dir / name)}
        for name in _ARTIFACT_FILES
        if (index_dir / name).is_file()
    }
    return {
        "manifest_version": _MANIFEST_VERSION,
        "analyzer_version": _analyzer_version(),
        "codec_schema_sha256": (
            _sha256(_SCHEMA_FILE) if _SCHEMA_FILE.is_file() else None
        ),
        "capture": capture,
        "source": source,
        "artifacts": artifacts,
        "coverage": _coverage_summary(index_dir),
        # Provenance metadata only: the timestamp binds nothing and is
        # deliberately outside every hash so double exports stay comparable.
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def write_manifest(
    index_dir: Path,
    source: JsonDict,
    capture: JsonDict,
) -> Path:
    manifest = build_manifest(index_dir, source, capture)
    index_dir.mkdir(parents=True, exist_ok=True)
    out_path = index_dir / MANIFEST_FILE
    out_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return out_path


def _load_manifest(manifest_path: Path) -> tuple[dict | None, list[str]]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None, [f"manifest missing: {manifest_path}"]
    try:
        with _open_nofollow(manifest_path) as handle:
            manifest = json.loads(handle.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        return None, [f"manifest unreadable: {error}"]
    if not isinstance(manifest, dict):
        return None, ["manifest is not a JSON object"]
    return manifest, []


def _check_trusted_digest(manifest_path: Path, trusted: str) -> list[str]:
    try:
        actual = _sha256(manifest_path)
    except OSError as error:
        return [f"manifest unreadable: {error}"]
    if actual != trusted.lower():
        return [
            "manifest digest does not match the trusted digest: "
            f"expected {trusted.lower()} got {actual}"
        ]
    return []


def _coverage_problems(index_dir: Path, manifest: dict) -> list[str]:
    claimed = manifest.get("coverage")
    actual_coverage = _coverage_summary(index_dir)
    if claimed != actual_coverage:
        return [
            "coverage summary disagrees with the graph: "
            f"manifest {claimed} vs graph {actual_coverage}"
        ]
    return []


def _artifact_problem(index_dir: Path, name: str, expected: object) -> str | None:
    if name not in _ARTIFACT_FILES:
        # A crafted manifest must never steer verification at files outside
        # the index directory (path traversal / absolute names).
        return f"unknown artifact name in manifest: {name}"
    artifact = index_dir / name
    if artifact.is_symlink() or not artifact.is_file():
        return f"artifact missing: {name}"
    try:
        actual = _sha256(artifact)
    except OSError:
        return f"artifact missing: {name}"
    if expected != actual:
        return f"artifact hash mismatch: {name} expected {expected} got {actual}"
    return None


def _check_artifacts(index_dir: Path, artifacts: dict) -> list[str]:
    problems: list[str] = []
    if cs.PROTOBUF_INDEX_FILE in artifacts and cs.PROTOBUF_NODES_FILE in artifacts:
        problems.append(
            "mixed index layouts: manifest covers both joint and split artifacts"
        )
    for name, hashes in artifacts.items():
        expected = hashes.get(_HASH_ALGORITHM) if isinstance(hashes, dict) else None
        if (problem := _artifact_problem(index_dir, name, expected)) is not None:
            problems.append(problem)
    for name in _ARTIFACT_FILES:
        if (index_dir / name).is_file() and name not in artifacts:
            problems.append(f"artifact not covered by manifest: {name}")
    return problems


def verify_index(
    index_dir: Path, trusted_manifest_sha256: str | None = None
) -> list[str]:
    """Every way the artifact/manifest binding can be broken, as messages;
    empty means verified.

    Local verification alone proves internal consistency, not authorship: a
    writer who can replace both an artifact and its recorded hash defeats it.
    Passing trusted_manifest_sha256 (the digest an attestation vouches for)
    anchors the whole chain: manifest bytes -> artifact hashes -> artifacts.
    """
    manifest_path = index_dir / MANIFEST_FILE
    manifest, problems = _load_manifest(manifest_path)
    if manifest is None:
        return problems
    if trusted_manifest_sha256 is not None:
        digest_problems = _check_trusted_digest(manifest_path, trusted_manifest_sha256)
        if digest_problems:
            return digest_problems
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        problems.append("manifest lists no artifacts")
        artifacts = {}
    problems.extend(_check_artifacts(index_dir, artifacts))
    if not problems:
        problems.extend(_coverage_problems(index_dir, manifest))
    return problems
