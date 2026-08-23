# The duplicates CLI groups client-side: one linear fetch of fingerprint
# rows feeds the Python engine (Stage-1 group-by, Stage-2 branch-overlap),
# mirroring the dead-code engine's memgraph-timeout reasoning.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.duplicates import (
    _maximal_cliques,
    collect_duplicates,
    collect_duplicates_with_coverage,
    default_duplicates_config,
)
from codebase_rag.types_defs import ResultRow

_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value


class FakeIngestor:
    def __init__(self, rows: list[ResultRow], skipped: int = 0) -> None:
        self._rows = rows
        self._skipped = skipped
        self.queries: list[str] = []

    def fetch_all(
        self, query: str, params: dict[str, str] | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return self._rows
        return [{cs.KEY_SKIPPED: self._skipped}]


def _row(
    qn: str,
    fingerprint: str,
    branches: list[str],
    nodes: int = 20,
    path: str | None = None,
    start_line: int = 1,
    start_col: int = 0,
    label: str = _FUNCTION,
    end_line: int | None = None,
) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path if path is not None else f"proj/{qn.replace('.', '_')}.py",
        "start_line": start_line,
        "start_col": start_col,
        "end_line": end_line if end_line is not None else start_line + 9,
        "ast_fingerprint": fingerprint,
        "ast_fingerprint_nodes": nodes,
        "ast_branch_fingerprints": branches,
    }


_CONFIG = default_duplicates_config()


