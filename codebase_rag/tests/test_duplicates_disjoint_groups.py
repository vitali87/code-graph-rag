"""Issue #2473: `cgr duplicates` groups overlap.

Similar groups were the maximal cliques of the threshold graph, and an exact
group's members were expanded into every clique their fingerprint joined, so
one function was reported in up to seven groups and the group count (which
drives --fail-on-found) overstated the duplication. Groups are now the
connected components of that graph: disjoint, with exact copies nested in the
cluster that contains them and the cluster's similarity reported as a range.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.config import settings
from codebase_rag.duplicates import (
    collect_duplicates,
    collect_duplicates_with_coverage,
    default_duplicates_config,
)
from codebase_rag.tools.duplicate_detection import create_find_duplicates_tool
from codebase_rag.types_defs import DuplicateGroup, PropertyValue, ResultRow
from codebase_rag.utils.terminal_console import terminal_aware_console
from evals.duplicates import duplicate_pairs, score_duplicates

_CONFIG = default_duplicates_config()
_SHARED = [f"s{i}" for i in range(9)]


class FakeIngestor:
    def __init__(self, rows: list[ResultRow], root: str | None = None) -> None:
        self._rows = rows
        self._root = root

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return self._rows
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: "proj", cs.KEY_ROOT_PATH: self._root}]
        return [{cs.KEY_SKIPPED: 0}]


def _row(qn: str, fingerprint: str, branches: list[str], line: int = 1) -> ResultRow:
    module = qn.rsplit(".", 1)[0]
    return {
        "label": cs.NodeLabel.METHOD.value,
        "qualified_name": qn,
        "name": qn.rsplit(".", 1)[-1],
        "path": f"{module.replace('.', '/')}.java",
        "start_line": line,
        "start_col": 0,
        "end_line": line + 9,
        "ast_fingerprint": fingerprint,
        "ast_fingerprint_nodes": 20,
        "ast_branch_fingerprints": branches,
    }


def _gson_shape() -> list[ResultRow]:
    # The JsonWriterTest shape from the issue: testEmptyArray and
    # testEmptyObject are exact copies, and similar tests link to them in
    # different combinations. Every link scores 9/11; nulls-vs-deep and
    # empty-vs-deep share only 8 of 12 branches and do not qualify, so main
    # reported {empty pair, nulls, close} AND {close, deep} AND the exact
    # pair on its own: five functions in eight member rows.
    return [
        _row("proj.w.T.testEmptyArray", "e", [*_SHARED, "x"], line=10),
        _row("proj.w.T.testEmptyObject", "e", [*_SHARED, "x"], line=20),
        _row("proj.w.T.testNulls", "n", [*_SHARED, "y"], line=30),
        _row("proj.w.T.testClose", "c", [*_SHARED, "z"], line=40),
        _row("proj.w.T.testDeep", "d", [*_SHARED[1:], "z", "w"], line=50),
    ]


def _member_counts(groups: list[DuplicateGroup]) -> Counter[str]:
    return Counter(
        member["qualified_name"] for group in groups for member in group["members"]
    )


class TestDisjointGroups:
    def test_each_function_is_reported_in_one_group(self) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        counts = _member_counts(groups)
        assert len(counts) == 5
        assert {qn: n for qn, n in counts.items() if n > 1} == {}

    def test_exact_copies_nest_inside_the_cluster_that_contains_them(self) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        assert [group["kind"] for group in groups] == [cs.KIND_SIMILAR]
        assert groups[0]["exact_subgroups"] == [
            ["proj.w.T.testEmptyArray", "proj.w.T.testEmptyObject"]
        ]

    def test_chained_pairs_form_one_cluster(self) -> None:
        # A-B = 4/6 and B-C = 4/8 qualify at 0.5; A-C = 2/8 does not. Main
        # reported {A, B} and {B, C}, seating B twice.
        config = default_duplicates_config(threshold=0.5)
        rows = [
            _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
            _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
            _row("proj.c.three", "cccc", ["b3", "b4", "b5", "b6", "b7", "b8"]),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", config)

        assert len(groups) == 1
        assert set(_member_counts(groups)) == {
            "proj.a.one",
            "proj.b.two",
            "proj.c.three",
        }

    def test_similarity_is_the_range_of_the_cluster_links(self) -> None:
        # A-B = 4/6 and A-C = 4/7 link the cluster; B-C = 4/9 is below the
        # threshold, so it is not one of the cluster's links.
        config = default_duplicates_config(threshold=0.5)
        rows = [
            _row("proj.a.one", "aaaa", ["b1", "b2", "b3", "b4"]),
            _row("proj.b.two", "bbbb", ["b1", "b2", "b3", "b4", "b5", "b6"]),
            _row("proj.c.three", "cccc", ["b1", "b2", "b3", "b4", "b7", "b8", "b9"]),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", config)

        assert len(groups) == 1
        assert groups[0]["similarity"] == round(4 / 7, 3)
        assert groups[0]["max_similarity"] == round(4 / 6, 3)

    def test_exact_group_inside_a_full_overlap_cluster_is_not_repeated(
        self,
    ) -> None:
        # The constructors case: three exact copies plus two bodies with the
        # same statements in another shape (similarity 1.0, distinct
        # skeletons). Main printed the exact trio, then all five again as a
        # `similar` group at 100%.
        branches = [f"k{i}" for i in range(5)]
        rows = [
            _row("proj.a.A.ctor", "same", branches),
            _row("proj.b.B.ctor", "same", branches),
            _row("proj.c.C.ctor", "same", branches),
            _row("proj.d.D.ctor", "swap1", branches),
            _row("proj.e.E.ctor", "swap2", branches),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        assert len(groups) == 1
        group = groups[0]
        assert group["kind"] == cs.KIND_SIMILAR
        assert (group["similarity"], group["max_similarity"]) == (1.0, 1.0)
        assert group["exact_subgroups"] == [
            ["proj.a.A.ctor", "proj.b.B.ctor", "proj.c.C.ctor"]
        ]


class TestNeighbouringBehaviourUnchanged:
    def test_unrelated_clusters_stay_separate(self) -> None:
        rows = [
            _row("proj.a.one", "aaaa", [*_SHARED, "x1"]),
            _row("proj.b.two", "bbbb", [*_SHARED, "x2"]),
            _row("proj.c.three", "cccc", [f"t{i}" for i in range(9)] + ["y1"]),
            _row("proj.d.four", "dddd", [f"t{i}" for i in range(9)] + ["y2"]),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        assert sorted(sorted(_member_counts([g])) for g in groups) == [
            ["proj.a.one", "proj.b.two"],
            ["proj.c.three", "proj.d.four"],
        ]

    def test_standalone_exact_group_keeps_its_shape(self) -> None:
        rows = [
            _row("proj.a.total", "f3a9", ["b1", "b2"]),
            _row("proj.b.sum_w", "f3a9", ["b1", "b2"]),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        assert len(groups) == 1
        group = groups[0]
        assert group["kind"] == cs.KIND_EXACT
        assert (group["similarity"], group["max_similarity"]) == (1.0, 1.0)
        assert group["exact_subgroups"] == []

    def test_single_link_cluster_has_one_score(self) -> None:
        rows = [
            _row("proj.a.orig", "aaaa", [*_SHARED, "x1"]),
            _row("proj.b.edit", "bbbb", [*_SHARED, "x2"]),
        ]

        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        assert len(groups) == 1
        assert groups[0]["similarity"] == groups[0]["max_similarity"] == 0.818
        assert groups[0]["exact_subgroups"] == []

    def test_cluster_cap_still_flags_truncation(self) -> None:
        # Two disjoint clusters: a cap of one keeps the larger and says the
        # report is partial; a cap of two is a complete scan.
        rows = [
            _row("proj.a.one", "aaaa", [*_SHARED, "x1"]),
            _row("proj.b.two", "bbbb", [*_SHARED, "x2"]),
            _row("proj.e.five", "eeee", [*_SHARED, "x3"]),
            _row("proj.c.three", "cccc", [f"t{i}" for i in range(9)] + ["y1"]),
            _row("proj.d.four", "dddd", [f"t{i}" for i in range(9)] + ["y2"]),
        ]

        capped = collect_duplicates_with_coverage(
            FakeIngestor(rows),
            "proj",
            default_duplicates_config(max_similar_groups=1),
        )
        complete = collect_duplicates_with_coverage(
            FakeIngestor(rows),
            "proj",
            default_duplicates_config(max_similar_groups=2),
        )

        assert capped.truncated is True
        assert [len(group["members"]) for group in capped.groups] == [3]
        assert complete.truncated is False
        assert len(complete.groups) == 2


def _cli_ingestor(rows: list[ResultRow], root: str | None = None) -> MagicMock:
    mock = MagicMock()
    mock.list_projects.return_value = ["proj"]
    mock.fetch_all.side_effect = FakeIngestor(rows, root).fetch_all
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _render_table(monkeypatch: pytest.MonkeyPatch, groups: list[DuplicateGroup]) -> str:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)
    console.width = 200
    # Without a VT console (Windows CI) rich draws the header with the body's
    # border, so a header row would parse as a member row.
    console.legacy_windows = False
    monkeypatch.setattr(cli.app_context, "console", console)
    cli._emit_duplicates(
        groups, cs.DuplicatesFormat.TABLE, None, "proj", analyzed_symbols=5
    )
    return buffer.getvalue()


class TestReport:
    def test_json_report_names_each_function_once(self) -> None:
        with patch(
            "codebase_rag.cli.connect_memgraph",
            return_value=_cli_ingestor(_gson_shape()),
        ):
            result = CliRunner().invoke(cli.app, ["duplicates", "--format", "json"])

        assert result.exit_code == 0
        groups = json.loads(result.output)[cs.KEY_DUPLICATE_GROUPS]
        assert max(_member_counts(groups).values()) == 1

    def test_summary_counts_distinct_functions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        out = _render_table(monkeypatch, groups)

        assert "covering 5 function(s)" in out

    def test_table_shows_the_range_and_numbers_the_exact_copies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        out = _render_table(monkeypatch, groups)

        assert "82-100%" in out
        assert cs.CLI_DUPLICATES_COL_EXACT in out
        rows = {
            line.split("│")[4].strip(): line.split("│")[6].strip()
            for line in out.splitlines()
            if line.startswith("│")
        }
        assert rows["w.T.testEmptyArray"] == rows["w.T.testEmptyObject"] == "1"
        assert rows["w.T.testNulls"] == rows["w.T.testDeep"] == ""

    def test_table_without_exact_copies_keeps_its_columns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            _row("proj.a.orig", "aaaa", [*_SHARED, "x1"]),
            _row("proj.b.edit", "bbbb", [*_SHARED, "x2"]),
        ]
        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        out = _render_table(monkeypatch, groups)

        assert cs.CLI_DUPLICATES_COL_EXACT not in out
        assert "82%" in out
        assert "82-82%" not in out

    def test_agent_tool_reports_the_range_and_the_exact_copies(self) -> None:
        tool = create_find_duplicates_tool(FakeIngestor(_gson_shape()))

        response = asyncio.run(tool.function(project="proj"))

        assert "(82%-100% similar)" in response
        assert (
            cs.MSG_DUPLICATES_EXACT_SUBGROUP.format(
                names="proj.w.T.testEmptyArray, proj.w.T.testEmptyObject"
            )
            in response
        )
        assert response.count("proj.w.T.testNulls") == 1


# Review of the cluster change: A-C = 4/6 and B-C = 4/8 qualify at 0.5, A-B =
# 2/8 does not, and the path order puts A and B first. Anything that reads a
# cluster's members as pairs must see the two links only.
_CHAIN_CONFIG = default_duplicates_config(threshold=0.5)
_A, _B, _C = "proj.a.one", "proj.b.two", "proj.c.three"


def _chain_rows() -> list[ResultRow]:
    return [
        _row(_A, "aaaa", ["b1", "b2", "b3", "b4"]),
        _row(_B, "bbbb", ["b3", "b4", "b5", "b6", "b7", "b8"]),
        _row(_C, "cccc", ["b1", "b2", "b3", "b4", "b5", "b6"]),
    ]


class TestQualifyingPairs:
    @pytest.fixture(autouse=True)
    def _neutral_editor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(cs.ENV_CF_BUNDLE_ID, raising=False)
        monkeypatch.delenv(cs.ENV_TERM_PROGRAM, raising=False)
        monkeypatch.setattr(settings, "CGR_EDITOR", cs.EDITOR_AUTO)
        monkeypatch.setattr(settings, "CGR_EDITOR_URL_TEMPLATE", None)
        monkeypatch.setattr(settings, "CGR_DIFF_COMMAND", "difftool {left} {right}")

    def test_group_records_its_qualifying_links(self) -> None:
        groups = collect_duplicates(FakeIngestor(_chain_rows()), "proj", _CHAIN_CONFIG)

        assert groups[0]["links"] == [
            {"first": _A, "second": _C, "similarity": round(4 / 6, 3)},
            {"first": _B, "second": _C, "similarity": 0.5},
        ]

    def test_table_link_previews_the_strongest_link(self) -> None:
        groups = collect_duplicates(FakeIngestor(_chain_rows()), "proj", _CHAIN_CONFIG)

        cell = cli._duplicates_group_cell(1, groups[0], Path("/repo"))

        assert [span.style for span in cell.spans] == [
            "link diff://open?left=%2Frepo%2Fproj%2Fa.java%3A1"
            "&right=%2Frepo%2Fproj%2Fc.java%3A1"
        ]

    def test_open_diffs_the_strongest_link(self) -> None:
        ingestor = _cli_ingestor(_chain_rows(), root="/repo")
        with (
            patch("codebase_rag.cli.connect_memgraph", return_value=ingestor),
            patch("codebase_rag.cli.subprocess.Popen") as popen,
        ):
            result = CliRunner().invoke(
                cli.app, ["duplicates", "--threshold", "0.5", "--open", "1"]
            )

        assert result.exit_code == 0
        assert popen.call_args.args[0] == [
            "difftool",
            str(Path("/repo/proj/a.java")),
            str(Path("/repo/proj/c.java")),
        ]

    def test_evaluation_counts_only_qualifying_pairs(self) -> None:
        groups = collect_duplicates(FakeIngestor(_chain_rows()), "proj", _CHAIN_CONFIG)
        oracle = {(_A, _C), (_B, _C)}

        pairs = duplicate_pairs(groups)
        row = score_duplicates(pairs, oracle).rows[0]

        assert pairs == oracle
        assert (row["precision"], row["recall"]) == (1.0, 1.0)

    def test_exact_group_preview_is_unchanged(self) -> None:
        rows = [
            _row("proj.c.copy", "same", _SHARED),
            _row("proj.a.copy", "same", _SHARED),
            _row("proj.b.copy", "same", _SHARED),
        ]
        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        cell = cli._duplicates_group_cell(1, groups[0], Path("/repo"))

        assert groups[0]["links"] == []
        assert [span.style for span in cell.spans] == [
            "link diff://open?left=%2Frepo%2Fproj%2Fa.java%3A1"
            "&right=%2Frepo%2Fproj%2Fb.java%3A1"
        ]
        assert duplicate_pairs(groups) == {
            ("proj.a.copy", "proj.b.copy"),
            ("proj.a.copy", "proj.c.copy"),
            ("proj.b.copy", "proj.c.copy"),
        }

    def test_fully_connected_cluster_yields_all_its_pairs(self) -> None:
        rows = [
            _row("proj.a.one", "aaaa", [*_SHARED, "x1"]),
            _row("proj.b.two", "bbbb", [*_SHARED, "x2"]),
            _row("proj.c.three", "cccc", [*_SHARED, "x3"]),
        ]
        groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)

        assert len(groups[0]["links"]) == 3
        assert duplicate_pairs(groups) == {(_A, _B), (_A, _C), (_B, _C)}

    def test_exact_copies_pair_up_but_unlinked_members_do_not(self) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        pairs = duplicate_pairs(groups)

        assert ("proj.w.T.testEmptyArray", "proj.w.T.testEmptyObject") in pairs
        assert ("proj.w.T.testClose", "proj.w.T.testDeep") in pairs
        assert ("proj.w.T.testDeep", "proj.w.T.testNulls") not in pairs

    def test_a_function_never_links_to_its_own_closure(self) -> None:
        # The closure's external copy puts factory and closure in one
        # cluster, but the factory-closure pair itself is no duplicate.
        shared = [f"b{i}" for i in range(9)]
        factory = _row("proj.m.factory", "aaaa", [*shared, "outer"], line=30)
        factory["end_line"] = 60
        inner = _row("proj.m.factory.inner", "bbbb", shared, line=38)
        inner["path"] = factory["path"]
        inner["end_line"] = 58
        copy = _row("proj.o.copy", "bbbb", shared, line=5)
        groups = collect_duplicates(
            FakeIngestor([factory, inner, copy]), "proj", _CONFIG
        )

        pairs = duplicate_pairs(groups)

        assert pairs == {
            ("proj.m.factory", "proj.o.copy"),
            ("proj.m.factory.inner", "proj.o.copy"),
        }


# Review of the links field: two clone classes of N copies each that are
# similar to each other are one fingerprint link but N * N member pairs. The
# collector must keep the link, not the cross product, or a large class pair
# exhausts memory before any report is printed.
def _clone_classes(copies: int) -> list[ResultRow]:
    return [
        _row(f"proj.{side}{i}.f", f"fp_{side}", [*_SHARED, f"x_{side}"])
        for side in ("a", "b")
        for i in range(copies)
    ]


def _json_groups(
    rows: list[ResultRow], *args: str
) -> tuple[list[DuplicateGroup], bool]:
    with patch("codebase_rag.cli.connect_memgraph", return_value=_cli_ingestor(rows)):
        result = CliRunner().invoke(cli.app, ["duplicates", "--format", "json", *args])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    return payload[cs.KEY_DUPLICATE_GROUPS], payload[cs.KEY_TRUNCATED]


class TestLinkStorage:
    def test_collected_group_grows_linearly_with_copies(self) -> None:
        def stored(copies: int) -> int:
            rows = _clone_classes(copies)
            groups = collect_duplicates(FakeIngestor(rows), "proj", _CONFIG)
            return len(json.dumps(groups))

        assert stored(40) < 2.5 * stored(20)

    def test_one_link_per_linked_fingerprint_pair(self) -> None:
        groups = collect_duplicates(FakeIngestor(_clone_classes(30)), "proj", _CONFIG)

        assert len(groups[0]["links"]) == 1

    def test_json_report_caps_links_per_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cs, "DUPLICATES_MAX_GROUP_LINKS", 10)

        groups, truncated = _json_groups(_clone_classes(5))

        assert len(groups[0]["links"]) == 10
        assert truncated is True

    def test_json_report_under_the_cap_lists_every_pair(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cs, "DUPLICATES_MAX_GROUP_LINKS", 25)

        groups, truncated = _json_groups(_clone_classes(5))

        assert len(groups[0]["links"]) == 25
        assert {(link["first"], link["second"]) for link in groups[0]["links"]} == {
            (f"proj.a{i}.f", f"proj.b{j}.f") for i in range(5) for j in range(5)
        }
        assert truncated is False

    def test_json_report_lists_the_same_links_as_before(self) -> None:
        # Recorded from the eager expansion this replaces (a6dee6582).
        groups, truncated = _json_groups(_gson_shape())

        pairs = [
            (link["first"].rsplit(".", 1)[1], link["second"].rsplit(".", 1)[1])
            for link in groups[0]["links"]
        ]
        assert pairs == [
            ("testEmptyArray", "testNulls"),
            ("testEmptyArray", "testClose"),
            ("testEmptyObject", "testNulls"),
            ("testEmptyObject", "testClose"),
            ("testNulls", "testClose"),
            ("testClose", "testDeep"),
        ]
        assert {link["similarity"] for link in groups[0]["links"]} == {0.818}
        assert truncated is False

    def test_json_report_of_a_chain_is_unchanged(self) -> None:
        groups, _ = _json_groups(_chain_rows(), "--threshold", "0.5")

        assert groups[0]["links"] == [
            {"first": _A, "second": _C, "similarity": round(4 / 6, 3)},
            {"first": _B, "second": _C, "similarity": 0.5},
        ]

    def test_table_rows_are_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        groups = collect_duplicates(FakeIngestor(_gson_shape()), "proj", _CONFIG)

        out = _render_table(monkeypatch, groups)

        rows = [
            [cell.strip() for cell in line.split("│")[1:-1]]
            for line in out.splitlines()
            if line.startswith("│")
        ]
        assert rows == [
            [
                "1",
                "similar",
                "82-100%",
                "w.T.testEmptyArray",
                "proj/w/T.java:10-19",
                "1",
            ],
            ["", "", "", "w.T.testNulls", "proj/w/T.java:30-39", ""],
            ["", "", "", "w.T.testEmptyObject", "proj/w/T.java:20-29", "1"],
            ["", "", "", "w.T.testClose", "proj/w/T.java:40-49", ""],
            ["", "", "", "w.T.testDeep", "proj/w/T.java:50-59", ""],
        ]

    def test_agent_report_is_unchanged(self) -> None:
        tool = create_find_duplicates_tool(FakeIngestor(_gson_shape()))

        response = asyncio.run(tool.function(project="proj"))

        assert response.splitlines()[1:] == [
            "1. similar (82%-100% similar):",
            "   - proj.w.T.testEmptyArray  proj/w/T.java:10-19",
            "   - proj.w.T.testNulls  proj/w/T.java:30-39",
            "   - proj.w.T.testEmptyObject  proj/w/T.java:20-29",
            "   - proj.w.T.testClose  proj/w/T.java:40-49",
            "   - proj.w.T.testDeep  proj/w/T.java:50-59",
            "   exact copies: proj.w.T.testEmptyArray, proj.w.T.testEmptyObject",
        ]
