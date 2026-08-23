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
from math import ceil

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .types_defs import (
    DuplicateGroup,
    DuplicateMember,
    DuplicatesConfig,
    DuplicatesReport,
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
    max_similar_groups: int = cs.DUPLICATES_MAX_SIMILAR_GROUPS,
    max_candidate_pairs: int = cs.DUPLICATES_MAX_CANDIDATE_PAIRS,
) -> DuplicatesConfig:
    return DuplicatesConfig(
        threshold=threshold,
        min_nodes=min_nodes,
        exact_only=exact_only,
        exclude_patterns=exclude_patterns,
        max_similar_groups=max_similar_groups,
        max_candidate_pairs=max_candidate_pairs,
    )


def collect_duplicates(
    ingestor: GraphQueryClient, project_name: str, config: DuplicatesConfig
) -> list[DuplicateGroup]:
    return collect_duplicates_with_coverage(ingestor, project_name, config).groups


def collect_duplicates_with_coverage(
    ingestor: GraphQueryClient, project_name: str, config: DuplicatesConfig
) -> DuplicatesReport:
    """Duplicate groups plus scan-completeness metadata.

    skipped_symbols counts ast-grep-tier languages (no tree-sitter tree at
    ingest) and bodiless declarations; truncated reports whether similar-group
    enumeration stopped at the configured cap. Both ride along so the CLI
    does not need a second bespoke query path and no consumer mistakes a
    partial report for a complete scan.
    """
    prefix = project_name + cs.SEPARATOR_DOT
    params: dict[str, PropertyValue] = {cs.KEY_PROJECT_PREFIX: prefix}

    rows = ingestor.fetch_all(cq.CYPHER_DUPLICATE_FINGERPRINTS, params)
    skipped_rows = ingestor.fetch_all(cq.CYPHER_DUPLICATE_SKIPPED_COUNT, params)
    skipped = int(str(skipped_rows[0].get(cs.KEY_SKIPPED) or 0)) if skipped_rows else 0

    entries = _entries_from_rows(rows, config)
    groups = _exact_groups(entries)
    truncated = False
    if not config.exact_only:
        similar, truncated = _similar_groups(entries, config)
        groups.extend(similar)
    groups.sort(
        key=lambda group: (
            group["kind"] != cs.KIND_EXACT,
            -len(group["members"]),
            -group["node_count"],
        )
    )
    return DuplicatesReport(
        groups=groups,
        skipped_symbols=skipped,
        truncated=truncated,
        analyzed_symbols=len(rows),
    )


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
        if _row_excluded(fingerprint, node_count, path, config):
            continue
        start_line = int(str(row.get(cs.KEY_START_LINE) or 0))
        start_col = int(str(row.get(cs.KEY_START_COL) or 0))
        span = (path, start_line, start_col, fingerprint)
        if span in seen_spans:
            continue
        seen_spans.add(span)
        entry = _entry_for(entries, row, fingerprint, node_count)
        entry.members.append(_member_from_row(row, path, start_line))
    return entries


def _row_excluded(
    fingerprint: str, node_count: int, path: str, config: DuplicatesConfig
) -> bool:
    if not fingerprint or node_count < config.min_nodes:
        return True
    return any(fnmatch(path, pattern) for pattern in config.exclude_patterns)


def _entry_for(
    entries: dict[str, _Entry], row: ResultRow, fingerprint: str, node_count: int
) -> _Entry:
    entry = entries.get(fingerprint)
    if entry is None:
        entry = _Entry(fingerprint, node_count)
        branches = row.get(cs.KEY_AST_BRANCH_FINGERPRINTS)
        if isinstance(branches, list):
            entry.branches = frozenset(str(branch) for branch in branches)
        entries[fingerprint] = entry
    return entry


def _member_from_row(row: ResultRow, path: str, start_line: int) -> DuplicateMember:
    return DuplicateMember(
        label=str(row.get(cs.KEY_LABEL) or ""),
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME) or ""),
        name=str(row.get(cs.KEY_NAME) or ""),
        path=path,
        start_line=start_line,
        end_line=int(str(row.get(cs.KEY_END_LINE) or 0)),
    )


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


def _candidate_pairs(
    order: list[_Entry], threshold: float, max_pairs: int
) -> tuple[set[tuple[int, int]], bool]:
    """Exact prefix-filtered candidate generation (AllPairs/PPJoin).

    Jaccard >= threshold forces an overlap of at least ceil(threshold * size)
    branches, so the globally rarest shared branch of any qualifying pair
    must sit inside BOTH members' prefixes of length size - overlap + 1
    (pigeonhole). Indexing only those prefixes therefore loses no qualifying
    pair, while ubiquitous boilerplate branches sort to the ends of the
    canonical order and enter a prefix only for functions that are mostly
    boilerplate - exactly the case where they are needed for correctness.
    Returns (pairs, truncated): generation stops past max_pairs, and the
    overflow pair is the truncation evidence (an exactly-at-budget scan is
    complete and not flagged).
    """
    frequency: dict[str, int] = {}
    for entry in order:
        for branch in entry.branches:
            frequency[branch] = frequency.get(branch, 0) + 1
    index = _prefix_index(order, threshold, frequency)
    return _pairs_from_index(index, max_pairs)


