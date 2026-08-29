"""`cpp_l1` must refuse a database that yields no oracle nodes.

`restrict_to_files(cgr, {key.file for key in oracle.nodes})` scopes cgr to the
files the oracle names -- the compile_commands.json "defines the gradeable
universe", as the comment there says. When that universe is EMPTY the
restriction keeps nothing, and the run scores 0 against 0: a clean pass for a
target nothing analysed.

The availability check above it answers a different question (is libclang here,
does the file exist) and cannot see this, because a database naming only
out-of-target source passes both. Same class as the four preflight /
oracle disagreements fixed on PR #1513: the guard must key on what the grader
will actually GRADE, not on what is merely present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.oracles.cpp_oracle import cpp_available, run_cpp_oracle

_NEEDS_LIBCLANG = pytest.mark.skipif(
    not cpp_available(), reason="libclang not available"
)


def _ungradeable_project(tmp_path: Path) -> Path:
    """A project whose database is valid but names only out-of-target source.

    The oracle keeps a cursor only when its file resolves inside the target
    (`_rel` returns None otherwise), so this parses perfectly and yields no
    nodes -- exactly the shape the guard exists for, and reachable without
    depending on any other fix.
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
    return target


@_NEEDS_LIBCLANG
def test_the_oracle_yields_nothing_from_an_out_of_target_database(
    tmp_path: Path,
) -> None:
    """The premise, asserted so the guard's test cannot pass vacuously.

    Without this, `test_cpp_l1_refuses_a_database_that_grades_nothing` would
    also pass against a fixture the oracle simply could not read for some
    unrelated reason.
    """
    project = _ungradeable_project(tmp_path)

    graph = run_cpp_oracle(project)

    assert not graph.nodes


@_NEEDS_LIBCLANG
def test_cpp_l1_refuses_a_database_that_grades_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driving the real command, not a reimplementation of its logic."""
    import typer

    from evals import cpp_l1

    project = _ungradeable_project(tmp_path)

    with pytest.raises(typer.Exit) as excinfo:
        cpp_l1.main(target=project, project_name="", out_dir=tmp_path / "out")

    assert excinfo.value.exit_code == 1


@_NEEDS_LIBCLANG
def test_a_usable_database_is_not_refused(tmp_path: Path) -> None:
    """The control: the guard must not reject a project it can grade.

    Without this, a guard that refused every target would satisfy the test
    above.
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
                    "command": f"clang++ -std=c++17 -c {src}",
                    "file": str(src),
                }
            ]
        ),
        encoding="utf-8",
    )

    graph = run_cpp_oracle(tmp_path)

    assert graph.nodes


def test_the_diagnostic_does_not_blame_a_single_cause() -> None:
    """An empty oracle has at least four causes; the message must not pick one.

    The first version said "the database is stale or names no reachable
    source". Both bots flagged it independently, and both were right: a
    comment-only project parses perfectly and legitimately yields zero nodes,
    so that wording sends the reader to check a database that is fine
    (CodeRabbit and Greptile, PR #1517).

    Pinned as an assertion on the message rather than left to review, because
    a diagnostic that names the wrong cause is worse than one that names none
    -- it spends the reader's time before they can start looking.
    """
    from evals import logs as ls

    message = ls.CPP_ORACLE_GRADED_NOTHING.format(
        compdb="compile_commands.json", target="/repo"
    )

    # Each cause the oracle can return empty for must be reachable from the
    # text; none may be presented as the explanation.
    assert "outside" in message
    assert "stale" in message
    assert "failed to parse" in message
    assert "declare nothing" in message
