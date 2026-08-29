"""C++ resolved-INHERITS grading against the libclang oracle (issue #1190).

Gap 2 of that issue is resolved-edge grading beyond Python. Inheritance was
graded for Python and Java only; the libclang oracle already emitted base
specifiers as `name_edges` in the exact shape the Java path consumes, so this
adds the C++ grading arm rather than any oracle-side work.

The oracle names a base by SIMPLE name while pinning the subclass to a file
and line, so the scored row carries its own label: a C++ 1.0 is not measuring
the same unit as the Python one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import constants as ec
from evals.inheritance import (
    CgrResult,
    OracleResult,
    _cpp_compile_db_units,
    cpp_oracle_inheritance,
    score_inheritance,
)
from evals.oracles.cpp_oracle import cpp_available, run_cpp_oracle

_NEEDS_LIBCLANG = pytest.mark.skipif(
    not cpp_available(), reason="libclang not available"
)


def _cpp_project(tmp_path: Path, source: str) -> Path:
    """A minimal compile_commands.json project.

    The source path inside it must be ABSOLUTE: `index.parse` runs from the
    process cwd rather than the compile directory, and a relative path yields
    zero nodes SILENTLY, which reads exactly like "no inheritance support".
    """
    src = tmp_path / "a.cpp"
    src.write_text(source, encoding="utf-8")
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "command": f"clang++ -std=c++17 -c {src}",
                    "file": str(src),
                }
            ]
        ),
        encoding="utf-8",
    )
    return tmp_path


@_NEEDS_LIBCLANG
class TestTheOracleReadsBaseSpecifiers:
    def test_a_single_base_is_reported_against_the_subclass_location(
        self, tmp_path: Path
    ) -> None:
        project = _cpp_project(
            tmp_path, "class Base {};\nclass Derived : public Base {};\n"
        )

        result = cpp_oracle_inheritance(project)

        assert result.inherits == {("a.cpp:2", "Base")}
        assert result.top_classes == frozenset({"a.cpp:2"})

    def test_every_base_of_a_multiply_derived_class_is_reported(
        self, tmp_path: Path
    ) -> None:
        # One subclass, two bases: the pair must not collapse to one edge.
        project = _cpp_project(
            tmp_path,
            "class A {};\nclass B {};\nclass C : public A, public B {};\n",
        )

        result = cpp_oracle_inheritance(project)

        assert result.inherits == {("a.cpp:3", "A"), ("a.cpp:3", "B")}

    def test_a_namespaced_base_is_named_by_its_last_component(
        self, tmp_path: Path
    ) -> None:
        # `ns::Base` must arrive as `Base`, mirroring cgr's normalisation;
        # without the collapse the two sides never match and every edge is
        # scored as one false positive plus one false negative.
        project = _cpp_project(
            tmp_path,
            "namespace ns { class Base {}; }\nclass Derived : public ns::Base {};\n",
        )

        result = cpp_oracle_inheritance(project)

        assert result.inherits == {("a.cpp:2", "Base")}

    def test_a_class_with_no_base_contributes_no_edge(self, tmp_path: Path) -> None:
        project = _cpp_project(tmp_path, "class Alone {};\n")

        result = cpp_oracle_inheritance(project)

        assert result.inherits == set()
        assert result.top_classes == frozenset()

    def test_the_oracle_adjudicates_no_overrides(self, tmp_path: Path) -> None:
        # A virtual override is not distinguishable from a shadowing
        # redeclaration without more analysis than the oracle does, so the
        # category stays empty and _prf omits the row rather than scoring
        # what the oracle cannot adjudicate.
        project = _cpp_project(
            tmp_path,
            "class B { public: virtual void f() {} };\n"
            "class D : public B { public: void f() {} };\n",
        )

        result = cpp_oracle_inheritance(project)

        assert result.overrides == set()
        assert result.override_scope == frozenset()


class TestTheScoreDistinguishesDisagreement:
    """A 1.0 means nothing unless the grader can report something else.

    These pin that each kind of disagreement moves the metric it should, so a
    perfect score on a real fixture is evidence of agreement rather than of a
    comparison that cannot fail.
    """

    ORACLE = OracleResult(
        inherits={("f.cpp:1", "Base"), ("f.cpp:2", "Other")},
        overrides=set(),
        top_classes=frozenset({"f.cpp:1", "f.cpp:2"}),
        override_scope=frozenset(),
    )

    def _row(self, cgr: CgrResult) -> dict[str, object]:
        return score_inheritance(
            cgr, self.ORACLE, inherits_label=ec.CPP_BASES_LABEL
        ).rows[0]

    def test_agreement_scores_one(self) -> None:
        row = self._row(CgrResult(inherits=self.ORACLE.inherits, overrides=set()))

        assert row["precision"] == 1.0
        assert row["recall"] == 1.0

    def test_a_missed_edge_lowers_recall_only(self) -> None:
        row = self._row(CgrResult(inherits={("f.cpp:1", "Base")}, overrides=set()))

        assert row["recall"] == 0.5
        assert row["precision"] == 1.0
        assert row["fn"] == 1

    def test_an_invented_edge_lowers_precision_only(self) -> None:
        row = self._row(
            CgrResult(
                inherits={*self.ORACLE.inherits, ("f.cpp:2", "Ghost")}, overrides=set()
            )
        )

        assert row["precision"] < 1.0
        assert row["recall"] == 1.0
        assert row["fp"] == 1

    def test_a_wrong_base_name_lowers_both(self) -> None:
        row = self._row(
            CgrResult(
                inherits={("f.cpp:1", "WrongName"), ("f.cpp:2", "Other")},
                overrides=set(),
            )
        )

        assert row["precision"] == 0.5
        assert row["recall"] == 0.5

    def test_the_row_carries_the_cpp_label(self) -> None:
        # The unit differs from Python's resolved-qn inheritance, so the label
        # must say so rather than letting a C++ 1.0 read as the Python one.
        row = self._row(CgrResult(inherits=self.ORACLE.inherits, overrides=set()))

        assert row["label"] == ec.CPP_BASES_LABEL
        assert row["label"] != ec.INHERITS_LABEL


@_NEEDS_LIBCLANG
def test_the_cpp_oracle_emits_only_inheritance_name_edges(tmp_path: Path) -> None:
    """Pins the assumption that makes the INHERITS filter unreachable.

    `cpp_oracle_inheritance` filters `rel_type != _INHERITS`, and removing that
    filter today changes nothing -- the oracle has one name-edge construction
    site, hard-coded to INHERITS. That makes the filter untestable by behaviour
    alone, so the assumption is pinned directly instead: if the oracle ever
    emits a second edge kind, this fails and whoever added it has to decide
    whether inheritance grading should count it.
    """
    project = _cpp_project(
        tmp_path,
        "class A {};\nclass B {};\nclass C : public A, public B {};\n"
        "class D { public: virtual void f() {} };\n"
        "class E : public D { public: void f() {} };\n",
    )

    graph = run_cpp_oracle(project)

    assert {edge.rel_type for edge in graph.name_edges} == {"INHERITS"}


@_NEEDS_LIBCLANG
def test_a_relative_path_project_is_actually_graded_not_just_admitted(
    tmp_path: Path,
) -> None:
    """The preflight and the oracle must agree about what they can read.

    Fixing `_cpp_compile_db_units` to resolve against `command.directory`
    without fixing `run_cpp_oracle` created a NEW fail-open: the preflight
    admitted a spec-valid relative-path project and the oracle then graded
    nothing, so an unanalysed target scored a clean zero (Greptile, PR #1513).

    A guard that is smarter than the code it guards is worse than no guard,
    because it converts a loud failure into a silent pass.
    """
    src = tmp_path / "a.cpp"
    src.write_text(
        "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
    )
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "command": "clang++ -std=c++17 -c a.cpp",
                    "file": "a.cpp",  # relative, per the spec
                }
            ]
        ),
        encoding="utf-8",
    )

    # Admitted by the preflight AND graded by the oracle -- asserting only the
    # first is what let this ship.
    assert _cpp_compile_db_units(tmp_path) == 1
    assert cpp_oracle_inheritance(tmp_path).inherits == {("a.cpp:2", "Base")}


@_NEEDS_LIBCLANG
class TestAnUngradableTargetIsRefusedNotScoredEmpty:
    """A target the oracle cannot read must stop the run, not score 0 vs 0.

    Raised by Greptile on PR #1513. Its example was wrong -- a MISSING
    `compile_commands.json` raises `CompilationDatabaseError` rather than
    returning empty -- but the class was real, and a non-reproducing example
    refutes the example rather than the class. Two genuine cases found by
    sweeping it: an empty database (`getAllCompileCommands()` returns None,
    giving `TypeError`) and a STALE one naming files that no longer exist,
    which has entries, parses nothing, and reports a clean empty result for a
    target that was never graded.

    Counting entries would miss the stale case, so the guard counts units that
    actually parse.
    """

    def test_a_missing_database_is_reported_as_absent_not_empty(
        self, tmp_path: Path
    ) -> None:
        # `None` rather than `0`: the caller reports "no compile_commands.json"
        # for one and "it yielded no translation units" for the other, and
        # collapsing them sends the reader to the wrong remedy (CodeRabbit,
        # PR #1513).
        (tmp_path / "a.cpp").write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )

        assert _cpp_compile_db_units(tmp_path) is None

    def test_an_empty_database_is_reported_as_empty_not_absent(
        self, tmp_path: Path
    ) -> None:
        # The counterpart: the file opened, so the remedy is "your database is
        # stale or empty", not "create one".
        (tmp_path / "a.cpp").write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )
        (tmp_path / "compile_commands.json").write_text("[]", encoding="utf-8")

        assert _cpp_compile_db_units(tmp_path) == 0

    def test_a_stale_database_naming_a_deleted_file_yields_no_units(
        self, tmp_path: Path
    ) -> None:
        # Entries exist, so an entry COUNT would report 1 and let the run
        # proceed to grade nothing. Only parsing distinguishes this.
        (tmp_path / "a.cpp").write_text("class Base {};\n", encoding="utf-8")
        gone = tmp_path / "gone.cpp"
        (tmp_path / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(tmp_path),
                        "command": f"clang++ -std=c++17 -c {gone}",
                        "file": str(gone),
                    }
                ]
            ),
            encoding="utf-8",
        )

        assert _cpp_compile_db_units(tmp_path) == 0

    def test_a_valid_but_empty_translation_unit_still_counts(
        self, tmp_path: Path
    ) -> None:
        """Emptiness is a fact about the CODE, not a failure to read it.

        The guard first counted units whose AST had children, which rejected a
        comment-only or empty source file -- a project libclang parsed
        perfectly, refused as ungradable. That turns a legitimate
        zero-inheritance grade into an error (Greptile, PR #1513).

        The distinction the guard must draw is "could the oracle read this",
        not "did it find anything", and those come apart exactly here.
        """
        project = _cpp_project(tmp_path, "// only a comment, no declarations\n")

        assert _cpp_compile_db_units(project) > 0

    def test_a_relative_source_path_is_resolved_against_its_directory(
        self, tmp_path: Path
    ) -> None:
        """Spec-valid relative paths must not read as an ungradable target.

        The Clang JSON Compilation Database spec resolves `file` and relative
        command paths against that entry's `directory`, not the reader's cwd.
        Checking them as given skipped spec-valid entries, so a perfectly
        usable database was refused (CodeRabbit, PR #1513).
        """
        (tmp_path / "a.cpp").write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )
        (tmp_path / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(tmp_path),
                        "command": "clang++ -std=c++17 -c a.cpp",
                        "file": "a.cpp",  # relative, per the spec
                    }
                ]
            ),
            encoding="utf-8",
        )

        assert _cpp_compile_db_units(tmp_path) == 1

    def test_a_source_outside_the_target_does_not_count_as_gradeable(
        self, tmp_path: Path
    ) -> None:
        """The preflight must count what the ORACLE will grade, not what parses.

        `_walk` keeps only cursors whose file resolves inside the target
        (`_rel` returns None otherwise), so a database entry pointing outside
        it contributes nothing. Counting it admitted a target the oracle then
        scored empty -- the third instance of this preflight/oracle asymmetry
        on this PR (Greptile, PR #1513).
        """
        target = tmp_path / "target"
        target.mkdir()
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        src = outside / "ext.cpp"
        src.write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )
        (target / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(outside),
                        "command": f"clang++ -std=c++17 -c {src}",
                        "file": str(src),
                    }
                ]
            ),
            encoding="utf-8",
        )

        # Parses perfectly -- but nothing it declares can reach the grade.
        assert _cpp_compile_db_units(target) == 0
        assert cpp_oracle_inheritance(target).inherits == set()

    def test_an_in_target_unit_declaring_only_outside_it_is_a_true_empty(
        self, tmp_path: Path
    ) -> None:
        """Not every empty grade is a fail-open, and this one is legitimate.

        A TU inside the target that declares everything in an out-of-target
        header is admitted by the preflight and grades empty. That LOOKS like
        the asymmetry fixed three times on this PR, but it is not: cgr indexes
        only files inside the repository too, so its side is empty as well and
        0-vs-0 is a correct grade of a project with no in-target inheritance.

        Pinned so the distinction is explicit. The fail-open cases are the ones
        where cgr HAS edges the oracle cannot see; here neither side does, and
        "fixing" this by refusing the target would refuse a gradeable project.
        """
        target = tmp_path / "target"
        target.mkdir()
        include = tmp_path / "include"
        include.mkdir()
        (include / "far.hpp").write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )
        src = target / "main.cpp"
        src.write_text('#include "far.hpp"\n', encoding="utf-8")
        (target / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(target),
                        "command": f"clang++ -std=c++17 -I{include} -c {src}",
                        "file": str(src),
                    }
                ]
            ),
            encoding="utf-8",
        )

        # Admitted, and legitimately graded empty: both sides see nothing.
        assert _cpp_compile_db_units(target) == 1
        assert cpp_oracle_inheritance(target).inherits == set()

    def test_a_missing_command_directory_is_reported_not_raised(
        self, tmp_path: Path
    ) -> None:
        """A stale build directory is an unusable entry, not a crash.

        `os.chdir(command.directory)` raises `FileNotFoundError` when the
        directory named by the database no longer exists. Letting that escape
        denies the caller its whole job -- reporting the database as
        ungradable with an actionable message (Greptile, PR #1513).

        The source itself exists and is in-target, so every earlier guard
        passes and only the chdir can fail: the fixture is degenerate for
        exactly this branch.
        """
        src = tmp_path / "a.cpp"
        src.write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )
        (tmp_path / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        # Never created.
                        "directory": str(tmp_path / "build_gone"),
                        "command": f"clang++ -std=c++17 -c {src}",
                        "file": str(src),
                    }
                ]
            ),
            encoding="utf-8",
        )

        # Negative rather than zero: the database WAS readable and one
        # in-target entry was not -- the partially-readable case. Zero is
        # reserved for "opened and yielded nothing gradeable at all". Both stop
        # the run; the distinction is what the error message names.
        assert _cpp_compile_db_units(tmp_path) == -1

    def test_a_partially_readable_database_is_refused_not_partly_graded(
        self, tmp_path: Path
    ) -> None:
        """Half a grade that reports 1.0 is worse than no grade.

        With one usable entry and one whose directory is gone, the preflight
        used to admit the target because *something* parsed. The oracle skips
        the same entry, and `score_inheritance` then filters the unread file
        out of BOTH sides via `top_classes` -- so cgr's 2 edges and the
        oracle's 1 score **precision 1.0, recall 1.0** on a half-covered
        project (Greptile, PR #1513).

        Measured before the fix:

            oracle: [('good.cpp:2', 'GoodBase')]
            cgr   : [('good.cpp:2', 'GoodBase'), ('skipped.cpp:2', 'SkipBase')]
            scored: tp=1 fp=0 fn=0 precision=1.0 recall=1.0
        """
        good = tmp_path / "good.cpp"
        good.write_text(
            "class GoodBase {};\nclass GoodDerived : public GoodBase {};\n",
            encoding="utf-8",
        )
        skipped = tmp_path / "skipped.cpp"
        skipped.write_text(
            "class SkipBase {};\nclass SkipDerived : public SkipBase {};\n",
            encoding="utf-8",
        )
        (tmp_path / "compile_commands.json").write_text(
            json.dumps(
                [
                    {
                        "directory": str(tmp_path),
                        "command": f"clang++ -std=c++17 -c {good}",
                        "file": str(good),
                    },
                    {
                        # Never created: this entry cannot be read.
                        "directory": str(tmp_path / "gone"),
                        "command": f"clang++ -std=c++17 -c {skipped}",
                        "file": str(skipped),
                    },
                ]
            ),
            encoding="utf-8",
        )

        # Negative, not 1: one entry parsed, but one in-target entry did not.
        assert _cpp_compile_db_units(tmp_path) == -1
        # The premise, so this cannot pass on a fixture where nothing parses.
        assert cpp_oracle_inheritance(tmp_path).inherits == {("good.cpp:2", "GoodBase")}

    def test_a_usable_database_yields_units(self, tmp_path: Path) -> None:
        # The control: without this the three assertions above are satisfied by
        # a helper that returns 0 unconditionally.
        project = _cpp_project(tmp_path, "class Base {};\nclass D : public Base {};\n")

        assert _cpp_compile_db_units(project) > 0


@_NEEDS_LIBCLANG
def test_a_cpp_file_outside_the_compilation_database_is_not_a_false_positive(
    tmp_path: Path,
) -> None:
    """The cgr side filters by SUFFIX; the oracle sees only the database.

    Greptile's other finding on PR #1513 was that "processing captured
    inheritance relationships can fail while filtering C++ source paths". The
    asymmetry is real -- `cpp_cgr_inheritance` accepts any `CPP_SUFFIXES` path
    (including `.c` and `.h`), while the oracle adjudicates only translation
    units the compilation database names -- but it is already defended:
    `score_inheritance` restricts cgr to `oracle.top_classes`.

    Measured rather than argued. Removing that restriction scores this fixture
    at precision 0.5 on one false positive; with it, 1.0. Nothing pinned that
    interaction before, so a future change to either side could reintroduce it
    silently.
    """
    src = tmp_path / "main.cpp"
    src.write_text(
        "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
    )
    # A real C++ file cgr indexes and the database does NOT name.
    (tmp_path / "extra.cpp").write_text(
        "class Other {};\nclass AlsoDerived : public Other {};\n", encoding="utf-8"
    )
    (tmp_path / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(tmp_path),
                    "command": f"clang++ -std=c++17 -c {src}",
                    "file": str(src),
                }
            ]
        ),
        encoding="utf-8",
    )

    oracle = cpp_oracle_inheritance(tmp_path)
    uncovered = ("extra.cpp:2", "Other")
    # The premise: cgr really does produce an edge the oracle never saw.
    cgr = CgrResult(inherits={*oracle.inherits, uncovered}, overrides=set())

    row = score_inheritance(cgr, oracle, inherits_label=ec.CPP_BASES_LABEL).rows[0]

    assert uncovered not in oracle.inherits
    assert row["fp"] == 0
    assert row["precision"] == 1.0
