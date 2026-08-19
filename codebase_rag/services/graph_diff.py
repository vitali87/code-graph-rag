"""Structural diff between two canonical protobuf indexes (issue #1139).

A versioned snapshot sequence is only as useful as the ability to compare two
of them: which functions gained or lost CALLS edges, which FLOWS_TO paths
appeared, which modules changed coverage. Nodes match on (kind, identity,
path) — path included because a qualified name alone is not unique in the
Rust cfg-twin cases (#1017) — and relationships on (source, type, target).
Renames report as remove+add for the first cut. Cross-schema diffs are
refused: both artifacts must record the same codec schema hash in their
manifests, because field semantics may differ between schema versions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

import codec.schema_pb2 as pb

from .. import constants as cs
from .provenance import MANIFEST_FILE, _coverage_from_nodes

type JsonDict = dict[str, Any]

_NODE_FILES = (cs.PROTOBUF_INDEX_FILE, cs.PROTOBUF_NODES_FILE)
_REL_FILES = (cs.PROTOBUF_INDEX_FILE, cs.PROTOBUF_RELS_FILE)


class NodeKey(NamedTuple):
    kind: str
    identity: str
    path: str


class DiffError(ValueError):
    """The two artifacts cannot be diffed meaningfully."""


def _load_indexes(index_dir: Path, names: tuple[str, ...]) -> list[pb.GraphCodeIndex]:
    found = []
    for name in names:
        artifact = index_dir / name
        if not artifact.is_file():
            continue
        index = pb.GraphCodeIndex()
        index.ParseFromString(artifact.read_bytes())
        found.append(index)
    if not found:
        raise DiffError(f"no index artifacts in {index_dir}")
    return found


def _schema_hash(index_dir: Path) -> str | None:
    manifest_path = index_dir / MANIFEST_FILE
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = manifest.get("codec_schema_sha256") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) else None


def _require_same_schema(old_dir: Path, new_dir: Path) -> None:
    old_hash = _schema_hash(old_dir)
    new_hash = _schema_hash(new_dir)
    if old_hash is None or new_hash is None:
        # An absent/malformed manifest or missing hash means compatibility
        # cannot be verified at all; proceeding would produce a delta with
        # unknowable field semantics.
        missing = old_dir if old_hash is None else new_dir
        raise DiffError(
            f"schema metadata missing: no codec_schema_sha256 for {missing}; "
            "re-export with a manifest before diffing"
        )
    if old_hash != new_hash:
        raise DiffError(
            "schema mismatch: the artifacts were produced by different codec "
            f"schemas ({old_hash[:12]} vs {new_hash[:12]}); field semantics "
            "may differ, so the diff would be meaningless"
        )


def _payload_fields(message) -> JsonDict:
    fields: JsonDict = {}
    # Iterate the DESCRIPTOR, not ListFields(): proto3 implicit-presence
    # scalars (flow_covered=false, start_line=0) are omitted by ListFields,
    # which would report one side of a boolean flip as null instead of its
    # declared default. Repeated containers expose extend(); scalars never do
    # (avoids the descriptor.label deprecation and the property/method drift
    # of is_repeated across protobuf versions).
    for descriptor in message.DESCRIPTOR.fields:
        value = getattr(message, descriptor.name)
        if hasattr(value, "extend"):
            if value:
                fields[descriptor.name] = [str(v) for v in value]
        else:
            fields[descriptor.name] = value
    return fields


def _node_key(node: pb.Node) -> NodeKey | None:
    kind = node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF)
    if kind is None:
        return None
    payload = getattr(node, kind)
    fields = {d.name for d, _v in payload.ListFields()}
    identity = ""
    if "qualified_name" in fields:
        identity = payload.qualified_name
    elif "path" in fields:
        identity = payload.path
    elif "name" in fields:
        identity = payload.name
    path = payload.path if "path" in fields else ""
    return NodeKey(kind, identity, path)


def _node_map(indexes: list[pb.GraphCodeIndex]) -> dict[NodeKey, JsonDict]:
    nodes: dict[NodeKey, JsonDict] = {}
    for index in indexes:
        for node in index.nodes:
            key = _node_key(node)
            if key is None:
                continue
            kind = node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF)
            nodes[key] = _payload_fields(getattr(node, kind))
    return nodes


def _rel_map(indexes: list[pb.GraphCodeIndex]) -> dict[tuple[str, str, str], JsonDict]:
    rels: dict[tuple[str, str, str], JsonDict] = {}
    for index in indexes:
        for rel in index.relationships:
            key = (
                rel.source_id,
                pb.Relationship.RelationshipType.Name(rel.type),
                rel.target_id,
            )
            rels[key] = dict(rel.properties)
    return rels


def _changed_fields(old: JsonDict, new: JsonDict) -> JsonDict:
    delta: JsonDict = {}
    for field in sorted(set(old) | set(new)):
        if old.get(field) != new.get(field):
            delta[field] = {"old": old.get(field), "new": new.get(field)}
    return delta


def _node_delta(
    old_nodes: dict[NodeKey, JsonDict], new_nodes: dict[NodeKey, JsonDict]
) -> JsonDict:
    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    changed = {}
    for key in sorted(set(old_nodes) & set(new_nodes)):
        delta = _changed_fields(old_nodes[key], new_nodes[key])
        if delta:
            changed["::".join(key)] = delta
    return {
        "added": ["::".join(k) for k in added],
        "removed": ["::".join(k) for k in removed],
        "changed": changed,
    }


def _rel_delta(
    old_rels: dict[tuple[str, str, str], JsonDict],
    new_rels: dict[tuple[str, str, str], JsonDict],
) -> JsonDict:
    by_type: dict[str, dict[str, list[str] | JsonDict]] = {}

    def _bucket(rel_type: str) -> dict:
        return by_type.setdefault(rel_type, {"added": [], "removed": [], "changed": {}})

    for src, rel_type, dst in sorted(set(new_rels) - set(old_rels)):
        _bucket(rel_type)["added"].append(f"{src} -> {dst}")
    for src, rel_type, dst in sorted(set(old_rels) - set(new_rels)):
        _bucket(rel_type)["removed"].append(f"{src} -> {dst}")
    for key in sorted(set(old_rels) & set(new_rels)):
        delta = _changed_fields(old_rels[key], new_rels[key])
        if delta:
            src, rel_type, dst = key
            _bucket(rel_type)["changed"][f"{src} -> {dst}"] = delta
    return dict(sorted(by_type.items()))


def _coverage_delta(
    old_indexes: list[pb.GraphCodeIndex], new_indexes: list[pb.GraphCodeIndex]
) -> JsonDict:
    old_nodes = [n for i in old_indexes for n in i.nodes]
    new_nodes = [n for i in new_indexes for n in i.nodes]
    old_cov = _coverage_from_nodes(old_nodes)
    new_cov = _coverage_from_nodes(new_nodes)
    flips = {}
    old_flow = {
        _node_key(n): n.module.flow_covered
        for n in old_nodes
        if n.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == cs.ONEOF_MODULE
    }
    for n in new_nodes:
        if n.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) != cs.ONEOF_MODULE:
            continue
        key = _node_key(n)
        if (
            key is not None
            and key in old_flow
            and old_flow[key] != n.module.flow_covered
        ):
            flips["::".join(key)] = {
                "old": old_flow[key],
                "new": n.module.flow_covered,
            }
    return {
        "flow_covered_flips": flips,
        "per_language": _changed_fields(old_cov, new_cov),
    }


def diff_indexes(old_dir: Path, new_dir: Path) -> JsonDict:
    """The structural delta between two canonical exports, per category."""
    _require_same_schema(old_dir, new_dir)
    old_nodes_idx = _load_indexes(old_dir, _NODE_FILES)
    new_nodes_idx = _load_indexes(new_dir, _NODE_FILES)
    old_rels_idx = _load_indexes(old_dir, _REL_FILES)
    new_rels_idx = _load_indexes(new_dir, _REL_FILES)
    return {
        "nodes": _node_delta(_node_map(old_nodes_idx), _node_map(new_nodes_idx)),
        "relationships": _rel_delta(_rel_map(old_rels_idx), _rel_map(new_rels_idx)),
        "coverage": _coverage_delta(old_nodes_idx, new_nodes_idx),
    }


def diff_is_empty(diff: JsonDict) -> bool:
    nodes = diff["nodes"]
    coverage = diff["coverage"]
    return (
        not nodes["added"]
        and not nodes["removed"]
        and not nodes["changed"]
        and not diff["relationships"]
        and not coverage["flow_covered_flips"]
        and not coverage["per_language"]
    )
