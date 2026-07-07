from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger

from . import constants as cs
from . import logs as ls

# (H) Relationships that are usually meaningless without a companion still
# (H) enabled. Dropping the companion is obeyed, but warned about.
_SOFT_DEPENDENCIES: dict[cs.RelationshipType, cs.RelationshipType] = {
    cs.RelationshipType.OVERRIDES: cs.RelationshipType.INHERITS,
}


@dataclass(frozen=True)
class CaptureSelection:
    enabled_rels: frozenset[cs.RelationshipType]
    enabled_node_labels: frozenset[cs.NodeLabel]

    def rel_enabled(self, rel: cs.RelationshipType) -> bool:
        return rel in self.enabled_rels

    def node_enabled(self, label: cs.NodeLabel) -> bool:
        return label in self.enabled_node_labels

    @property
    def io_enabled(self) -> bool:
        return (
            cs.RelationshipType.READS_FROM in self.enabled_rels
            or cs.RelationshipType.WRITES_TO in self.enabled_rels
        )


def _node_labels_for(
    enabled_rels: frozenset[cs.RelationshipType],
) -> frozenset[cs.NodeLabel]:
    owner_of: dict[cs.NodeLabel, cs.CaptureGroup] = {
        label: group
        for group, labels in cs.CAPTURE_GROUP_NODE_LABELS.items()
        for label in labels
    }
    enabled: set[cs.NodeLabel] = set()
    for label in cs.NodeLabel:
        owner = owner_of.get(label)
        if owner is None or cs.CAPTURE_GROUP_RELS[owner] & enabled_rels:
            enabled.add(label)
    return frozenset(enabled)


def _selection_for(
    enabled_rels: frozenset[cs.RelationshipType],
) -> CaptureSelection:
    for rel, companion in _SOFT_DEPENDENCIES.items():
        if rel in enabled_rels and companion not in enabled_rels:
            logger.warning(ls.CAPTURE_DEPENDENCY_GAP.format(rel=rel, missing=companion))
    return CaptureSelection(
        enabled_rels=enabled_rels,
        enabled_node_labels=_node_labels_for(enabled_rels),
    )


_ALL_RELS: frozenset[cs.RelationshipType] = frozenset(cs.RelationshipType)

ALL_ENABLED = _selection_for(_ALL_RELS)


def _resolve_token(token: str) -> frozenset[cs.RelationshipType] | None:
    try:
        return cs.CAPTURE_GROUP_RELS[cs.CaptureGroup(token.lower())]
    except ValueError:
        pass
    try:
        return frozenset({cs.RelationshipType(token.upper())})
    except ValueError:
        return None


def resolve_capture(tokens: Iterable[str]) -> CaptureSelection:
    cleaned = [t.strip() for t in tokens if t and t.strip()]
    bare = {t.lower() for t in cleaned}

    if cs.CAPTURE_TOKEN_NONE in bare:
        base_groups: frozenset[cs.CaptureGroup] = frozenset()
    elif cs.CAPTURE_TOKEN_ALL in bare:
        base_groups = frozenset(cs.CaptureGroup)
    else:
        base_groups = cs.DEFAULT_CAPTURE_GROUPS

    enabled: set[cs.RelationshipType] = set()
    for group in base_groups:
        enabled |= cs.CAPTURE_GROUP_RELS[group]

    for token in cleaned:
        if token.lower() in (cs.CAPTURE_TOKEN_ALL, cs.CAPTURE_TOKEN_NONE):
            continue
        drop = token.startswith(cs.CAPTURE_DROP_PREFIX)
        name = token.lstrip(cs.CAPTURE_DROP_PREFIX + cs.CAPTURE_ADD_PREFIX)
        rels = _resolve_token(name)
        if rels is None:
            logger.warning(ls.CAPTURE_UNKNOWN_TOKEN.format(token=token))
            continue
        if drop:
            enabled -= rels
        else:
            enabled |= rels

    return _selection_for(frozenset(enabled))


def split_spec(spec: str) -> list[str]:
    out: list[str] = []
    token = ""
    for ch in spec:
        if ch in cs.CAPTURE_TOKEN_SEPARATORS:
            if token:
                out.append(token)
                token = ""
        else:
            token += ch
    if token:
        out.append(token)
    return out


def default_capture() -> CaptureSelection:
    from .config import settings

    return resolve_capture(split_spec(settings.CGR_CAPTURE))
