from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from loguru import logger

from . import constants as cs
from . import logs as ls
from .cli_help import HELP_CAPTURE

# Relationships that are usually meaningless without a companion still
# enabled. Dropping the companion is obeyed, but warned about.
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


def _base_rels(groups: Iterable[cs.CaptureGroup]) -> set[cs.RelationshipType]:
    enabled: set[cs.RelationshipType] = set()
    for group in groups:
        enabled |= cs.CAPTURE_GROUP_RELS[group]
    return enabled


def resolve_capture(tokens: Iterable[str]) -> CaptureSelection:
    # Tokens apply left-to-right so later ones win: a `none`/`all` base
    # clears everything before it, and add-drops after a base survive. So
    # `--capture none` overrides an inherited `CGR_CAPTURE=io`.
    enabled = _base_rels(cs.DEFAULT_CAPTURE_GROUPS)
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        low = token.lower()
        if low == cs.CAPTURE_TOKEN_NONE:
            enabled = set()
            continue
        if low == cs.CAPTURE_TOKEN_ALL:
            enabled = set(_ALL_RELS)
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


# The help and the docs describe the capture model from here, in declaration
# order, instead of listing groups by hand: `--capture` once named 5 of the 10
# groups the resolver accepts (#2584).
def default_groups() -> list[cs.CaptureGroup]:
    return [group for group in cs.CaptureGroup if group in cs.DEFAULT_CAPTURE_GROUPS]


def opt_in_groups() -> list[cs.CaptureGroup]:
    return [
        group for group in cs.CaptureGroup if group not in cs.DEFAULT_CAPTURE_GROUPS
    ]


def group_relationships(group: cs.CaptureGroup) -> list[cs.RelationshipType]:
    rels = cs.CAPTURE_GROUP_RELS[group]
    return [rel for rel in cs.RelationshipType if rel in rels]


def group_node_labels(group: cs.CaptureGroup) -> list[cs.NodeLabel]:
    labels = cs.CAPTURE_GROUP_NODE_LABELS.get(group, frozenset())
    return [label for label in cs.NodeLabel if label in labels]


def group_summary(group: cs.CaptureGroup) -> str:
    return cs.CAPTURE_GROUP_SUMMARIES[group]


def relationship_group(rel: cs.RelationshipType) -> cs.CaptureGroup:
    return next(group for group, rels in cs.CAPTURE_GROUP_RELS.items() if rel in rels)


def node_label_group(label: cs.NodeLabel) -> cs.CaptureGroup | None:
    # None for a label no group owns: it is written whatever the selection.
    return next(
        (
            group
            for group, labels in cs.CAPTURE_GROUP_NODE_LABELS.items()
            if label in labels
        ),
        None,
    )


# The help's example of a relationship-type token. No group shares its name;
# `CALLS` would read as the `calls` group, which `_resolve_token` tries first.
_HELP_EXAMPLE_TYPE = cs.RelationshipType.OVERRIDES


def capture_help() -> str:
    return HELP_CAPTURE.format(
        default_groups=cs.SEPARATOR_COMMA_SPACE.join(default_groups()),
        opt_in_groups=cs.SEPARATOR_COMMA_SPACE.join(opt_in_groups()),
        add=cs.CAPTURE_ADD_PREFIX,
        drop=cs.CAPTURE_DROP_PREFIX,
        example_type=_HELP_EXAMPLE_TYPE,
        all=cs.CAPTURE_TOKEN_ALL,
        none=cs.CAPTURE_TOKEN_NONE,
    )
