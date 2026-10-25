from pathlib import Path

import pytest
from packaging.requirements import Requirement

from vigil import constants as vc
from vigil.bench import compare, graph_delta, sign_test_p
from vigil.decide import Findings, decide_adopt, decide_upgrade
from vigil.gate import _failed_ids, new_failures, select_tests
from vigil.impact import import_roots, module_qn
from vigil.ledger import Ledger
from vigil.measure import canonical_lines, digest
from vigil.report import render
from vigil.types_defs import (
    Comparison,
    Cone,
    GraphDelta,
    Item,
    Measurement,
    Outcome,
    SuiteRun,
)
from vigil.watch import Declared, declared, latest_release, outdated
from vigil.workspace import WorkspaceError, conflict_summary, widen


def _item(in_range: bool = True, requirement: str = "protobuf>=6.33.5") -> Item:
    return Item(
        id="pypi:protobuf:7.36.2",
        package="protobuf",
        scope=vc.Scope.RUNTIME,
        requirement=requirement,
        locked="6.33.6",
        latest="7.36.2",
        in_range=in_range,
    )


def _run(passed: int, failed: list[str]) -> SuiteRun:
    return SuiteRun(selected=3, passed=passed, failed=failed)


def _cmp(speed: vc.Speed, relative: float = -0.05, rss: float = 1.0) -> Comparison:
    return Comparison(
        corpus="requests",
        baseline_ms=[3000.0] * 9,
        candidate_ms=[2850.0] * 9,
        median_delta_ms=-150.0,
        relative=relative,
        wins=9,
        losses=0,
        p_value=0.0039,
        speed=speed,
        rss_ratio=rss,
    )


def _delta() -> GraphDelta:
    return GraphDelta(
        corpus="express",
        baseline_digest="aaaa",
        candidate_digest="bbbb",
        nodes_added=0,
        nodes_removed=0,
        rels_added=2,
        rels_removed=1,
        examples=["+ edge"],
    )


def _m(ms: float, rss: float = 100.0) -> Measurement:
    return Measurement(
        corpus="requests", build_ms=ms, peak_rss_mib=rss, nodes=1, rels=1, digest="d"
    )


class TestDeclared:
    def test_a_package_in_several_places_keeps_the_widest_scope(self) -> None:
        pyproject = {
            "project": {
                "dependencies": ["click>=8.3.3"],
                "optional-dependencies": {
                    "test": ["jedi>=0.19", "click>=8"],
                    "python-semantics": ["jedi>=0.19"],
                },
            },
            "dependency-groups": {"dev": ["ruff>=0.5.5", {"include-group": "test"}]},
        }
        by_name = {d.package: d for d in declared(pyproject)}
        assert by_name["click"].scope == vc.Scope.RUNTIME
        assert by_name["jedi"].scope == vc.Scope.EXTRA
        assert by_name["ruff"].scope == vc.Scope.GROUP
        assert len(by_name) == 3


class TestLatestRelease:
    def test_the_newest_final_release_wins_not_the_last_uploaded(self) -> None:
        payload = {
            "releases": {
                "7.0.0": [{"upload_time_iso_8601": "2026-09-01T00:00:00Z"}],
                "6.33.9": [{"upload_time_iso_8601": "2026-10-20T00:00:00Z"}],
                "8.0.0rc1": [{"upload_time_iso_8601": "2026-10-21T00:00:00Z"}],
                "7.1.0": [
                    {"yanked": True, "upload_time_iso_8601": "2026-10-22T00:00:00Z"}
                ],
            },
            "info": {"version": "6.33.9", "project_urls": {"Source": "https://x"}},
        }
        release = latest_release(payload)
        assert release is not None
        assert release.version == "7.0.0"
        assert release.released == "2026-09-01T00:00:00Z"
        assert release.links == {"Source": "https://x"}

    def test_no_final_release_means_nothing_to_try(self) -> None:
        assert latest_release({"releases": {"1.0a1": [{}]}}) is None


class TestOutdated:
    def _decl(self, written: str) -> Declared:
        req = Requirement(written)
        return Declared(req.name, vc.Scope.RUNTIME, None, req, written)

    def test_in_range_says_whether_fresh_installs_already_get_the_release(self) -> None:
        decls = [self._decl("mcp>=1.30.0,<2"), self._decl("protobuf>=6.33.5")]
        releases = {
            "mcp": latest_release({"releases": {"2.3.0": [{}]}}),
            "protobuf": latest_release({"releases": {"7.36.2": [{}]}}),
        }
        items = outdated(
            decls,
            {"mcp": "1.30.0", "protobuf": "6.33.6"},
            {k: v for k, v in releases.items() if v is not None},
        )
        by_name = {i.package: i for i in items}
        assert by_name["mcp"].in_range is False
        assert by_name["protobuf"].in_range is True
        assert by_name["protobuf"].id == "pypi:protobuf:7.36.2"

    def test_a_package_already_at_the_newest_release_is_not_an_item(self) -> None:
        release = latest_release({"releases": {"8.5.0": [{}]}})
        assert release is not None
        items = outdated(
            [self._decl("click>=8.3.3")], {"click": "8.5.0"}, {"click": release}
        )
        assert items == []


