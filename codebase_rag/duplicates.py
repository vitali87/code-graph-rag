# Structural duplicate (clone) detection engine. Grouping and overlap
# scoring run client-side in Python, mirroring dead_code.py: the fetches stay
# linear scans, well inside memgraph's query timeout on big projects.
#
# Stage 1 (exact/renamed copies) is a group-by on the whole-skeleton
# fingerprint stamped at ingest. Stage 2 (edited copies) compares functions
# by Jaccard overlap of their statement-level branch fingerprints; candidate
# pairs come from an inverted index over those branches, so only functions
# that actually share a branch are ever compared - never O(n^2) over the
# project. The reported groups are disjoint (issue #2473): a fingerprint that
# links to another forms a `similar` cluster with it, and only a fingerprint
# linked to none is reported on its own as an `exact` group.
from __future__ import annotations

import heapq
import re
from collections.abc import Iterator
from fnmatch import fnmatch
from math import ceil
from typing import NamedTuple

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .types_defs import (
    DuplicateGroup,
    DuplicateLink,
    DuplicateMember,
    DuplicatesConfig,
    DuplicatesReport,
    GraphQueryClient,
    PropertyValue,
    ResultRow,
)
from .utils import qn_markers


class _Entry:
    __slots__ = ("branches", "fingerprint", "members", "node_count")

    def __init__(self, fingerprint: str, node_count: int) -> None:
        self.fingerprint = fingerprint
        self.node_count = node_count
        self.branches: frozenset[str] = frozenset()
        self.members: list[DuplicateMember] = []


class _Cluster(NamedTuple):
    # Positions into the entry order, and every qualifying (left, right,
    # score) entry pair that joined them: the group reports those links and
    # the range of their scores.
    positions: list[int]
    links: list[tuple[int, int, float]]


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

    order = list(_entries_from_rows(rows, config).values())
    clusters: list[_Cluster] = []
    truncated = False
    if not config.exact_only:
        clusters, truncated = _similar_clusters(order, config)
    clustered = {position for cluster in clusters for position in cluster.positions}
    groups = [_cluster_group(cluster, order) for cluster in clusters]
    groups.extend(_exact_groups(order, clustered))
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


def _member_key(member: DuplicateMember) -> tuple[str, int]:
    return member["path"], member["start_line"]


def _sorted_members(members: list[DuplicateMember]) -> list[DuplicateMember]:
    return sorted(members, key=_member_key)


