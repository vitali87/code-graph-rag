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

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
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
    once; the span key collapses them while treating the qualified name as
    opaque. The key carries start_col and the fingerprint so two distinct
    definitions sharing a start line (minified or generated one-liners) are
    never mistaken for one registration of the same definition.
    """
    entries: dict[str, _Entry] = {}
    seen_spans: set[tuple[str, int, int, str]] = set()
    for row in rows:
        fingerprint = str(row.get(cs.KEY_AST_FINGERPRINT) or "")
        node_count = int(str(row.get(cs.KEY_AST_FINGERPRINT_NODES) or 0))
        path = str(row.get(cs.KEY_PATH) or "")
        if not fingerprint or node_count < config.min_nodes:
            continue
        if any(fnmatch(path, pattern) for pattern in config.exclude_patterns):
            continue
        start_line = int(str(row.get(cs.KEY_START_LINE) or 0))
        start_col = int(str(row.get(cs.KEY_START_COL) or 0))
        span = (path, start_line, start_col, fingerprint)
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


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    union = len(first | second)
    return len(first & second) / union if union else 0.0


def _similar_groups(
    entries: dict[str, _Entry], threshold: float
) -> list[DuplicateGroup]:
    # Pairs already inside a Stage-1 exact group never reach this stage:
    # entries are keyed by whole fingerprint, so exact copies are one entry.
    #
    # A reported group must honour the PAIRWISE invariant (every two members
    # clear the threshold), and no qualifying pair may be silently dropped:
    # the groups are the maximal cliques of the threshold graph. Cliques can
    # overlap - when A duplicates both B and C but B and C are not similar,
    # A legitimately appears in {A, B} and in {A, C}.
    order = list(entries.values())
    adjacency: dict[int, set[int]] = {}
    for left, right in _candidate_pairs(order):
        first, second = order[left].branches, order[right].branches
        smaller, larger = min(len(first), len(second)), max(len(first), len(second))
        # Necessary condition for Jaccard >= threshold: even a full subset
        # overlap cannot exceed smaller/larger.
        if larger == 0 or smaller / larger < threshold:
            continue
        if _jaccard(first, second) < threshold:
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    cliques, truncated = _maximal_cliques(adjacency, cs.DUPLICATES_MAX_SIMILAR_GROUPS)
    if truncated:
        logger.warning(
            ls.DUPLICATES_GROUPS_TRUNCATED.format(cap=cs.DUPLICATES_MAX_SIMILAR_GROUPS)
        )
    groups: list[DuplicateGroup] = []
    for clique in cliques:
        similarity = min(
            _jaccard(order[left].branches, order[right].branches)
            for at, left in enumerate(clique)
            for right in clique[at + 1 :]
        )
        members = [member for position in clique for member in order[position].members]
        groups.append(
            DuplicateGroup(
                kind=cs.KIND_SIMILAR,
                similarity=round(similarity, 3),
                node_count=max(order[position].node_count for position in clique),
                members=_sorted_members(members),
            )
        )
    return groups


def _maximal_cliques(
    adjacency: dict[int, set[int]], cap: int
) -> tuple[list[list[int]], bool]:
    # Bron-Kerbosch with pivoting, deterministic via sorted iteration. The
    # threshold graph is sparse (edges need shared discriminative branches,
    # capped by DUPLICATES_HOT_FINGERPRINT_CAP) and its dense spots are
    # near-cliques, the cheap case for pivoted Bron-Kerbosch. A pathological
    # graph still has exponentially many maximal cliques (Moon-Moser), so
    # enumeration stops after `cap` cliques: with pivoting, work between two
    # emitted cliques is polynomial, making the cap a bound on total work,
    # not just on output size. Returns (cliques, truncated).
    cliques: list[list[int]] = []

    def expand(taken: set[int], candidates: set[int], excluded: set[int]) -> bool:
        if len(cliques) >= cap:
            return False
        if not candidates and not excluded:
            if len(taken) > 1:
                cliques.append(sorted(taken))
            return True
        pivot = max(
            sorted(candidates | excluded),
            key=lambda vertex: len(adjacency[vertex] & candidates),
        )
        for vertex in sorted(candidates - adjacency[pivot]):
            if not expand(
                taken | {vertex},
                candidates & adjacency[vertex],
                excluded & adjacency[vertex],
            ):
                return False
            candidates = candidates - {vertex}
            excluded = excluded | {vertex}
        return True

    completed = expand(set(), set(adjacency), set())
    return sorted(cliques), not completed
