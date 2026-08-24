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


class EditorTemplateError(ValueError):
    """A user-supplied editor template cannot be applied.

    Carries the actionable message (bad template, cause, allowed slots) so
    the CLI can print it instead of a traceback.
    """


def _active_url_template() -> str | None:
    return settings.CGR_EDITOR_URL_TEMPLATE or cs.EDITOR_URL_TEMPLATES.get(
        resolve_editor()
    )


def url_template_problem() -> str | None:
    """Why the active URL template is unusable, or None when it is fine.

    Built-in templates always pass; this guards CGR_EDITOR_URL_TEMPLATE
    typos ({unknown} placeholders, unmatched braces) so a report renders
    with plain locations and a notice rather than aborting mid-table.
    """
    template = _active_url_template()
    if not template:
        return None
    try:
        template.format(**{cs.TEMPLATE_KEY_PATH: "", cs.TEMPLATE_KEY_LINE: 1})
    except (KeyError, IndexError, ValueError) as e:
        return cs.CLI_DUPLICATES_URL_TEMPLATE_INVALID.format(template=template, error=e)
    return None


def editor_url(absolute_path: Path, line: int) -> str | None:
    """URL opening the editor at path:line, or None when links are off."""
    template = _active_url_template()
    if not template or url_template_problem() is not None:
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
    passed as single argv entries and never re-parsed by a shell. A
    malformed CGR_DIFF_COMMAND (unbalanced quotes, unknown placeholders)
    raises EditorTemplateError with the actionable message.
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
    try:
        return [token.format(**substitutions) for token in shlex.split(template)]
    except (KeyError, IndexError, ValueError) as e:
        raise EditorTemplateError(
            cs.CLI_ERR_DUPLICATES_DIFF_TEMPLATE_INVALID.format(
                template=template, error=e
            )
        ) from e
