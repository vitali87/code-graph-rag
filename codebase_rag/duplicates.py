# Structural duplicate (clone) detection engine. Grouping and overlap
# scoring run client-side in Python, mirroring dead_code.py: the fetches stay
# linear scans, well inside memgraph's query timeout on big projects.
#
# Stage 1 (exact/renamed copies) is a group-by on the whole-skeleton
# fingerprint stamped at ingest. Stage 2 (edited copies) compares functions
# by Jaccard overlap of their statement-level branch fingerprints; candidate
# pairs come from an inverted index over those branches, so only functions
# that actually share a branch are ever compared - never O(n^2) over the
# project.
from __future__ import annotations

from fnmatch import fnmatch

from . import constants as cs
from . import cypher_queries as cq
from .types_defs import (
    DuplicateGroup,
    DuplicateMember,
    DuplicatesConfig,
    GraphQueryClient,
    PropertyValue,
    ResultRow,
)


class _Entry:
    __slots__ = ("branches", "fingerprint", "members", "node_count")

    def __init__(self, fingerprint: str, node_count: int) -> None:
        self.fingerprint = fingerprint
        self.node_count = node_count
        self.branches: frozenset[str] = frozenset()
        self.members: list[DuplicateMember] = []


def default_duplicates_config(
    threshold: float = cs.DUPLICATES_DEFAULT_THRESHOLD,
    min_nodes: int = cs.DUPLICATES_DEFAULT_MIN_NODES,
    exact_only: bool = False,
    exclude_patterns: tuple[str, ...] = (),
) -> DuplicatesConfig:
    return DuplicatesConfig(
        threshold=threshold,
        min_nodes=min_nodes,
        exact_only=exact_only,
        exclude_patterns=exclude_patterns,
    )


def collect_duplicates(
    ingestor: GraphQueryClient, project_name: str, config: DuplicatesConfig
) -> list[DuplicateGroup]:
    return collect_duplicates_with_coverage(ingestor, project_name, config)[0]


def collect_duplicates_with_coverage(
    ingestor: GraphQueryClient, project_name: str, config: DuplicatesConfig
) -> tuple[list[DuplicateGroup], int]:
    """Duplicate groups plus the count of symbols with no fingerprint.

    The count covers ast-grep-tier languages (no tree-sitter tree at ingest)
    and bodiless declarations; like dead-code's coverage count it rides along
    so the CLI does not need a second bespoke query path.
    """
    prefix = project_name + cs.SEPARATOR_DOT
    params: dict[str, PropertyValue] = {cs.KEY_PROJECT_PREFIX: prefix}

    rows = ingestor.fetch_all(cq.CYPHER_DUPLICATE_FINGERPRINTS, params)
    skipped_rows = ingestor.fetch_all(cq.CYPHER_DUPLICATE_SKIPPED_COUNT, params)
    skipped = int(str(skipped_rows[0].get(cs.KEY_SKIPPED) or 0)) if skipped_rows else 0

    entries = _entries_from_rows(rows, config)
    groups = _exact_groups(entries)
    if not config.exact_only:
        groups.extend(_similar_groups(entries, config.threshold))
    groups.sort(
        key=lambda group: (
            group["kind"] != cs.KIND_EXACT,
            -len(group["members"]),
            -group["node_count"],
        )
    )
    return groups, skipped


