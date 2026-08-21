# Java inheritance grading for the eval harness (issue #1190). The javac oracle
# already emits extends/implements as name_edges, so this grades CGR's resolved
# INHERITS/IMPLEMENTS against it. The comparison unit is deliberately asymmetric
# and the label says so: the SUBCLASS is matched by location (exact), the
# supertype by simple name, because that is all the oracle names.
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from evals import constants as ec
from evals import inheritance as inh
from evals.inheritance import (
    java_cgr_inheritance,
    java_oracle_inheritance,
    score_inheritance,
)
from evals.oracles.java_oracle import java_available

_BASE = 'package com.app;\n\npublic class Base {\n    public String describe() {\n        return "base";\n    }\n}\n'
_SPEAKS = "package com.app;\n\npublic interface Speaks {\n    String speak();\n}\n"
_CHILD = (
    "package com.app;\n\n"
    "public class Child extends Base implements Speaks {\n"
    "    @Override\n"
    '    public String describe() {\n        return "child";\n    }\n\n'
    '    public String speak() {\n        return "hi";\n    }\n}\n'
)


def _write(repo: Path) -> None:
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "Base.java").write_text(_BASE, encoding="utf-8")
    (package / "Speaks.java").write_text(_SPEAKS, encoding="utf-8")
    (package / "Child.java").write_text(_CHILD, encoding="utf-8")


@pytest.mark.skipif(not java_available(), reason="java oracle needs a working JDK")
def test_oracle_reads_extends_and_implements(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write(repo)
    oracle = java_oracle_inheritance(repo)
    targets = {edge[1] for edge in oracle.inherits}
    assert targets == {"Base", "Speaks"}
    # The oracle emits no OVERRIDES, so the category stays empty rather than
    # being guessed at; score_inheritance then omits the row entirely.
    assert oracle.overrides == set()


@pytest.mark.skipif(not java_available(), reason="java oracle needs a working JDK")
def test_cgr_and_oracle_agree_on_the_fixture(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write(repo)
    result = score_inheritance(
        java_cgr_inheritance(repo, "proj"),
        java_oracle_inheritance(repo),
        inherits_label=ec.JAVA_SUPERTYPES_LABEL,
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["label"] == ec.JAVA_SUPERTYPES_LABEL
    assert (row["tp"], row["fp"], row["fn"]) == (2, 0, 0)


@pytest.mark.skipif(not java_available(), reason="java oracle needs a working JDK")
def test_no_overrides_row_is_emitted_for_java(tmp_path: Path) -> None:
    # CGR *does* resolve Java OVERRIDES, but the oracle cannot adjudicate them,
    # so reporting a score would be measuring nothing. Absence is the honest
    # result, and it must not silently become a 0.0 or a 1.0.
    repo = tmp_path / "proj"
    _write(repo)
    result = score_inheritance(
        java_cgr_inheritance(repo, "proj"),
        java_oracle_inheritance(repo),
        inherits_label=ec.JAVA_SUPERTYPES_LABEL,
    )
    assert [row["label"] for row in result.rows] == [ec.JAVA_SUPERTYPES_LABEL]


def test_the_java_label_differs_from_the_python_one() -> None:
    # A Java 1.0 must not be readable as the Python 1.0: different unit.
    assert ec.JAVA_SUPERTYPES_LABEL != ec.INHERITS_LABEL


def test_missing_java_toolchain_exits_instead_of_scoring_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without a JDK the oracle yields no edges. Scoring that would write a
    # header-only CSV and an empty diff and exit 0, which reads as "this repo
    # has no inheritance" rather than "the grader never ran" -- a measurement
    # that silently means nothing is worse than no measurement.
    repo = tmp_path / "proj"
    _write(repo)
    monkeypatch.setattr(inh, "java_available", lambda: False)
    with pytest.raises(typer.Exit) as exit_info:
        inh.main(
            target=repo,
            project_name="proj",
            out_dir=tmp_path / "out",
            language=ec.InheritanceLanguage.JAVA,
        )
    assert exit_info.value.exit_code == 1
    assert not (tmp_path / "out").exists()