def _prefix_index(
    order: list[_Entry], threshold: float, frequency: dict[str, int]
) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for position, entry in enumerate(order):
        size = len(entry.branches)
        if size == 0:
            continue
        required = max(1, ceil(threshold * size - cs.DUPLICATES_PREFIX_EPSILON))
        ranked = sorted(entry.branches, key=lambda branch: (frequency[branch], branch))
        for branch in ranked[: size - required + 1]:
            index.setdefault(branch, []).append(position)
    return index


def _pairs_from_index(
    index: dict[str, list[int]], max_pairs: int
) -> tuple[set[tuple[int, int]], bool]:
    pairs: set[tuple[int, int]] = set()
    for postings in index.values():
        for left_at, left in enumerate(postings):
            for right in postings[left_at + 1 :]:
                pair = (left, right)
                if len(pairs) >= max_pairs and pair not in pairs:
                    logger.warning(ls.DUPLICATES_PAIRS_TRUNCATED.format(cap=max_pairs))
                    return pairs, True
                pairs.add(pair)
    return pairs, False


def _span_contains(outer: DuplicateMember, inner: DuplicateMember) -> bool:
    return (
        outer["path"] == inner["path"]
        and outer["start_line"] <= inner["start_line"]
        and inner["end_line"] <= outer["end_line"]
    )


def _only_nested_members(
    first: list[DuplicateMember], second: list[DuplicateMember]
) -> bool:
    """True when every cross pair is one definition inside the other.

    A factory's body contains its nested function, so the outer branch set is
    a superset of the inner's and Jaccard clears any threshold - yet "this
    function duplicates its own body" is a false positive by construction.
    The exemption is scoped to pure containment: one non-nested cross pair
    (the closure's fingerprint also matching a copy elsewhere) keeps the
    entries a real clone pair.
    """
    return all(
        _span_contains(a, b) or _span_contains(b, a) for a in first for b in second
    )


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    union = len(first | second)
    return len(first & second) / union if union else 0.0


def _similar_groups(
    entries: dict[str, _Entry], config: DuplicatesConfig
) -> tuple[list[DuplicateGroup], bool]:
    # Pairs already inside a Stage-1 exact group never reach this stage:
    # entries are keyed by whole fingerprint, so exact copies are one entry.
    #
    # A reported group must honour the PAIRWISE invariant (every two members
    # clear the threshold), and no qualifying pair may be silently dropped:
    # the groups are the maximal cliques of the threshold graph. Cliques can
    # overlap - when A duplicates both B and C but B and C are not similar,
    # A legitimately appears in {A, B} and in {A, C}.
    threshold, max_groups = config.threshold, config.max_similar_groups
    order = list(entries.values())
    adjacency: dict[int, set[int]] = {}
    pairs, pairs_truncated = _candidate_pairs(
        order, threshold, config.max_candidate_pairs
    )
    for left, right in pairs:
        first, second = order[left].branches, order[right].branches
        smaller, larger = min(len(first), len(second)), max(len(first), len(second))
        # Necessary condition for Jaccard >= threshold: even a full subset
        # overlap cannot exceed smaller/larger.
        if larger == 0 or smaller / larger < threshold:
            continue
        if _jaccard(first, second) < threshold:
            continue
        if _only_nested_members(order[left].members, order[right].members):
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)

    cliques, cliques_truncated = _maximal_cliques(adjacency, max_groups)
    if cliques_truncated:
        logger.warning(ls.DUPLICATES_GROUPS_TRUNCATED.format(cap=max_groups))
    truncated = pairs_truncated or cliques_truncated
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
    return groups, truncated


def _maximal_cliques(
    adjacency: dict[int, set[int]], cap: int
) -> tuple[list[list[int]], bool]:
    # Bron-Kerbosch with pivoting, deterministic via sorted iteration. The
    # threshold graph is sparse (edges need Jaccard overlap at or above the
    # threshold across whole branch sets) and its dense spots are
    # near-cliques, the cheap case for pivoted Bron-Kerbosch. A pathological
    # graph still has exponentially many maximal cliques (Moon-Moser), so
    # enumeration stops once a clique BEYOND the cap materializes: with
    # pivoting, work between two emitted cliques is polynomial, making the
    # budget a bound on total work, not just on output size. The overflow
    # clique is the truncation evidence and is dropped from the result, so a
    # scan with exactly `cap` cliques completes and is NOT flagged truncated.
    # Returns (cliques, truncated).
    budget = cap + 1
    cliques: list[list[int]] = []

    def expand(taken: set[int], candidates: set[int], excluded: set[int]) -> bool:
        if len(cliques) >= budget:
            return False
        if not candidates and not excluded:
            if len(taken) > 1:
                cliques.append(sorted(taken))
            return len(cliques) < budget
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

    expand(set(), set(adjacency), set())
    truncated = len(cliques) > cap
    return sorted(cliques[:cap]), truncated