class TestExactGroups:
    def test_shared_fingerprint_forms_a_group(self) -> None:
        ingestor = FakeIngestor(
            [
                _row("proj.a.total", "f3a9", ["b1", "b2"]),
                _row("proj.b.sum_w", "f3a9", ["b1", "b2"]),
                _row("proj.c.other", "8b11", ["b9"]),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert groups[0]["kind"] == cs.KIND_EXACT
        assert groups[0]["similarity"] == 1.0
        assert {m["qualified_name"] for m in groups[0]["members"]} == {
            "proj.a.total",
            "proj.b.sum_w",
        }

    def test_functions_below_min_size_are_ignored(self) -> None:
        ingestor = FakeIngestor(
            [
                _row("proj.a.get_x", "aaaa", [], nodes=6),
                _row("proj.b.get_y", "aaaa", [], nodes=6),
            ]
        )
        assert collect_duplicates(ingestor, "proj", _CONFIG) == []

    def test_same_span_is_deduplicated(self) -> None:
        # C++ decl/def pairs and DUP_QN registry variants repeat a span.
        ingestor = FakeIngestor(
            [
                _row("proj.a.total", "f3a9", ["b1"], path="proj/a.cpp", start_line=3),
                _row("proj.a.total@1", "f3a9", ["b1"], path="proj/a.cpp", start_line=3),
                _row("proj.b.copy", "f3a9", ["b1"], path="proj/b.cpp", start_line=9),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 2

    def test_same_line_distinct_definitions_both_survive(self) -> None:
        # Two DIFFERENT definitions can share a start line (generated or
        # minified one-liners). Only true re-registrations of one definition
        # may be collapsed, so the span key carries column and fingerprint.
        ingestor = FakeIngestor(
            [
                _row("proj.r.alpha", "aaaa", ["b1"], path="proj/r.py", start_line=3),
                _row(
                    "proj.r.beta@1",
                    "eeee",
                    ["b2"],
                    path="proj/r.py",
                    start_line=3,
                    start_col=40,
                ),
                _row("proj.o.beta_copy", "eeee", ["b2"], path="proj/o.py"),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert {m["qualified_name"] for m in groups[0]["members"]} == {
            "proj.r.beta@1",
            "proj.o.beta_copy",
        }

    def test_exclude_glob_removes_members(self) -> None:
        config = default_duplicates_config(exclude_patterns=("*_generated*",))
        ingestor = FakeIngestor(
            [
                _row("proj.a.total", "f3a9", ["b1"], path="proj/a.py"),
                _row("proj.g.total", "f3a9", ["b1"], path="proj/x_generated/g.py"),
            ]
        )
        assert collect_duplicates(ingestor, "proj", config) == []


class TestSimilarGroups:
    def test_high_overlap_pair_is_reported_with_score(self) -> None:
        # 9 shared branches, one unique on each side -> jaccard 9/11 ~ 0.818.
        shared = [f"b{i}" for i in range(9)]
        ingestor = FakeIngestor(
            [
                _row("proj.a.orig", "aaaa", [*shared, "x1"]),
                _row("proj.b.edit", "bbbb", [*shared, "x2"]),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert groups[0]["kind"] == cs.KIND_SIMILAR
        assert groups[0]["similarity"] == round(9 / 11, 3)

    def test_low_overlap_pair_is_not_reported(self) -> None:
        # 4 shared of 6 union -> jaccard 0.667 < 0.8.
        ingestor = FakeIngestor(
            [
                _row("proj.a.orig", "aaaa", ["b1", "b2", "b3", "b4", "b5"]),
                _row("proj.b.edit", "bbbb", ["b1", "b2", "b3", "b4", "b6"]),
            ]
        )
        assert collect_duplicates(ingestor, "proj", _CONFIG) == []

    def test_size_ratio_prefilter_rejects_mismatched_sets(self) -> None:
        # Subset overlap, but 2 branches vs 10 can never reach 0.8.
        big = [f"b{i}" for i in range(10)]
        ingestor = FakeIngestor(
            [
                _row("proj.a.small", "aaaa", big[:2]),
                _row("proj.b.large", "bbbb", big),
            ]
        )
        assert collect_duplicates(ingestor, "proj", _CONFIG) == []

    def test_ubiquitous_shared_branch_still_yields_pairs(self) -> None:
        # A branch shared by many functions is boilerplate, but two edited
        # copies whose overlap is that branch still qualify: prefix
        # filtering must not silently drop them (the old hot-fingerprint
        # cap did exactly that).
        rows = [_row(f"proj.m{i}.fn", f"fp{i}", ["hot"], nodes=20) for i in range(60)]
        ingestor = FakeIngestor(rows)
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        # All 60 share their entire (single-element) branch set: one clique.
        assert len(groups) == 1
        assert groups[0]["kind"] == cs.KIND_SIMILAR
        assert len(groups[0]["members"]) == 60

    def test_rare_prefix_still_finds_pair_with_hot_overlap(self) -> None:
        # The qualifying pair shares nine hot branches plus rare tails; the
        # rarest shared branch lands in both prefixes, so the pair is found
        # even though every shared branch is carried by many functions.
        shared = [f"hot{i}" for i in range(9)]
        rows = [
            _row("proj.a.orig", "aaaa", [*shared, "rare_a"]),
            _row("proj.b.edit", "bbbb", [*shared, "rare_b"]),
        ]
        # 58 unrelated functions carry every hot branch (well past the old
        # cap of 50) but cannot themselves qualify: one branch vs ten fails
        # the size-ratio bound.
        rows.extend(
            _row(f"proj.n{i}.noise", f"n{i:04d}", [shared[i % len(shared)]])
            for i in range(58)
        )
        ingestor = FakeIngestor(rows)
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        pair_groups = [
            group
            for group in groups
            if {m["qualified_name"] for m in group["members"]}
            == {"proj.a.orig", "proj.b.edit"}
        ]
        assert len(pair_groups) == 1
        assert pair_groups[0]["similarity"] == round(9 / 11, 3)

    def test_candidate_pair_budget_reports_truncation(self) -> None:
        # Ten mutually-similar copies produce 45 true pairs; a budget of 3
        # must stop early and surface truncated=True, never hang or read as
        # a complete scan.
        rows = [
            _row(f"proj.m{i}.fn", f"fp{i}", ["b1", "b2", "b3"], nodes=20)
            for i in range(10)
        ]
        config = default_duplicates_config(max_candidate_pairs=3)
        report = collect_duplicates_with_coverage(FakeIngestor(rows), "proj", config)
        assert report.truncated is True

    def test_candidate_pairs_at_exact_budget_not_truncated(self) -> None:
        # Three copies produce exactly three pairs: a budget of 3 completes.
        rows = [
            _row(f"proj.m{i}.fn", f"fp{i}", ["b1", "b2", "b3"], nodes=20)
            for i in range(3)
        ]
        config = default_duplicates_config(max_candidate_pairs=3)
        report = collect_duplicates_with_coverage(FakeIngestor(rows), "proj", config)
        assert report.truncated is False
        assert len(report.groups) == 1
        assert len(report.groups[0]["members"]) == 3

    def test_union_find_merges_transitive_pairs(self) -> None:
        shared = [f"b{i}" for i in range(9)]
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", [*shared, "x1"]),
                _row("proj.b.two", "bbbb", [*shared, "x2"]),
                _row("proj.c.three", "cccc", [*shared, "x3"]),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert len(groups[0]["members"]) == 3

    def test_below_threshold_pair_is_not_grouped_transitively(self) -> None:
        # A-B = 4/6 ~ 0.667 and B-C = 4/8 = 0.5 qualify at threshold 0.5,
        # but A-C = 2/8 = 0.25 does not. Connectivity is transitive while
        # the threshold is pairwise: A and C must never share a group, yet
        # both qualifying pairs must be reported - two overlapping cliques.
        config = default_duplicates_config(threshold=0.5)
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
                _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
                _row("proj.c.three", "cccc", ["b3", "b4", "b5", "b6", "b7", "b8"]),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", config)
        member_sets = [
            {m["qualified_name"] for m in group["members"]} for group in groups
        ]
        assert {"proj.a.one", "proj.b.two"} in member_sets
        assert {"proj.b.two", "proj.c.three"} in member_sets
        assert len(groups) == 2

    def test_overlapping_qualifying_pairs_are_both_reported(self) -> None:
        # A-B = 4/6 ~ 0.667 and A-C = 4/7 ~ 0.571 qualify at threshold 0.5;
        # B-C = 4/9 ~ 0.444 does not. First-fit clustering would seat A with
        # B and silently lose the valid A-C pair; maximal cliques report
        # both groups, with A in each.
        config = default_duplicates_config(threshold=0.5)
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
                _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
                _row(
                    "proj.c.three",
                    "cccc",
                    ["b1", "b2", "b3", "b4", "b7", "b8", "b9"],
                ),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", config)
        member_sets = [
            {m["qualified_name"] for m in group["members"]} for group in groups
        ]
        assert {"proj.a.one", "proj.b.two"} in member_sets
        assert {"proj.a.one", "proj.c.three"} in member_sets
        assert len(groups) == 2
        by_members = dict(zip(map(frozenset, member_sets), groups, strict=True))
        assert by_members[frozenset({"proj.a.one", "proj.b.two"})][
            "similarity"
        ] == round(4 / 6, 3)
        assert by_members[frozenset({"proj.a.one", "proj.c.three"})][
            "similarity"
        ] == round(4 / 7, 3)

    def test_clique_enumeration_is_capped_with_truncation_signal(self) -> None:
        # Moon-Moser K(2,2,2): three partner pairs, every vertex adjacent to
        # all but its partner -> 2*2*2 = 8 maximal cliques. A pathological
        # threshold graph grows this exponentially, so enumeration must stop
        # at the cap and say so instead of materializing everything.
        partners = {0: 1, 1: 0, 2: 3, 3: 2, 4: 5, 5: 4}
        adjacency = {
            vertex: {other for other in range(6) if other not in (vertex, partner)}
            for vertex, partner in partners.items()
        }
        full, truncated = _maximal_cliques(adjacency, cap=100)
        assert len(full) == 8
        assert truncated is False

        capped, truncated = _maximal_cliques(adjacency, cap=3)
        assert len(capped) == 3
        assert truncated is True
        # Deterministic prefix: the capped result is a subset of the full one.
        assert all(clique in full for clique in capped)

        # A cap exactly equal to the clique count is a COMPLETE scan: the
        # flag must only fire when a clique beyond the cap materializes.
        exact, truncated = _maximal_cliques(adjacency, cap=8)
        assert len(exact) == 8
        assert truncated is False

    def test_enclosing_function_is_not_similar_to_its_own_closure(self) -> None:
        # A factory's body contains its nested function, so the outer branch
        # set is a superset of the inner's and Jaccard clears any threshold.
        # "This function duplicates its own body" is a false positive by
        # construction: a nested pair must never form a similar group
        # (create_query_tool vs its query_codebase_knowledge_graph closure).
        shared = [f"b{i}" for i in range(9)]
        ingestor = FakeIngestor(
            [
                _row(
                    "proj.m.factory",
                    "aaaa",
                    [*shared, "outer_extra"],
                    path="proj/m.py",
                    start_line=30,
                    end_line=60,
                ),
                _row(
                    "proj.m.factory.inner",
                    "bbbb",
                    shared,
                    path="proj/m.py",
                    start_line=38,
                    end_line=58,
                ),
            ]
        )
        assert collect_duplicates(ingestor, "proj", _CONFIG) == []

    def test_nested_pair_with_external_copy_still_groups(self) -> None:
        # The nested-pair exemption is scoped to containment: when the
        # closure's fingerprint ALSO matches a copy elsewhere, the entries
        # are a real clone pair and the group must survive.
        shared = [f"b{i}" for i in range(9)]
        ingestor = FakeIngestor(
            [
                _row(
                    "proj.m.factory",
                    "aaaa",
                    [*shared, "outer_extra"],
                    path="proj/m.py",
                    start_line=30,
                    end_line=60,
                ),
                _row(
                    "proj.m.factory.inner",
                    "bbbb",
                    shared,
                    path="proj/m.py",
                    start_line=38,
                    end_line=58,
                ),
                _row(
                    "proj.other.copy",
                    "bbbb",
                    shared,
                    path="proj/other.py",
                    start_line=5,
                ),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        member_sets = [
            {m["qualified_name"] for m in group["members"]} for group in groups
        ]
        # inner and its external copy share a fingerprint: one exact group.
        assert {"proj.m.factory.inner", "proj.other.copy"} in member_sets
        # factory vs the inner-fingerprint entry is a real cross-file clone
        # relationship, but only via the EXTERNAL copy: the similar group
        # must seat factory with proj.other.copy alone, never with its own
        # nested closure (whose exact twin is already reported above).
        assert {"proj.m.factory", "proj.other.copy"} in member_sets
        assert len(groups) == 2

    def test_exact_copies_are_not_rereported_as_similar(self) -> None:
        ingestor = FakeIngestor(
            [
                _row("proj.a.total", "f3a9", ["b1", "b2", "b3", "b4", "b5"]),
                _row("proj.b.sum_w", "f3a9", ["b1", "b2", "b3", "b4", "b5"]),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert [group["kind"] for group in groups] == [cs.KIND_EXACT]

    def test_exact_only_skips_similarity(self) -> None:
        config = default_duplicates_config(exact_only=True)
        ingestor = FakeIngestor(
            [
                _row("proj.a.orig", "aaaa", ["b1", "b2", "b3", "b4", "b5"]),
                _row("proj.b.edit", "bbbb", ["b1", "b2", "b3", "b4", "b6"]),
            ]
        )
        assert collect_duplicates(ingestor, "proj", config) == []


class TestOrderingAndCoverage:
    def test_exact_groups_sort_before_similar_and_by_size(self) -> None:
        shared = [f"b{i}" for i in range(9)]
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", [*shared, "x1"], nodes=30),
                _row("proj.b.two", "bbbb", [*shared, "x2"], nodes=30),
                _row("proj.c.pair", "eeee", ["c1"], nodes=18),
                _row("proj.d.pair", "eeee", ["c1"], nodes=18),
                _row("proj.e.trio", "ffff", ["c2"], nodes=25),
                _row("proj.f.trio", "ffff", ["c2"], nodes=25),
                _row("proj.g.trio", "ffff", ["c2"], nodes=25),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert [group["kind"] for group in groups] == [
            cs.KIND_EXACT,
            cs.KIND_EXACT,
            cs.KIND_SIMILAR,
        ]
        assert len(groups[0]["members"]) == 3

    def test_coverage_reports_skipped_symbols(self) -> None:
        ingestor = FakeIngestor([], skipped=12)
        report = collect_duplicates_with_coverage(ingestor, "proj", _CONFIG)
        assert report.groups == []
        assert report.skipped_symbols == 12
        assert report.truncated is False

    def test_truncation_flag_propagates_to_report(self) -> None:
        # A-B and A-C qualify at threshold 0.5 (two overlapping cliques);
        # with the cap at one group the second is cut and the report must
        # say so instead of passing the partial list off as complete.
        config = default_duplicates_config(threshold=0.5, max_similar_groups=1)
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
                _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
                _row(
                    "proj.c.three",
                    "cccc",
                    ["b1", "b2", "b3", "b4", "b7", "b8", "b9"],
                ),
            ]
        )
        report = collect_duplicates_with_coverage(ingestor, "proj", config)
        assert len(report.groups) == 1
        assert report.truncated is True

    def test_cap_matching_group_count_is_not_flagged_truncated(self) -> None:
        # Same two overlapping cliques with the cap at exactly two: the scan
        # is complete and must not be reported as truncated.
        config = default_duplicates_config(threshold=0.5, max_similar_groups=2)
        ingestor = FakeIngestor(
            [
                _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
                _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
                _row(
                    "proj.c.three",
                    "cccc",
                    ["b1", "b2", "b3", "b4", "b7", "b8", "b9"],
                ),
            ]
        )
        report = collect_duplicates_with_coverage(ingestor, "proj", config)
        assert len(report.groups) == 2
        assert report.truncated is False

    def test_method_and_function_share_a_group(self) -> None:
        ingestor = FakeIngestor(
            [
                _row("proj.a.total", "f3a9", ["b1"]),
                _row("proj.B.total", "f3a9", ["b1"], label=_METHOD),
            ]
        )
        groups = collect_duplicates(ingestor, "proj", _CONFIG)
        assert len(groups) == 1
        assert {m["label"] for m in groups[0]["members"]} == {_FUNCTION, _METHOD}
