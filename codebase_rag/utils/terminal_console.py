"""The console cgr prints user-facing output through.

Styling is left to Rich's own detection rather than forced on: a terminal
gets colour, a file or pipe gets plain text, and ``NO_COLOR``,
``TERM=dumb``, ``FORCE_COLOR`` and ``TTY_COMPATIBLE`` all mean what they
say. Forcing a terminal wrote escape sequences into every redirected log
and ``| grep`` pipeline (issue #2397).
"""

from __future__ import annotations

from typing import IO

from rich.console import Console, JustifyMethod, OverflowMethod, RenderableType
from rich.style import Style


class TerminalAwareConsole(Console):
    """A console that does not hard-wrap plain messages written to a file.

    Rich wraps every ``print`` at the console width, which for a file or pipe
    is the controlling terminal's width or 80 columns. A status line split
    mid-sentence breaks line-oriented parsing, so a message made only of
    strings is written on one line when nothing will display it. The
    console-wide ``soft_wrap`` switch is not used for this: it also stops a
    ``Panel`` wrapping its body, and the panel then crops the text instead.
    """

    def print(
        self,
        *objects: RenderableType,
        sep: str = " ",
        end: str = "\n",
        style: str | Style | None = None,
        justify: JustifyMethod | None = None,
        overflow: OverflowMethod | None = None,
        no_wrap: bool | None = None,
        emoji: bool | None = None,
        markup: bool | None = None,
        highlight: bool | None = None,
        width: int | None = None,
        height: int | None = None,
        crop: bool = True,
        soft_wrap: bool | None = None,
        new_line_start: bool = False,
    ) -> None:
        if (
            soft_wrap is None
            and not self.is_terminal
            and all(isinstance(obj, str) for obj in objects)
        ):
            soft_wrap = True
        super().print(
            *objects,
            sep=sep,
            end=end,
            style=style,
            justify=justify,
            overflow=overflow,
            no_wrap=no_wrap,
            emoji=emoji,
            markup=markup,
            highlight=highlight,
            width=width,
            height=height,
            crop=crop,
            soft_wrap=soft_wrap,
            new_line_start=new_line_start,
        )


def terminal_aware_console(
    *, stderr: bool = False, file: IO[str] | None = None
) -> TerminalAwareConsole:
    return TerminalAwareConsole(width=None, stderr=stderr, file=file)
