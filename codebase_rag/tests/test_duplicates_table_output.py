"""Issue #2397 (comment): `cgr duplicates` output in a pipe is plain text.

The table wrapped every Group and Location cell in OSC 8 hyperlink escapes
even when redirected, and every Member cell repeated the project prefix the
title already names, so at pipe width the column showed nothing but it.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.types_defs import DuplicateGroup, DuplicateMember
from codebase_rag.utils.terminal_console import terminal_aware_console

PROJECT = "click__d1899da0"
OSC8 = "\x1b]8;"


def _member(qn: str, path: str, line: int) -> DuplicateMember:
    return DuplicateMember(
        label="Function",
        qualified_name=qn,
        name=qn.rsplit(".", 1)[-1],
        path=path,
        start_line=line,
        end_line=line + 1,
    )


GROUPS = [
    DuplicateGroup(
        kind=cs.KIND_EXACT,
        similarity=1.0,
        node_count=12,
        members=[
            _member(
                f"{PROJECT}.tests.test_arguments.test_good_defaults_for_nargs",
                "tests/test_arguments.py",
                985,
            ),
            _member(
                f"{PROJECT}.tests.test_basic.test_flag_value_dual_options",
                "tests/test_basic.py",
                411,
            ),
        ],
    )
]


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _render(
    monkeypatch: pytest.MonkeyPatch, file: io.StringIO, width: int = 200
) -> str:
    # The fake TTY models a VT-capable terminal; pin the environment so Rich
    # does not inherit the CI runner's ``TERM=dumb`` setting.
    monkeypatch.setenv("TERM", "xterm-256color")
    console = terminal_aware_console(file=file)
    console.width = width
    # A VT-capable terminal, as Windows Terminal is detected: Rich never
    # writes OSC 8 links for a legacy Windows console, which is what it
    # detects on a CI runner whose stdout is not a console at all.
    console.legacy_windows = False
    monkeypatch.setattr(cli.app_context, "console", console)
    cli._emit_duplicates(
        GROUPS,
        cs.DuplicatesFormat.TABLE,
        None,
        PROJECT,
        analyzed_symbols=2,
        root_path=Path("/tmp/click"),
    )
    return file.getvalue()


def test_a_piped_table_carries_no_hyperlink_or_style_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _render(monkeypatch, io.StringIO())

    assert OSC8 not in out
    assert "\x1b[" not in out
    assert "tests/test_arguments.py:985-986" in out


def test_members_are_named_without_the_project_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _render(monkeypatch, io.StringIO())

    assert "tests.test_arguments.test_good_defaults_for_nargs" in out
    assert f"{PROJECT}.tests" not in out


def test_a_terminal_still_gets_the_editor_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: the links are for a terminal that can open them; only a
    # redirect loses them.
    out = _render(monkeypatch, _FakeTTY())

    assert OSC8 in out


def test_a_member_outside_the_project_keeps_its_full_name() -> None:
    # Negative: only the title's own `<project>.` prefix is dropped; a name
    # that merely starts with the same characters is not shortened.
    table = cli._build_duplicates_table(
        [
            DuplicateGroup(
                kind=cs.KIND_EXACT,
                similarity=1.0,
                node_count=3,
                members=[
                    _member(f"{PROJECT}x.mod.f", "mod.py", 1),
                    _member(f"{PROJECT}.mod.g", "mod.py", 5),
                ],
            )
        ],
        PROJECT,
    )
    buffer = io.StringIO()
    terminal_aware_console(file=buffer).print(table)

    assert f"{PROJECT}x.mod.f" in buffer.getvalue()
    assert f"{PROJECT}.mod.g" not in buffer.getvalue()


def _column(out: str, index: int) -> str:
    """One table column's body cells, joined in row order."""
    return "".join(
        line.split("│")[index].strip()
        for line in out.splitlines()
        if line.startswith("│")
    )


def test_long_names_are_folded_not_cut_at_pipe_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # At 80 columns every Member and Location cell ended in an ellipsis, so
    # a redirected report never named the function (issue #2397, comment).
    out = _render(monkeypatch, io.StringIO(), width=80)

    assert "…" not in out
    assert "tests.test_arguments.test_good_defaults_for_nargs" in _column(out, 4)
    assert "tests/test_arguments.py:985-986" in _column(out, 5)