class TestWiden:
    @pytest.mark.parametrize(
        ("written", "latest", "expected"),
        [
            ("mcp>=1.30.0,<2", "2.3.0", "mcp>=1.30.0,<3"),
            ("tree-sitter==0.26.0", "0.26.1", "tree-sitter==0.26.1"),
            (
                "foo[a,b]>=1,!=2.0.0;python_version>'3.11'",
                "2.0.0",
                "foo[a,b]>=1;python_version>'3.11'",
            ),
            ("bar~=1.4", "2.1.0", "bar>=2.1.0"),
        ],
    )
    def test_only_the_clause_that_excludes_the_release_changes(
        self, written: str, latest: str, expected: str
    ) -> None:
        assert widen(written, latest) == expected

    def test_a_lower_bound_above_the_release_is_refused(self) -> None:
        with pytest.raises(WorkspaceError):
            widen("foo>3", "2.0.0")


class TestSignTest:
    def test_eight_of_nine_clears_five_percent_and_six_does_not(self) -> None:
        assert sign_test_p(8, 1) == pytest.approx(0.0390625)
        assert sign_test_p(6, 3) > 0.05
        assert sign_test_p(0, 0) == 1.0


class TestCompare:
    def test_a_consistent_change_beyond_the_floor_is_a_verdict(self) -> None:
        c = compare("requests", [_m(3000)] * 9, [_m(2800)] * 9)
        assert c.speed == vc.Speed.FASTER
        assert c.wins == 9

    def test_a_consistent_change_inside_the_floor_is_noise(self) -> None:
        c = compare("requests", [_m(3000)] * 9, [_m(2970)] * 9)
        assert c.speed == vc.Speed.WITHIN_NOISE

    def test_a_large_but_inconsistent_change_is_noise(self) -> None:
        base = [_m(3000)] * 9
        cand = [_m(2700), _m(3300)] * 4 + [_m(2700)]
        assert compare("requests", base, cand).speed == vc.Speed.WITHIN_NOISE

    def test_peak_memory_is_compared_by_median(self) -> None:
        c = compare("requests", [_m(3000, 100)] * 9, [_m(3000, 120)] * 9)
        assert c.rss_ratio == pytest.approx(1.2)


class TestCanonicalGraph:
    def test_the_digest_ignores_order_and_scalar_typing(self) -> None:
        a = canonical_lines(
            [(("Function", "m.f"), {"line": 3, "name": "f"}), (("Module", "m"), {})],
            [("Module", "m", "DEFINES", "Function", "m.f")],
        )
        b = canonical_lines(
            [(("Module", "m"), {}), (("Function", "m.f"), {"name": "f", "line": "3"})],
            [("Module", "m", "DEFINES", "Function", "m.f")],
        )
        assert digest(a) == digest(b)

    def test_one_dropped_edge_changes_the_digest_and_is_counted(self) -> None:
        base = canonical_lines(
            [], [("a", "b", "CALLS", "c", "d"), ("a", "b", "CALLS", "c", "e")]
        )
        cand = canonical_lines([], [("a", "b", "CALLS", "c", "d")])
        assert digest(base) != digest(cand)
        delta = graph_delta("requests", base, cand)
        assert (delta.rels_added, delta.rels_removed) == (0, 1)


class TestImportRoots:
    def test_a_shared_namespace_narrows_to_the_package_below_it(self) -> None:
        files = {
            "protobuf": [
                "google/protobuf/__init__.py",
                "google/_upb/_message.abi3.so",
                "protobuf-7.dist-info/RECORD",
            ],
            "google-genai": ["google/genai/__init__.py"],
            "click": ["click/__init__.py", "click-8.dist-info/METADATA"],
            "six": ["six.py", "../../bin/six"],
        }
        assert import_roots(files, "protobuf") == ["google._upb", "google.protobuf"]
        assert import_roots(files, "click") == ["click"]
        assert import_roots(files, "six") == ["six"]
        assert import_roots(files, "missing") == []

    def test_a_package_path_maps_to_its_module(self) -> None:
        assert (
            module_qn("p.", "codebase_rag/ast_cache.py") == "p.codebase_rag.ast_cache"
        )
        assert module_qn("p.", "codec/__init__.py") == "p.codec"


