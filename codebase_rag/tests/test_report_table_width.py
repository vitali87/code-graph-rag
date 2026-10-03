"""Issue #2561: `dead-code` / `duplicates` table reports keep every name whole.

With no terminal to fit (an ``--output`` file, a pipe, a CI job) Rich laid
the table out at ``$COLUMNS`` or 80 columns and cut each long qualified name
with an ellipsis, so every javapoet row read the same and the saved report
depended on who ran it. The dead-code table also names each candidate's
file now, under the same width rules.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.models import AppContext
from codebase_rag.types_defs import DeadCodeRow, DuplicateGroup, DuplicateMember

PROJECT = "javapoet__43e4498c"
PKG = f"{PROJECT}.src.main.java.com.squareup.javapoet"
MAIN_DIR = "src/main/java/com/squareup/javapoet"
ELLIPSIS = "…"

# Rows that share a long prefix: cut at 80 columns they all read alike.
DEAD_NAMES = [
    f"{PKG}.CodeWriter.emitAndIndent(java.lang.String)",
    f"{PKG}.CodeWriter.emitWildcards(com.squareup.javapoet.WildcardTypeName)",
    f"{PKG}.TypeName.withoutAnnotations()",
]
# A name with a space in it used to wrap as well as being cut.
SPACED_NAME = (
    f"{PKG}.TypeVariableName.get(TypeVariable, Map<Element, TypeVariableName>)"
)


def _dead(qn: str, line: int, path: str | None = None) -> DeadCodeRow:
    top_level_class = qn.removeprefix(f"{PKG}.").split(".", 1)[0]
    return DeadCodeRow(
        label="Method",
        name=qn.rsplit(".", 1)[-1],
        qualified_name=qn,
        path=path or f"{MAIN_DIR}/{top_level_class}.java",
        start_line=line,
        end_line=line + 2,
    )


DEAD_ROWS = [
    _dead(qn, line) for line, qn in enumerate([*DEAD_NAMES, SPACED_NAME], start=100)
]
DEAD_PATHS = sorted({row["path"] for row in DEAD_ROWS})


def _member(qn: str, path: str, line: int) -> DuplicateMember:
    return DuplicateMember(
        label="Method",
        qualified_name=qn,
        name=qn.rsplit(".", 1)[-1],
        path=path,
        start_line=line,
        end_line=line + 3,
    )


TEST_PKG = f"{PROJECT}.src.test.java.com.squareup.javapoet"
TEST_DIR = "src/test/java/com/squareup/javapoet"
MEMBERS = [
    _member(
        f"{TEST_PKG}.AnnotationSpecTest.defaultAnnotation",
        f"{TEST_DIR}/AnnotationSpecTest.java",
        41,
    ),
    _member(
        f"{TEST_PKG}.AnnotationSpecTest.defaultAnnotationWithImport",
        f"{TEST_DIR}/AnnotationSpecTest.java",
        88,
    ),
    _member(
        f"{TEST_PKG}.TypeSpecTest.annotationWithFieldsInterface",
        f"{TEST_DIR}/TypeSpecTest.java",
        1203,
    ),
]
GROUPS = [
    DuplicateGroup(kind=cs.KIND_EXACT, similarity=1.0, node_count=40, members=MEMBERS)
]
LOCATIONS = [
    cs.CLI_DUPLICATES_LOCATION.format(
        path=m["path"], start=m["start_line"], end=m["end_line"]
    )
    for m in MEMBERS
]
# Each member's name within the project: all of what #2397's table shows once
# it drops the project prefix, and the part a cut name used to lose.
MEMBER_NAMES = [m["qualified_name"].removeprefix(f"{PROJECT}.") for m in MEMBERS]


def _no_terminal_anywhere(fd: int = 0) -> os.terminal_size:
    raise OSError(fd)


@pytest.fixture(autouse=True)
def _ci_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # As in a CI job: no $COLUMNS and no terminal on any standard stream, so
    # Rich's own fallback (80 columns) is what a report used to get.
    monkeypatch.delenv(cs.ENV_COLUMNS, raising=False)
    monkeypatch.setattr(os, "get_terminal_size", _no_terminal_anywhere)


@pytest.fixture
def app_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # The console the app builds, created after the environment above is set
    # so it cannot keep a width read at import time.
    monkeypatch.setattr(cli.app_context, "console", AppContext().console)


def _on_one_line(value: str, out: str) -> bool:
    return any(value in line for line in out.splitlines())


def _emit_dead_code(output: Path | None, rows: list[DeadCodeRow] = DEAD_ROWS) -> None:
    cli._emit_dead_code(rows, cs.DeadCodeFormat.TABLE, output, PROJECT)


def _emit_duplicates(output: Path | None) -> None:
    cli._emit_duplicates(
        GROUPS, cs.DuplicatesFormat.TABLE, output, PROJECT, analyzed_symbols=3
    )


@pytest.mark.usefixtures("app_console")
class TestAFileOrPipeGetsWholeNames:
    def test_dead_code_output_file_keeps_each_name_whole_on_one_line(
        self, tmp_path: Path
    ) -> None:
        report = tmp_path / "dead.txt"

        _emit_dead_code(report)

        text = report.read_text(encoding=cs.ENCODING_UTF8)
        assert ELLIPSIS not in text
        for value in [*DEAD_NAMES, SPACED_NAME, *DEAD_PATHS]:
            assert _on_one_line(value, text), value

    def test_piped_dead_code_keeps_each_name_whole_on_one_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _emit_dead_code(None)

        out = capsys.readouterr().out
        assert ELLIPSIS not in out
        for value in [*DEAD_NAMES, SPACED_NAME, *DEAD_PATHS]:
            assert _on_one_line(value, out), value

    def test_duplicates_output_file_keeps_members_and_locations_whole(
        self, tmp_path: Path
    ) -> None:
        report = tmp_path / "dups.txt"

        _emit_duplicates(report)

        text = report.read_text(encoding=cs.ENCODING_UTF8)
        assert ELLIPSIS not in text
        for value in [*MEMBER_NAMES, *LOCATIONS]:
            assert _on_one_line(value, text), value

    def test_piped_duplicates_keep_members_and_locations_whole(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _emit_duplicates(None)

        out = capsys.readouterr().out
        assert ELLIPSIS not in out
        for value in [*MEMBER_NAMES, *LOCATIONS]:
            assert _on_one_line(value, out), value


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _terminal(width: int) -> Console:
    # A VT-capable terminal, as #2397's duplicates tests pin it: on a Windows
    # CI runner Rich would otherwise detect a legacy console and change how
    # the table is drawn. No colour, so a line's length is its display width.
    return Console(
        file=_FakeTTY(), width=width, color_system=None, legacy_windows=False
    )


def _terminal_output(console: Console) -> str:
    file = console.file
    assert isinstance(file, _FakeTTY)
    return file.getvalue()


def _column(out: str, index: int) -> str:
    """One table column's body cells, joined in row order."""
    return "".join(
        line.split("│")[index].strip()
        for line in out.splitlines()
        if line.startswith("│")
    )


