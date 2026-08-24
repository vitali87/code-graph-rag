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

import re
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


# Registration artifacts on a qualified name ("@<line>", optionally
# "_<col>"), never part of the written name; stripped before any
# hierarchy comparison.
_DUP_QN_MARKER_RE = re.compile(
    re.escape(cs.DUP_QN_MARKER)
    + r"\d+(?:"
    + re.escape(cs.DUP_QN_COLUMN_MARKER)
    + r"\d+)?"
)


# C#/Java qualified names carry a parameter signature ("Run(int)") that a
# nested definition's qn does not repeat ("Run.Local"); stripped before the
# hierarchy comparison, alongside the registration markers.
_QN_SIGNATURE_RE = re.compile(r"\([^()]*\)")


def _qn_normalized(qn: str) -> str:
    return _QN_SIGNATURE_RE.sub("", _DUP_QN_MARKER_RE.sub("", qn))


def _qn_within(outer_qn: str, inner_qn: str) -> bool:
    return _qn_normalized(inner_qn).startswith(
        _qn_normalized(outer_qn) + cs.SEPARATOR_DOT
    )


def _member_nested_in(outer: DuplicateMember, inner: DuplicateMember) -> bool:
    """True when inner's definition sits textually inside outer's.

    Only STRICT containment on both boundaries proves nesting by lines
    alone. Any shared boundary is ambiguous - a one-liner at 5-5 beside a
    sibling spanning 5-9 shares a start line without nesting, and minified
    one-liners share both - so there the qualified-name hierarchy decides:
    a nested definition's qn extends its container's, a sibling's never
    does.
    """
    if not _span_contains(outer, inner):
        return False
    if (
        outer["start_line"] < inner["start_line"]
        and inner["end_line"] < outer["end_line"]
    ):
        return True
    return _qn_within(outer["qualified_name"], inner["qualified_name"])


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
        _member_nested_in(a, b) or _member_nested_in(b, a)
        for a in first
        for b in second
    )


def _drop_contained_members(
    members: list[DuplicateMember],
) -> list[DuplicateMember]:
    """Drop members nested inside another member of the same group.

    Expanding an entry's full member list can seat an enclosing function
    next to its own nested closure (the closure's fingerprint matching a
    copy elsewhere keeps the entry edge legitimately alive, and two similar
    factories can carry their identical closures as one all-nested entry).
    The nested member is always the redundant one: whenever its entry has
    more than one member, the Stage-1 exact group already reports the
    closure clone class, and its container's real partners stay in this
    group. Nesting requires proper containment or a qualified-name
    hierarchy on a shared boundary, so two distinct definitions sharing a
    span (adjacent minified one-liners) both survive.
    """
    return [
        member
        for member in members
        if not any(
            other is not member and _member_nested_in(other, member)
            for other in members
        )
    ]


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    union = len(first | second)
    return len(first & second) / union if union else 0.0


def _entry_contains_any(container: _Entry, contained: _Entry) -> bool:
    return any(
        _member_nested_in(outer, inner)
        for outer in container.members
        for inner in contained.members
    )


def _supplemental_cliques(
    clique: list[int], order: list[_Entry], kept: list[DuplicateMember]
) -> list[list[int]]:
    """Sub-cliques preserving a fully-pruned entry's non-container partners.

    Dropping nested members can erase an ENTIRE entry from a clique (two
    factories carrying their identical closures). Its relationship to a
    partner that contains none of its members (a standalone function similar
    to the closures) is covered by no other group, so each such entry is
    re-emitted with exactly those partners.
    """
    kept_ids = {id(member) for member in kept}
    pruned = [
        position
        for position in clique
        if not any(id(member) in kept_ids for member in order[position].members)
    ]
    subcliques: list[list[int]] = []
    for position in pruned:
        partners = [
            other
            for other in clique
            if other != position
            and not _entry_contains_any(order[other], order[position])
        ]
        if partners:
            subcliques.append(sorted([position, *partners]))
    return subcliques


def _edge_qualifies(left: _Entry, right: _Entry, threshold: float) -> bool:
    first, second = left.branches, right.branches
    smaller, larger = min(len(first), len(second)), max(len(first), len(second))
    # Necessary condition for Jaccard >= threshold: even a full subset
    # overlap cannot exceed smaller/larger.
    if larger == 0 or smaller / larger < threshold:
        return False
    if _jaccard(first, second) < threshold:
        return False
    return not _only_nested_members(left.members, right.members)


def _threshold_adjacency(
    order: list[_Entry], pairs: set[tuple[int, int]], threshold: float
) -> dict[int, set[int]]:
    adjacency: dict[int, set[int]] = {}
    for left, right in pairs:
        if not _edge_qualifies(order[left], order[right], threshold):
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    return adjacency


def _pruned_members(positions: list[int], order: list[_Entry]) -> list[DuplicateMember]:
    return _drop_contained_members(
        [member for position in positions for member in order[position].members]
    )


def _clique_emissions(
    clique: list[int], order: list[_Entry]
) -> list[tuple[list[int], list[DuplicateMember]]]:
    members = _pruned_members(clique, order)
    emissions = [(clique, members)]
    emissions.extend(
        (subclique, _pruned_members(subclique, order))
        for subclique in _supplemental_cliques(clique, order, members)
    )
    return emissions


def _similar_group(
    positions: list[int], members: list[DuplicateMember], order: list[_Entry]
) -> DuplicateGroup:
    similarity = min(
        _jaccard(order[left].branches, order[right].branches)
        for at, left in enumerate(positions)
        for right in positions[at + 1 :]
    )
    return DuplicateGroup(
        kind=cs.KIND_SIMILAR,
        similarity=round(similarity, 3),
        node_count=max(order[position].node_count for position in positions),
        members=_sorted_members(members),
    )


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
    pairs, pairs_truncated = _candidate_pairs(
        order, threshold, config.max_candidate_pairs
    )
    adjacency = _threshold_adjacency(order, pairs, threshold)
    cliques, cliques_truncated = _maximal_cliques(adjacency, max_groups)
    if cliques_truncated:
        logger.warning(ls.DUPLICATES_GROUPS_TRUNCATED.format(cap=max_groups))
    truncated = pairs_truncated or cliques_truncated
    groups: list[DuplicateGroup] = []
    seen_member_sets: set[frozenset[str]] = set()
    for clique in cliques:
        for group_positions, group_members in _clique_emissions(clique, order):
            if len(group_members) < 2:
                continue
            key = frozenset(m[cs.KEY_QUALIFIED_NAME] for m in group_members)
            if key in seen_member_sets:
                continue
            seen_member_sets.add(key)
            groups.append(_similar_group(group_positions, group_members, order))
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
