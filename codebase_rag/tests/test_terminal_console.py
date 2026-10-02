"""Issue #2397: output written to a file or pipe carries no escape codes.

The app console used to be built with ``force_terminal=True``, so every
line printed through it arrived in redirected logs wrapped in colour and
style sequences, ``NO_COLOR`` stripped only the colours, and long status
lines were hard-wrapped mid-sentence.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest
from rich.panel import Panel

from codebase_rag import constants as cs
from codebase_rag.models import AppContext
from codebase_rag.utils.terminal_console import (
    TerminalAwareConsole,
    terminal_aware_console,
)

ESC = "\x1b["
STYLED_LINE = "[cyan]Knowledge graph sync done for 'pyproj'[/cyan] in 4.05s."
LONG_LINE = " ".join(
    ["Knowledge graph already in sync for 'x' (0.14s, no changes detected)."] * 3
)

_COLOUR_VARS = ("FORCE_COLOR", "NO_COLOR", "TTY_COMPATIBLE", "TERM", "COLUMNS")


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _neutral_colour_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _COLOUR_VARS:
        monkeypatch.delenv(name, raising=False)


def test_a_file_receives_plain_text() -> None:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)

    console.print(STYLED_LINE)

    assert ESC not in buffer.getvalue()
    assert "4.05s" in buffer.getvalue()


def test_force_color_opts_a_file_back_into_styling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)

    console.print(STYLED_LINE)

    assert ESC in buffer.getvalue()


def test_a_terminal_is_still_styled() -> None:
    tty = _FakeTTY()
    console = terminal_aware_console(file=tty)

    console.print(STYLED_LINE)

    assert ESC in tty.getvalue()


def test_no_color_on_a_terminal_drops_every_colour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    tty = _FakeTTY()
    console = terminal_aware_console(file=tty)

    console.print(STYLED_LINE)

    assert "\x1b[36m" not in tty.getvalue()


def test_a_plain_message_is_not_hard_wrapped_in_a_file() -> None:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)

    console.print(LONG_LINE)

    assert buffer.getvalue() == LONG_LINE + "\n"


def test_a_panel_in_a_file_still_wraps_its_body_rather_than_cropping() -> None:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)
    words = [f"w{i}" for i in range(60)]

    console.print(Panel(" ".join(words)))

    rendered = buffer.getvalue()
    assert all(word in rendered for word in words)
    assert max(len(line) for line in rendered.splitlines()) <= console.width


def test_the_app_console_is_terminal_aware() -> None:
    console = AppContext().console

    assert isinstance(console, TerminalAwareConsole)
    assert console.is_terminal == sys.stdout.isatty()


def test_the_app_console_writes_no_escapes_into_a_pipe() -> None:
    env = {k: v for k, v in os.environ.items() if k not in _COLOUR_VARS}
    script = (
        "from codebase_rag.main import app_context\n"
        f"app_context.console.print({STYLED_LINE!r})\n"
        f"app_context.console.print({LONG_LINE!r})\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        env=env,
        check=True,
        stdin=subprocess.DEVNULL,
    )

    assert ESC not in result.stdout
    assert LONG_LINE in result.stdout.splitlines()


def test_term_dumb_on_a_terminal_writes_no_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TERM", "dumb")
    tty = _FakeTTY()
    console = terminal_aware_console(file=tty)

    console.print(STYLED_LINE)

    assert ESC not in tty.getvalue()


def test_a_terminal_still_wraps_a_long_plain_message() -> None:
    tty = _FakeTTY()
    console = terminal_aware_console(file=tty)

    console.print(LONG_LINE)

    assert len(tty.getvalue().splitlines()) > 1


def test_an_explicit_soft_wrap_false_is_respected_in_a_file() -> None:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)

    console.print(LONG_LINE, soft_wrap=False)

    assert len(buffer.getvalue().splitlines()) > 1


def test_a_renderable_mixed_with_text_is_not_soft_wrapped() -> None:
    buffer = io.StringIO()
    console = terminal_aware_console(file=buffer)
    words = [f"w{i}" for i in range(60)]

    console.print("heading", Panel(" ".join(words)))

    rendered = buffer.getvalue()
    assert all(word in rendered for word in words)