def _widest(out: str) -> int:
    return max(cell_len(line) for line in out.splitlines())


def _assert_names_and_paths_fold_whole(out: str, rows: list[DeadCodeRow]) -> None:
    names, paths = _column(out, 2), _column(out, 3)
    for row in rows:
        assert row["qualified_name"] in names, row["qualified_name"]
        assert row["path"] in paths, row["path"]


def test_a_narrow_terminal_folds_dead_code_names_and_paths_instead_of_cutting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = _terminal(60)
    monkeypatch.setattr(cli.app_context, "console", console)
    rows = [_dead(qn, 7) for qn in DEAD_NAMES]

    _emit_dead_code(None, rows)

    out = _terminal_output(console)
    assert ELLIPSIS not in out
    _assert_names_and_paths_fold_whole(out, rows)


# A route directory such as Next.js's `app/[slug]/` is a path and a module qn
# in square brackets, which Rich would read as a markup tag: `[slug]` vanished
# from the row and `[/slug]` raised before the report was written.
BRACKETED_ROWS = [
    _dead("proj.app.[slug].page.loader", 3, "app/[slug]/page.tsx"),
    _dead("proj.src.[/slug].helpers.tidy", 9, "src/[/slug]/helpers.py"),
]


def test_a_terminal_shows_bracketed_names_and_paths_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console = _terminal(120)
    monkeypatch.setattr(cli.app_context, "console", console)

    _emit_dead_code(None, BRACKETED_ROWS)

    _assert_names_and_paths_fold_whole(_terminal_output(console), BRACKETED_ROWS)