def _exact_groups(order: list[_Entry], clustered: set[int]) -> list[DuplicateGroup]:
    # A clustered entry's copies are reported inside its cluster; repeating
    # them here put one function in several groups (issue #2473).
    return [
        DuplicateGroup(
            kind=cs.KIND_EXACT,
            similarity=1.0,
            max_similarity=1.0,
            node_count=entry.node_count,
            members=_sorted_members(entry.members),
            exact_subgroups=[],
            links=[],
        )
        for position, entry in enumerate(order)
        if len(entry.members) > 1 and position not in clustered
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


# C#/Java qualified names carry a parameter signature ("Run(int)") that a
# nested definition's qn does not repeat ("Run.Local"); stripped before the
# hierarchy comparison, alongside the registration markers.
_QN_SIGNATURE_RE = re.compile(r"\([^()]*\)")


def _qn_normalized(qn: str) -> str:
    # Registration markers are stripped from every segment, not just the
    # end: the comparison is over whole hierarchies.
    return _QN_SIGNATURE_RE.sub("", qn_markers.strip_all_markers(qn))


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


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    union = len(first | second)
    return len(first & second) / union if union else 0.0


def _link_score(left: _Entry, right: _Entry, threshold: float) -> float | None:
    """The pair's similarity when it qualifies as a link, else None."""
    first, second = left.branches, right.branches
    smaller, larger = min(len(first), len(second)), max(len(first), len(second))
    # Necessary condition for Jaccard >= threshold: even a full subset
    # overlap cannot exceed smaller/larger.
    if larger == 0 or smaller / larger < threshold:
        return None
    score = _jaccard(first, second)
    if score < threshold or _only_nested_members(left.members, right.members):
        return None
    return score


def _qualifying_links(
    order: list[_Entry], pairs: set[tuple[int, int]], threshold: float
) -> list[tuple[int, int, float]]:
    links: list[tuple[int, int, float]] = []
    for left, right in pairs:
        score = _link_score(order[left], order[right], threshold)
        if score is not None:
            links.append((left, right, score))
    return links


def _link_components(links: list[tuple[int, int, float]]) -> list[_Cluster]:
    # Union-find over the qualifying links: one cluster per connected
    # component, so no qualifying pair is ever split across groups.
    parent: dict[int, int] = {}

    def root(position: int) -> int:
        parent.setdefault(position, position)
        while parent[position] != position:
            parent[position] = parent[parent[position]]
            position = parent[position]
        return position

    for left, right, _ in links:
        left_root, right_root = root(left), root(right)
        if left_root != right_root:
            parent[left_root] = right_root
    clusters: dict[int, _Cluster] = {}
    for link in links:
        clusters.setdefault(root(link[0]), _Cluster([], [])).links.append(link)
    for position in sorted(parent):
        clusters[root(position)].positions.append(position)
    return list(clusters.values())


def _cluster_rank(
    cluster: _Cluster, order: list[_Entry]
) -> tuple[int, int, tuple[str, int]]:
    entries = [order[position] for position in cluster.positions]
    first = min(_member_key(member) for entry in entries for member in entry.members)
    return (
        -sum(len(entry.members) for entry in entries),
        -max(entry.node_count for entry in entries),
        first,
    )


def _similar_clusters(
    order: list[_Entry], config: DuplicatesConfig
) -> tuple[list[_Cluster], bool]:
    """Disjoint near-duplicate clusters, largest first, and a truncation flag.

    Exact copies share one entry (entries are keyed by whole fingerprint),
    so a cluster's vertices are distinct fingerprints and its links are the
    pairs clearing the threshold. The groups are the connected components of
    that graph (issue #2473). Maximal cliques kept every two members above
    the threshold but seated one function in up to seven overlapping groups
    on gson, so the group count overstated the duplication; a component
    reports each function once and still drops no qualifying pair. The price
    is that two members of one cluster may be linked only through a third,
    which the group's similarity range (weakest to strongest link) shows.
    A definition and its own closure still never link (issue #1398); they
    share a cluster only through an external copy of the closure, which the
    group then lists among its exact copies.
    """
    threshold, cap = config.threshold, config.max_similar_groups
    pairs, pairs_truncated = _candidate_pairs(
        order, threshold, config.max_candidate_pairs
    )
    clusters = _link_components(_qualifying_links(order, pairs, threshold))
    clusters.sort(key=lambda cluster: _cluster_rank(cluster, order))
    capped = len(clusters) > cap
    if capped:
        logger.warning(ls.DUPLICATES_GROUPS_TRUNCATED.format(cap=cap))
    return clusters[:cap], pairs_truncated or capped


def _cross_pairs(
    left: list[DuplicateMember], right: list[DuplicateMember]
) -> Iterator[tuple[DuplicateMember, DuplicateMember]]:
    """Path-ordered duplicate pairs across two linked fingerprints.

    Every cross pair shares the fingerprints' score, but one that nests a
    definition in the other is no duplicate (issue #1398), even when a
    sibling pair keeps the fingerprint link. A generator, because two large
    clone classes have a cross product far bigger than either class.
    """
    for a in left:
        for b in right:
            if not (_member_nested_in(a, b) or _member_nested_in(b, a)):
                first, second = sorted((a, b), key=_member_key)
                yield first, second


def _pair_rank(
    pair: tuple[float, DuplicateMember, DuplicateMember],
) -> tuple[float, tuple[str, int], tuple[str, int]]:
    return -pair[0], _member_key(pair[1]), _member_key(pair[2])


def _best_cross_pair(
    left: list[DuplicateMember], right: list[DuplicateMember]
) -> tuple[DuplicateMember, DuplicateMember]:
    """The path-first duplicate pair across two linked fingerprints.

    The path-first member of either side leads it whenever that member has
    a partner it does not nest with, which is the norm, so the cross product
    is walked only when nesting rules all of that member's partners out.
    """
    ordered_left, ordered_right = _sorted_members(left), _sorted_members(right)
    if _member_key(ordered_right[0]) < _member_key(ordered_left[0]):
        ordered_left, ordered_right = ordered_right, ordered_left
    lead = ordered_left[0]
    for partner in ordered_right:
        if not (_member_nested_in(lead, partner) or _member_nested_in(partner, lead)):
            return lead, partner
    return min(
        _cross_pairs(left, right),
        key=lambda pair: (_member_key(pair[0]), _member_key(pair[1])),
    )


def _cluster_group(cluster: _Cluster, order: list[_Entry]) -> DuplicateGroup:
    entries = [order[position] for position in cluster.positions]
    copies = sorted(
        (_sorted_members(entry.members) for entry in entries if len(entry.members) > 1),
        key=lambda members: _member_key(members[0]),
    )
    scores = [score for _, _, score in cluster.links]
    # Two exact copies inside the cluster are its strongest possible link.
    strongest = 1.0 if copies else max(scores)
    # One link per fingerprint pair, named by its best member pair: two
    # clone classes of N copies are one link here, not N * N member pairs
    # (expanded_links generates those for the reports that print them).
    links = sorted(
        (
            (score, *_best_cross_pair(order[left].members, order[right].members))
            for left, right, score in cluster.links
        ),
        key=_pair_rank,
    )
    # The table link and --open preview a group's first two members; in a
    # cluster those two may not be similar to each other at all, so the
    # strongest qualifying pair leads and the rest follow in path order.
    _, lead, partner = links[0]
    rest = [
        member
        for member in _sorted_members([m for entry in entries for m in entry.members])
        if member is not lead and member is not partner
    ]
    return DuplicateGroup(
        kind=cs.KIND_SIMILAR,
        similarity=round(min(scores), 3),
        max_similarity=round(strongest, 3),
        node_count=max(entry.node_count for entry in entries),
        members=[lead, partner, *rest],
        exact_subgroups=[
            [member["qualified_name"] for member in members] for members in copies
        ],
        links=[
            DuplicateLink(
                first=first["qualified_name"],
                second=second["qualified_name"],
                similarity=round(score, 3),
            )
            for score, first, second in links
        ],
    )


def expanded_links(
    group: DuplicateGroup, limit: int | None = None
) -> tuple[list[DuplicateLink], bool]:
    """A group's member-level duplicate pairs, strongest first.

    Each fingerprint link stands for every pair across the exact copies of
    its two members. Returns at most `limit` pairs (all when None) and
    whether more existed; only the strongest are ever held, so a pair of
    large clone classes costs memory in the limit, not in its cross product.
    An `exact` group has no links: every pair of its members is a duplicate.
    """
    if group["kind"] != cs.KIND_SIMILAR:
        return [], False
    by_name = {member["qualified_name"]: member for member in group["members"]}
    copies = {name: names for names in group["exact_subgroups"] for name in names}

    def side(name: str) -> list[DuplicateMember]:
        return [by_name[copy] for copy in copies.get(name, [name])]

    pairs = (
        (link["similarity"], first, second)
        for link in group["links"]
        for first, second in _cross_pairs(side(link["first"]), side(link["second"]))
    )
    if limit is None:
        kept, truncated = sorted(pairs, key=_pair_rank), False
    else:
        kept = heapq.nsmallest(limit + 1, pairs, key=_pair_rank)
        truncated = len(kept) > limit
        kept = kept[:limit]
    return [
        DuplicateLink(
            first=first["qualified_name"],
            second=second["qualified_name"],
            similarity=score,
        )
        for score, first, second in kept
    ], truncated


def reported_groups(
    groups: list[DuplicateGroup], limit: int
) -> tuple[list[DuplicateGroup], bool]:
    """Groups as a JSON report writes them, and whether a link list was cut.

    The collector keeps fingerprint links; a report that prints links lists
    member pairs, at most `limit` per group. Other groups pass through as
    they are.
    """
    reported: list[DuplicateGroup] = []
    truncated = False
    for group in groups:
        if group["kind"] != cs.KIND_SIMILAR:
            reported.append(group)
            continue
        links, cut = expanded_links(group, limit)
        expanded = group.copy()
        expanded["links"] = links
        reported.append(expanded)
        truncated = truncated or cut
    if truncated:
        logger.warning(ls.DUPLICATES_LINKS_TRUNCATED.format(cap=limit))
    return reported, truncated
