"""Issue #2662: graph names reach the terminal literally, never as Rich markup.

File-system routers put bracketed segments in paths (`app/[id]/page.tsx`,
`pages/[slug].tsx`), so qualified names such as `repo.app.[id].page.Page`
are ordinary. Rich read `[id]` as a style tag and dropped it from the
dead-code and duplicates tables and from `diff-index` stdout, and a `[/]`
in a name raised MarkupError.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.types_defs import DeadCodeRow, DuplicateGroup, DuplicateMember

BRACKETED = ["[id]", "[slug]", "[lang]", "[locale]", "[...slug]", "[/]", "[bold red]"]


def _qn(segment: str) -> str:
    return f"repo.app.{segment}.page.unusedHelper"


def _path(segment: str) -> str:
    return f"app/{segment}/page.tsx"


def _dead_row(qn: str, path: str = "mod.py") -> DeadCodeRow:
    return DeadCodeRow(
        label="Function",
        name=qn.rsplit(".", 1)[-1],
        qualified_name=qn,
        path=path,
        start_line=7,
        end_line=9,
    )


def _member(qn: str, path: str, line: int) -> DuplicateMember:
    return DuplicateMember(
        label="Function",
        qualified_name=qn,
        name=qn.rsplit(".", 1)[-1],
        path=path,
        start_line=line,
        end_line=line + 2,
    )


def _group(segment: str) -> DuplicateGroup:
    return DuplicateGroup(
        kind=cs.KIND_EXACT,
        similarity=1.0,
        node_count=12,
        members=[
            _member(_qn(segment), _path(segment), 7),
            _member("repo.app.other.page.unusedHelper", "app/other/page.tsx", 3),
        ],
    )


def _plain_console() -> Console:
    return Console(file=io.StringIO(), width=200, color_system=None)


def _output(console: Console) -> str:
    file = console.file
    assert isinstance(file, io.StringIO)
    return file.getvalue()


@pytest.fixture
def app_console(monkeypatch: pytest.MonkeyPatch) -> Console:
    console = _plain_console()
    monkeypatch.setattr(cli.app_context, "console", console)
    return console


class TestDeadCodeTable:
    @pytest.mark.parametrize("segment", BRACKETED)
    def test_bracketed_segment_survives_on_screen(
        self, segment: str, app_console: Console
    ) -> None:
        cli._emit_dead_code(
            [_dead_row(_qn(segment), _path(segment))],
            cs.DeadCodeFormat.TABLE,
            None,
            "repo",
        )

        assert _qn(segment) in _output(app_console)

    @pytest.mark.parametrize("segment", BRACKETED)
    def test_bracketed_segment_survives_in_the_output_file(
        self, segment: str, app_console: Console, tmp_path: Path
    ) -> None:
        report = tmp_path / "dead.txt"

        cli._emit_dead_code(
            [_dead_row(_qn(segment), _path(segment))],
            cs.DeadCodeFormat.TABLE,
            report,
            "repo",
        )

        assert _qn(segment) in report.read_text(encoding=cs.ENCODING_UTF8)


class TestDuplicatesTable:
    @pytest.mark.parametrize("segment", BRACKETED)
    def test_member_and_rootless_location_keep_the_segment(self, segment: str) -> None:
        console = _plain_console()

        console.print(cli._build_duplicates_table([_group(segment)], "repo"))

        out = _output(console)
        # The title names the project, so a member drops that prefix.
        assert _qn(segment).removeprefix("repo.") in out
        assert f"{_path(segment)}:7-9" in out

    def test_linked_location_keeps_the_segment(self, tmp_path: Path) -> None:
        console = _plain_console()

        console.print(
            cli._build_duplicates_table([_group("[id]")], "repo", root_path=tmp_path)
        )

        assert "app/[id]/page.tsx:7-9" in _output(console)


class TestDiffIndexStdout:
    # The shape diff_indexes returns, with route segments in every string.
    DIFF = {
        "nodes": {
            "added": [
                "file::app/[id]/page.tsx::app/[id]/page.tsx",
                "function::repo.app.[id].page.Page::",
            ],
            "removed": [],
            "changed": {},
        },
        "relationships": {
            "CONTAINS_FILE": {
                "added": ["app/[slug] -> app/[slug]/page.tsx [/]"],
                "removed": [],
                "changed": {},
            }
        },
        "coverage": {"flow_covered_flips": {}, "per_language": {}},
    }

    def test_stdout_is_the_json_with_every_segment(self) -> None:
        with patch("codebase_rag.cli.diff_indexes", return_value=self.DIFF):
            result = CliRunner().invoke(app, ["diff-index", "--old", "a", "--new", "b"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == self.DIFF

    def test_stdout_matches_the_json_out_file_byte_for_byte(
        self, tmp_path: Path
    ) -> None:
        written = tmp_path / "diff.json"
        with patch("codebase_rag.cli.diff_indexes", return_value=self.DIFF):
            to_file = CliRunner().invoke(
                app,
                ["diff-index", "--old", "a", "--new", "b", "--json-out", str(written)],
            )
            to_stdout = CliRunner().invoke(
                app, ["diff-index", "--old", "a", "--new", "b"]
            )

        assert to_file.exit_code == 0, to_file.output
        assert to_stdout.stdout == written.read_text(encoding="utf-8")

    def test_stdout_carries_no_escape_codes(self) -> None:
        # The JSON is data for jq or a file, so the console's syntax
        # highlighting must not reach it.
        with patch("codebase_rag.cli.diff_indexes", return_value=self.DIFF):
            result = CliRunner().invoke(app, ["diff-index", "--old", "a", "--new", "b"])

        assert "\x1b[" not in result.stdout


# Negative: what must not change.


def test_plain_names_render_as_before() -> None:
    console = _plain_console()

    console.print(cli._build_dead_code_table([_dead_row("proj.mod.orphan")], "proj"))

    out = _output(console)
    assert "proj.mod.orphan" in out
    assert "7-9" in out


def test_title_markup_is_still_interpreted() -> None:
    # Only graph-derived cells are literal; the table's own styled title must
    # not start showing its markup tags.
    console = _plain_console()

    console.print(
        cli._build_dead_code_table([_dead_row(_qn("[id]"), _path("[id]"))], "proj")
    )

    out = _output(console)
    assert "Dead Code Candidates (proj)" in out
    assert "[bold" not in out
    assert "[/bold" not in out


def test_name_column_keeps_its_colour() -> None:
    console = Console(
        file=io.StringIO(), width=200, color_system="standard", force_terminal=True
    )

    console.print(cli._build_dead_code_table([_dead_row("proj.mod.orphan")], "proj"))

    cyan = "\x1b[36m"
    assert f"{cyan}proj.mod.orphan" in _output(console)


def test_empty_diff_still_reports_no_differences() -> None:
    empty = {
        "nodes": {"added": [], "removed": [], "changed": {}},
        "relationships": {},
        "coverage": {"flow_covered_flips": {}, "per_language": {}},
    }
    with patch("codebase_rag.cli.diff_indexes", return_value=empty):
        result = CliRunner().invoke(app, ["diff-index", "--old", "a", "--new", "b"])

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_DIFF_EMPTY in result.output
