# Terminal-to-editor navigation for report output. Locations become OSC 8
# hyperlinks (cmd/ctrl-clickable in iTerm2, Kitty, WezTerm, Windows Terminal,
# VS Code's terminal) whose URL opens the user's editor at the exact line;
# side-by-side comparison shells out to the editor's diff CLI, since a
# hyperlink can carry only a single URL and no scheme opens two files at once.
from __future__ import annotations

import os
import shlex
import string
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
    # Off macOS there is no bundle id; editors that announce themselves via
    # TERM_PROGRAM (zed, vscode) are honored when the name is one we can
    # link to. VS Code forks inherit TERM_PROGRAM=vscode, so on Linux and
    # Windows they need an explicit CGR_EDITOR to be told apart.
    term_program = os.environ.get(cs.ENV_TERM_PROGRAM, "").strip().lower()
    if term_program in cs.EDITOR_URL_TEMPLATES:
        return term_program
    return cs.EDITOR_VSCODE


class EditorTemplateError(ValueError):
    """A user-supplied editor template cannot be applied.

    Carries the actionable message (bad template, cause, allowed slots) so
    the CLI can print it instead of a traceback.
    """


# Everything str.format can raise on a hostile template: unknown keyword
# ({oops} -> KeyError), positional field ({} / {0} -> IndexError), unmatched
# brace (-> ValueError), attribute access ({path.foo} -> AttributeError),
# and indexing a non-subscriptable value ({line[0]} -> TypeError).
_TEMPLATE_ERRORS = (KeyError, IndexError, ValueError, AttributeError, TypeError)


def _configured(value: str | None) -> str | None:
    """A user template setting, with blank/whitespace treated as unset."""
    if value is None:
        return None
    return value.strip() or None


def _active_url_template() -> str | None:
    return _configured(settings.CGR_EDITOR_URL_TEMPLATE) or cs.EDITOR_URL_TEMPLATES.get(
        resolve_editor()
    )


def _fields_problem(template: str, allowed: frozenset[str]) -> str | None:
    """Why template's replacement fields are unusable, or None when fine.

    Only bare allowed names pass: conversions ({path!r}), format specs
    ({line:>4}), indexing ({left[0]}), and attribute access ({path.foo})
    all format *something* silently wrong (a repr-quoted URL, one
    character of a path), so they are rejected up front rather than left
    to produce broken links or argv entries.
    """
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as e:
        return str(e)
    for _, name, spec, conversion in fields:
        if name is None:
            continue
        if name not in allowed or spec or conversion:
            rendered = name + (f"!{conversion}" if conversion else "")
            rendered += f":{spec}" if spec else ""
            return cs.EDITOR_ERR_UNSUPPORTED_FIELD.format(field=rendered)
    return None


def url_template_problem() -> str | None:
    """Why the active URL template is unusable, or None when it is fine.

    Built-in templates always pass; this guards CGR_EDITOR_URL_TEMPLATE
    typos ({unknown} placeholders, unmatched braces, {path!r}) so a report
    renders with plain locations and a notice rather than aborting
    mid-table or linking to a mangled URL.
    """
    template = _active_url_template()
    if not template:
        return None
    problem = _fields_problem(
        template, frozenset((cs.TEMPLATE_KEY_PATH, cs.TEMPLATE_KEY_LINE))
    )
    if problem is not None:
        return cs.CLI_DUPLICATES_URL_TEMPLATE_INVALID.format(
            template=template, error=problem
        )
    return None


def editor_url(absolute_path: Path, line: int) -> str | None:
    """URL opening the editor at path:line, or None when links are off.

    Paths go into the URL in POSIX form: editor deep links expect forward
    slashes on every platform (vscode://file/C:/dir/mod.py), and Windows
    backslashes would otherwise percent-encode into a broken %5C path.
    """
    if resolve_editor() == cs.EDITOR_NONE:
        return None
    template = _active_url_template()
    if not template or url_template_problem() is not None:
        return None
    return template.format(
        **{
            cs.TEMPLATE_KEY_PATH: quote(absolute_path.as_posix()),
            cs.TEMPLATE_KEY_LINE: line,
        }
    )


def diff_link(left: Path, left_line: int, right: Path, right_line: int) -> str | None:
    """`diff://` deep link opening two files side by side, or None when
    links are off. Terminals that understand the scheme (Croft) open the
    pair; single-file editor schemes cannot express it, so the link is a
    scheme of its own and other terminals refuse it like any custom URI.
    """
    if resolve_editor() == cs.EDITOR_NONE:
        return None
    # POSIX form for the same reason as editor_url: URL consumers parse
    # forward slashes; native Windows separators would arrive as %5C.
    return cs.DIFF_LINK_TEMPLATE.format(
        left=quote(f"{left.as_posix()}:{left_line}", safe=""),
        right=quote(f"{right.as_posix()}:{right_line}", safe=""),
    )


def diff_command(left: Path, right: Path) -> list[str] | None:
    """Argv opening left and right side by side, or None when unavailable.

    Paths are substituted after shlex-splitting the template, so they are
    passed as single argv entries and never re-parsed by a shell. A
    malformed CGR_DIFF_COMMAND (unbalanced quotes, unknown placeholders)
    raises EditorTemplateError with the actionable message.
    """
    template = _configured(settings.CGR_DIFF_COMMAND) or cs.EDITOR_DIFF_COMMANDS.get(
        resolve_editor()
    )
    if not template:
        return None
    problem = _fields_problem(
        template, frozenset((cs.TEMPLATE_KEY_LEFT, cs.TEMPLATE_KEY_RIGHT))
    )
    if problem is not None:
        raise EditorTemplateError(
            cs.CLI_ERR_DUPLICATES_DIFF_TEMPLATE_INVALID.format(
                template=template, error=problem
            )
        )
    substitutions = {
        cs.TEMPLATE_KEY_LEFT: str(left),
        cs.TEMPLATE_KEY_RIGHT: str(right),
    }
    try:
        tokens = shlex.split(template)
        # An argv that splits to nothing must never reach Popen; raise the
        # actionable error instead (whitespace-only *settings* already read
        # as unset above, so this guards degenerate built-ins/quoting).
        if not tokens:
            raise ValueError(cs.EDITOR_ERR_EMPTY_COMMAND)
        return [token.format(**substitutions) for token in tokens]
    except _TEMPLATE_ERRORS as e:
        raise EditorTemplateError(
            cs.CLI_ERR_DUPLICATES_DIFF_TEMPLATE_INVALID.format(
                template=template, error=e
            )
        ) from e
