# Terminal-to-editor navigation for report output. Locations become OSC 8
# hyperlinks (cmd/ctrl-clickable in iTerm2, Kitty, WezTerm, Windows Terminal,
# VS Code's terminal) whose URL opens the user's editor at the exact line;
# side-by-side comparison shells out to the editor's diff CLI, since a
# hyperlink can carry only a single URL and no scheme opens two files at once.
from __future__ import annotations

import os
import shlex
from pathlib import Path
from urllib.parse import quote

from . import constants as cs
from .config import settings


def resolve_editor() -> str:
    """The editor name links should target, honoring CGR_EDITOR."""
    editor = settings.CGR_EDITOR.strip().lower()
    if editor != cs.EDITOR_AUTO:
        return editor
    bundle_id = os.environ.get(cs.ENV_CF_BUNDLE_ID, "").lower()
    for marker, name in cs.EDITOR_BUNDLE_MARKERS:
        if marker in bundle_id:
            return name
    # VS Code's integrated terminal (Cursor/Windsurf forks are caught above
    # by bundle id) and the plain-terminal fallback share one answer.
    return cs.EDITOR_VSCODE


def editor_url(absolute_path: Path, line: int) -> str | None:
    """URL opening the editor at path:line, or None when links are off."""
    template = settings.CGR_EDITOR_URL_TEMPLATE or cs.EDITOR_URL_TEMPLATES.get(
        resolve_editor()
    )
    if not template:
        return None
    return template.format(
        **{
            cs.TEMPLATE_KEY_PATH: quote(str(absolute_path)),
            cs.TEMPLATE_KEY_LINE: line,
        }
    )


def diff_command(left: Path, right: Path) -> list[str] | None:
    """Argv opening left and right side by side, or None when unavailable.

    Paths are substituted after shlex-splitting the template, so they are
    passed as single argv entries and never re-parsed by a shell.
    """
    template = settings.CGR_DIFF_COMMAND or cs.EDITOR_DIFF_COMMANDS.get(
        resolve_editor()
    )
    if not template:
        return None
    substitutions = {
        cs.TEMPLATE_KEY_LEFT: str(left),
        cs.TEMPLATE_KEY_RIGHT: str(right),
    }
    return [token.format(**substitutions) for token in shlex.split(template)]