class TestDecideUpgrade:
    def _green(self, item: Item) -> Findings:
        return Findings(
            item=item,
            experiment=vc.Experiment.UPGRADE,
            baseline_tests=_run(3, []),
            candidate_tests=_run(3, []),
            comparison=_cmp(vc.Speed.WITHIN_NOISE),
        )

    def test_a_break_inside_the_published_range_is_act(self) -> None:
        f = self._green(_item())._replace(new_failures=["t::a"])
        assert decide_upgrade(f).verdict == vc.Verdict.ACT

    def test_a_break_outside_the_range_only_says_the_cap_holds(self) -> None:
        f = self._green(_item(in_range=False, requirement="protobuf<7"))._replace(
            new_failures=["t::a"]
        )
        d = decide_upgrade(f)
        assert d.verdict == vc.Verdict.WATCHING
        assert "protobuf<7" in d.headline

    def test_a_changed_graph_in_range_is_act_even_when_tests_pass(self) -> None:
        f = self._green(_item())._replace(deltas=[_delta()])
        assert decide_upgrade(f).verdict == vc.Verdict.ACT

    def test_green_in_range_is_a_lock_bump(self) -> None:
        d = decide_upgrade(self._green(_item()))
        assert d.verdict == vc.Verdict.READY
        assert "lock bump" in d.headline

    def test_green_outside_the_range_proposes_the_widened_requirement(self) -> None:
        f = self._green(_item(in_range=False, requirement="protobuf<7"))._replace(
            widened="protobuf<8"
        )
        d = decide_upgrade(f)
        assert d.verdict == vc.Verdict.READY
        assert "`protobuf<8`" in d.headline

    def test_slower_in_range_is_act(self) -> None:
        f = self._green(_item())._replace(
            comparison=_cmp(vc.Speed.SLOWER, relative=0.06)
        )
        assert decide_upgrade(f).verdict == vc.Verdict.ACT

    def test_a_resolver_conflict_and_an_untestable_extra_are_watching(self) -> None:
        f = Findings(item=_item(), experiment=vc.Experiment.UPGRADE)
        assert (
            decide_upgrade(f._replace(error="no solution")).verdict
            == vc.Verdict.WATCHING
        )
        assert (
            decide_upgrade(f._replace(untestable="torch")).verdict
            == vc.Verdict.WATCHING
        )


class TestDecideAdopt:
    def _f(self, **kw: object) -> Findings:
        base = Findings(
            item=_item(),
            experiment=vc.Experiment.ADOPT,
            baseline_tests=_run(3, []),
            candidate_tests=_run(3, []),
            comparison=_cmp(vc.Speed.FASTER),
        )
        return base._replace(**kw)

    def test_only_a_measured_identical_graph_speedup_is_ready(self) -> None:
        assert decide_adopt(self._f(), "use X").verdict == vc.Verdict.READY

    @pytest.mark.parametrize(
        "change",
        [
            {"frozen": ["vigil/bench.py"]},
            {"new_failures": ["t::a"]},
            {"deltas": [_delta()]},
            {"comparison": _cmp(vc.Speed.WITHIN_NOISE)},
            {"comparison": _cmp(vc.Speed.FASTER, rss=1.2)},
            {"comparison": None},
        ],
    )
    def test_anything_short_of_that_is_discarded(
        self, change: dict[str, object]
    ) -> None:
        assert decide_adopt(self._f(**change), "use X").verdict == vc.Verdict.DISCARDED


class TestGate:
    def test_a_grammar_selects_its_language_tests(self, tmp_path: Path) -> None:
        tests = tmp_path / vc.TESTS_PREFIX
        tests.mkdir(parents=True)
        for name in (
            "test_scala_companions.py",
            "test_lua_l1.py",
            "test_csharp_generics.py",
        ):
            (tests / name).write_text("")
        empty = Cone(
            package="x",
            imports=[],
            source_files=[],
            test_files=["codebase_rag/tests/test_z.py"],
        )
        assert select_tests(tmp_path, "tree-sitter-scala", empty) == [
            "codebase_rag/tests/test_scala_companions.py",
            "codebase_rag/tests/test_z.py",
        ]
        assert select_tests(tmp_path, "tree-sitter-c-sharp", empty)[0].endswith(
            "test_csharp_generics.py"
        )
        assert select_tests(tmp_path, "click", empty) == [
            "codebase_rag/tests/test_z.py"
        ]

    def test_junit_failures_and_errors_count_skips_do_not(self, tmp_path: Path) -> None:
        junit = tmp_path / "j.xml"
        junit.write_text(
            "<testsuites><testsuite>"
            '<testcase classname="a" name="ok"/>'
            '<testcase classname="a" name="bad"><failure message="x"/></testcase>'
            '<testcase classname="b" name="boom"><error message="y"/></testcase>'
            '<testcase classname="b" name="skip"><skipped/></testcase>'
            "</testsuite></testsuites>"
        )
        assert _failed_ids(junit) == (1, ["a::bad", "b::boom"])

    def test_only_failures_the_baseline_does_not_have_are_new(self) -> None:
        assert new_failures(_run(1, ["a", "b"]), _run(1, ["b", "c"])) == ["c"]


