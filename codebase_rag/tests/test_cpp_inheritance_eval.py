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

    def test_a_missing_database_yields_no_units(self, tmp_path: Path) -> None:
        (tmp_path / "a.cpp").write_text(
            "class Base {};\nclass Derived : public Base {};\n", encoding="utf-8"
        )

        assert _cpp_compile_db_units(tmp_path) == 0

    def test_an_empty_database_yields_no_units(self, tmp_path: Path) -> None:
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
