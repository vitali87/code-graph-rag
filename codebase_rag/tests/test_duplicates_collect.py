# The duplicates CLI groups client-side: one linear fetch of fingerprint
# rows feeds the Python engine (Stage-1 group-by, Stage-2 branch-overlap),
# mirroring the dead-code engine's memgraph-timeout reasoning.
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.duplicates import (
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
    label: str = _FUNCTION,
) -> ResultRow:
    return {
        "label": label,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": path if path is not None else f"proj/{qn.replace('.', '_')}.py",
        "start_line": start_line,
        "end_line": start_line + 9,
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

    def test_hot_branch_generates_no_pairs(self) -> None:
        # One branch shared by more functions than the cap is not
        # discriminative; without a second shared branch, no candidates.
        rows = [
            _row(f"proj.m{i}.fn", f"fp{i}", ["hot"], nodes=20)
            for i in range(cs.DUPLICATES_HOT_FINGERPRINT_CAP + 1)
        ]
        ingestor = FakeIngestor(rows)
        assert collect_duplicates(ingestor, "proj", _CONFIG) == []

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
        groups, skipped = collect_duplicates_with_coverage(ingestor, "proj", _CONFIG)
        assert groups == []
        assert skipped == 12

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