class TestLedger:
    def _outcome(
        self, item: str, verdict: vc.Verdict, night: str = "2026-10-26"
    ) -> Outcome:
        return Outcome(
            item_id=item,
            experiment=vc.Experiment.UPGRADE,
            verdict=verdict,
            headline="h",
            evidence=["e1"],
            night=night,
            minutes=2.5,
        )

    def test_an_item_is_tried_once_and_every_attempt_is_logged(
        self, tmp_path: Path
    ) -> None:
        ledger = Ledger(tmp_path)
        ledger.record(self._outcome("pypi:a:1", vc.Verdict.READY))
        ledger.record(self._outcome("pypi:b:2", vc.Verdict.ACT))
        assert ledger.unseen(["pypi:a:1", "pypi:c:3"]) == ["pypi:c:3"]
        rows = (tmp_path / vc.STATE_DIR / vc.RESULTS_FILE).read_text().splitlines()
        assert rows[0].split("\t") == list(vc.RESULTS_HEADER)
        assert len(rows) == 3
        assert [o.item_id for o in ledger.night("2026-10-26")] == [
            "pypi:a:1",
            "pypi:b:2",
        ]
        assert "  - e1" in (tmp_path / vc.STATE_DIR / vc.JOURNAL_FILE).read_text()


class TestReport:
    def test_sections_follow_urgency_and_carry_their_evidence(self) -> None:
        outcomes = [
            Outcome(
                item_id="pypi:click:8.5.0",
                experiment=vc.Experiment.UPGRADE,
                verdict=vc.Verdict.READY,
                headline="lock bump",
                branch="vigil/click-8.5.0",
                night="2026-10-26",
            ),
            Outcome(
                item_id="pypi:protobuf:7.36.2",
                experiment=vc.Experiment.UPGRADE,
                verdict=vc.Verdict.ACT,
                headline="breaks 3 tests",
                evidence=["failing: t::a"],
                night="2026-10-26",
            ),
        ]
        text = render("2026-10-26", outcomes, "8f34219", 21)
        assert text.index("### ACT") < text.index("### READY")
        assert "- **protobuf** breaks 3 tests" in text
        assert "  - failing: t::a" in text
        assert "branch `vigil/click-8.5.0`" in text
        assert "Mon 26 Oct 2026" in text

    def test_a_quiet_night_says_so(self) -> None:
        assert "Nothing new" in render("2026-10-26", [], "8f34219", 0)


class TestLockBlocked:
    def _f(self, **kw: object) -> Findings:
        base = Findings(
            item=_item(),
            experiment=vc.Experiment.UPGRADE,
            baseline_tests=_run(3, []),
            candidate_tests=_run(3, []),
            comparison=_cmp(vc.Speed.WITHIN_NOISE),
            lock_blocked="Because semgrep depends on click<8.2",
        )
        return base._replace(**kw)

    def test_green_for_pip_users_but_unlockable_is_watching(self) -> None:
        d = decide_upgrade(self._f())
        assert d.verdict == vc.Verdict.WATCHING
        assert any("semgrep" in e for e in d.evidence)

    def test_broken_for_pip_users_is_act_whatever_the_lock_says(self) -> None:
        assert decide_upgrade(self._f(new_failures=["t::a"])).verdict == vc.Verdict.ACT

    def test_the_summary_starts_at_the_first_because(self) -> None:
        text = (
            "  × No solution found when resolving dependencies:\n"
            "  ╰─▶ Because semgrep==1.182.1 depends on click<8.2 and\n"
            "      you require click==8.5.0, we can conclude that\n"
        )
        assert conflict_summary(text) == (
            "Because semgrep==1.182.1 depends on click<8.2 and you require "
            "click==8.5.0, we can conclude that"
        )
        assert conflict_summary("x" * 500, limit=10) == "x" * 9 + "…"


class TestLedgerRerun:
    def test_a_rerun_replaces_the_nights_verdict_but_not_the_notebook(
        self, tmp_path: Path
    ) -> None:
        ledger = Ledger(tmp_path)
        for verdict in (vc.Verdict.WATCHING, vc.Verdict.READY):
            ledger.record(
                Outcome(
                    item_id="pypi:click:8.5.0",
                    experiment=vc.Experiment.UPGRADE,
                    verdict=verdict,
                    headline="h",
                    night="2026-10-26",
                )
            )
        assert [o.verdict for o in ledger.night("2026-10-26")] == [vc.Verdict.READY]
        rows = (tmp_path / vc.STATE_DIR / vc.RESULTS_FILE).read_text().splitlines()
        assert len(rows) == 3