def _entries_from_rows(
    rows: list[ResultRow], config: DuplicatesConfig
) -> dict[str, _Entry]:
    """One entry per distinct whole-skeleton fingerprint, members deduped.

    A C++ declaration/definition pair and the registry's DUP_QN variants
    ("@"/"_" suffixes) can put the same source span in the graph more than
    once; the (path, start_line) key collapses them while treating the
    qualified name as opaque.
    """
    entries: dict[str, _Entry] = {}
    seen_spans: set[tuple[str, int]] = set()
    for row in rows:
        fingerprint = str(row.get(cs.KEY_AST_FINGERPRINT) or "")
        node_count = int(str(row.get(cs.KEY_AST_FINGERPRINT_NODES) or 0))
        path = str(row.get(cs.KEY_PATH) or "")
        if not fingerprint or node_count < config.min_nodes:
            continue
        if any(fnmatch(path, pattern) for pattern in config.exclude_patterns):
            continue
        start_line = int(str(row.get(cs.KEY_START_LINE) or 0))
        span = (path, start_line)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        entry = entries.get(fingerprint)
        if entry is None:
            entry = _Entry(fingerprint, node_count)
            branches = row.get(cs.KEY_AST_BRANCH_FINGERPRINTS)
            if isinstance(branches, list):
                entry.branches = frozenset(str(branch) for branch in branches)
            entries[fingerprint] = entry
        entry.members.append(
            DuplicateMember(
                label=str(row.get(cs.KEY_LABEL) or ""),
                qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME) or ""),
                name=str(row.get(cs.KEY_NAME) or ""),
                path=path,
                start_line=start_line,
                end_line=int(str(row.get(cs.KEY_END_LINE) or 0)),
            )
        )
    return entries


def _sorted_members(members: list[DuplicateMember]) -> list[DuplicateMember]:
    return sorted(members, key=lambda m: (m["path"], m["start_line"]))


def _exact_groups(entries: dict[str, _Entry]) -> list[DuplicateGroup]:
    return [
        DuplicateGroup(
            kind=cs.KIND_EXACT,
            similarity=1.0,
            node_count=entry.node_count,
            members=_sorted_members(entry.members),
        )
        for entry in entries.values()
        if len(entry.members) > 1
    ]


def _candidate_pairs(order: list[_Entry]) -> set[tuple[int, int]]:
    # Inverted index branch-fingerprint -> entries carrying it. A branch
    # shared by more entries than the cap (a ubiquitous guard clause) is not
    # discriminative and generates no pairs.
    index: dict[str, list[int]] = {}
    for position, entry in enumerate(order):
        for branch in entry.branches:
            index.setdefault(branch, []).append(position)
    pairs: set[tuple[int, int]] = set()
    for postings in index.values():
        if len(postings) < 2 or len(postings) > cs.DUPLICATES_HOT_FINGERPRINT_CAP:
            continue
        for left_at, left in enumerate(postings):
            for right in postings[left_at + 1 :]:
                pairs.add((left, right))
    return pairs


def _similar_groups(
    entries: dict[str, _Entry], threshold: float
) -> list[DuplicateGroup]:
    # Pairs already inside a Stage-1 exact group never reach this stage:
    # entries are keyed by whole fingerprint, so exact copies are one entry.
    order = list(entries.values())
    parent = list(range(len(order)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    pair_similarity: dict[tuple[int, int], float] = {}
    for left, right in _candidate_pairs(order):
        first, second = order[left].branches, order[right].branches
        smaller, larger = min(len(first), len(second)), max(len(first), len(second))
        # Necessary condition for Jaccard >= threshold: even a full subset
        # overlap cannot exceed smaller/larger.
        if larger == 0 or smaller / larger < threshold:
            continue
        union = len(first | second)
        similarity = len(first & second) / union if union else 0.0
        if similarity < threshold:
            continue
        pair_similarity[(left, right)] = similarity
        parent[find(left)] = find(right)

    clusters: dict[int, list[int]] = {}
    for position in range(len(order)):
        clusters.setdefault(find(position), []).append(position)

    groups: list[DuplicateGroup] = []
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        in_cluster = set(cluster)
        similarity = min(
            value
            for (left, right), value in pair_similarity.items()
            if left in in_cluster and right in in_cluster
        )
        members = [member for position in cluster for member in order[position].members]
        groups.append(
            DuplicateGroup(
                kind=cs.KIND_SIMILAR,
                similarity=round(similarity, 3),
                node_count=max(order[position].node_count for position in cluster),
                members=_sorted_members(members),
            )
        )
    return groups