@pytest.mark.usefixtures("app_console")
def test_a_saved_report_keeps_bracketed_names_and_paths_as_written(
    tmp_path: Path,
) -> None:
    report = tmp_path / "dead.txt"

    _emit_dead_code(report, BRACKETED_ROWS)

    _assert_names_and_paths_fold_whole(
        report.read_text(encoding=cs.ENCODING_UTF8), BRACKETED_ROWS
    )


# Negative: what must not change.


@pytest.mark.parametrize("width", [60, 100])
def test_a_terminal_table_still_fits_the_terminal_width(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    # Only a destination with no screen gets the content's width; an
    # interactive terminal keeps its own, so nothing scrolls sideways.
    console = _terminal(width)
    monkeypatch.setattr(cli.app_context, "console", console)

    _emit_dead_code(None)
    _emit_duplicates(None)

    assert _widest(_terminal_output(console)) <= width


def test_an_explicit_columns_still_sets_a_file_report_width(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # $COLUMNS is the one width a user sets on purpose, so it still wins in a
    # file; the names fold inside it rather than losing their ends.
    monkeypatch.setenv(cs.ENV_COLUMNS, "70")
    report = tmp_path / "dead.txt"
    rows = [_dead(qn, 7) for qn in DEAD_NAMES]

    _emit_dead_code(report, rows)

    text = report.read_text(encoding=cs.ENCODING_UTF8)
    assert _widest(text) <= 70
    _assert_names_and_paths_fold_whole(text, rows)


def test_an_explicit_columns_still_sets_the_piped_table_width(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(cs.ENV_COLUMNS, "70")
    monkeypatch.setattr(cli.app_context, "console", AppContext().console)

    _emit_dead_code(None, [_dead(qn, 7) for qn in DEAD_NAMES])

    out = capsys.readouterr().out
    assert _widest(Text.from_ansi(out).plain) <= 70


def test_a_report_that_already_fits_is_written_as_before(tmp_path: Path) -> None:
    # A narrow table is not stretched, re-laid out or padded: the file holds
    # exactly what Rich renders for it at its old 80 columns.
    rows = [
        _dead("proj.mod.orphan", 5, "mod.py"),
        _dead("proj.mod.Thing.stale", 20, "mod.py"),
    ]
    report = tmp_path / "dead.txt"
    expected = io.StringIO()
    Console(file=expected, width=80).print(cli._build_dead_code_table(rows, "proj"))

    cli._emit_dead_code(rows, cs.DeadCodeFormat.TABLE, report, "proj")

    assert report.read_text(encoding=cs.ENCODING_UTF8) == expected.getvalue()


@pytest.mark.usefixtures("app_console")
def test_duplicates_json_file_is_unchanged(tmp_path: Path) -> None:
    # The dead-code JSON gains `path` (test_dead_code_json_path.py); the
    # duplicates envelope, which already had it, is written exactly as before.
    report = tmp_path / "dups.json"

    cli._emit_duplicates(GROUPS, cs.DuplicatesFormat.JSON, report, PROJECT)

    assert report.read_text(encoding=cs.ENCODING_UTF8) == json.dumps(
        {
            cs.KEY_DUPLICATE_GROUPS: GROUPS,
            cs.KEY_SKIPPED_SYMBOLS: 0,
            cs.KEY_TRUNCATED: False,
        },
        indent=2,
    )
