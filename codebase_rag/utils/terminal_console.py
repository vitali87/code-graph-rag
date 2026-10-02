"""The console cgr prints user-facing output through.

Styling is left to Rich's own detection rather than forced on: a terminal
gets colour, a file or pipe gets plain text, and ``NO_COLOR``,
``TERM=dumb``, ``FORCE_COLOR`` and ``TTY_COMPATIBLE`` all mean what they
say. Forcing a terminal wrote escape sequences into every redirected log
and ``| grep`` pipeline (issue #2397).
"""

from __future__ import annotations

from typing import IO, Unpack

from rich.console import Console, RenderableType

from ..types_defs import ConsolePrintOptions


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
        soft_wrap: bool | None = None,
        **options: Unpack[ConsolePrintOptions],
    ) -> None:
        if (
            soft_wrap is None
            and not self.is_terminal
            and all(isinstance(obj, str) for obj in objects)
        ):
            soft_wrap = True
        super().print(*objects, soft_wrap=soft_wrap, **options)


def terminal_aware_console(
    *, stderr: bool = False, file: IO[str] | None = None
) -> TerminalAwareConsole:
    return TerminalAwareConsole(width=None, stderr=stderr, file=file)
