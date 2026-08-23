"""Calls that name their target in a string literal.

Some call sites do not name their callee syntactically: they pass its name as a
string to a dispatcher.

    const rows = await callSp('usp_invoice_list', params)   # TypeScript -> PL/pgSQL
    cursor.callproc('usp_invoice_list')                     # Python DB-API
    conn.prepareCall("{call usp_invoice_list(?)}")          # Java JDBC

No parser resolves those as calls, so the graph stops at the dispatcher and the
routine looks unreachable: dead-code reports it, and "what breaks if I change
this procedure" has no answer. That gap is widest exactly where it hurts most,
in codebases that keep business logic in the database.

The dispatcher is project-specific, so it is declared rather than guessed, in a
`.cgr.toml` at the repository root:

    [[string_calls]]
    callee = "callSp"      # dispatcher; matched on the last name segment
    arg_index = 0          # which argument carries the name

Nothing is inferred: without a declaration this module does nothing at all.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from ..sql_names import normalize_sql_reference

if TYPE_CHECKING:
    from tree_sitter import Node

CONFIG_FILENAME = ".cgr.toml"
CONFIG_SECTION = "string_calls"
# Points at a config file outside the repository, for indexing a checkout that
# cannot carry one (a read-only mount, a third-party repo).
CONFIG_PATH_ENV = "CGR_CONFIG_PATH"
_QUOTES = "\"'`"
# JDBC names its target inside an escape expression: `{call usp_x(?)}`, or
# `{?= call fn(?)}` for a function with a return value. The routine name is
# the token after `call`, still possibly schema-qualified.
_JDBC_CALL = re.compile(
    r"^\{\s*(?:\?\s*=\s*)?call\s+([^\s({}]+)\s*(?:\(.*\))?\s*\}$",
    re.IGNORECASE | re.DOTALL,
)
# ECMAScript's braced form (`\u{5f}` .. `\u{10FFFF}`), which the
# unicode_escape codec does not know. The leading backslashes are captured so
# parity decides: an even count means the `u{...}` is literal text behind
# escaped backslashes and stays for the codec to handle.
_BRACED_UNICODE = re.compile(r"(\\+)u\{([0-9a-fA-F]{1,6})\}")


def _decode_braced_escape(match: re.Match[str]) -> str:
    slashes = match.group(1)
    if len(slashes) % 2 == 0:
        return match.group(0)
    return slashes[:-1] + chr(int(match.group(2), 16))


@dataclass(frozen=True)
class StringCallSpec:
    """A dispatcher whose argument names the real callee."""

    callee: str
    arg_index: int = 0


def load_string_call_specs(repo_root: Path) -> tuple[StringCallSpec, ...]:
    """Read `[[string_calls]]` from the repository's `.cgr.toml`.

    A missing or malformed file disables the feature instead of failing the
    ingestion: this is an optional refinement, never a prerequisite.
    """
    override = os.environ.get(CONFIG_PATH_ENV)
    config_path = Path(override) if override else repo_root / CONFIG_FILENAME
    if not config_path.is_file():
        return ()
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(f"Ignoring unreadable {CONFIG_FILENAME}: {exc}")
        return ()

    section = data.get(CONFIG_SECTION, [])
    if not isinstance(section, list):
        # A scalar here (`string_calls = 5`) is valid TOML; iterating it
        # would raise while the call processor initializes and abort the
        # whole ingestion over an optional refinement.
        logger.warning(
            f"Ignoring {CONFIG_FILENAME}: {CONFIG_SECTION} is not an array of tables"
        )
        return ()

    specs: list[StringCallSpec] = []
    for entry in section:
        if not isinstance(entry, dict):
            continue
        callee = entry.get("callee")
        if not isinstance(callee, str) or not callee:
            continue
        arg_index = entry.get("arg_index", 0)
        # bool is an int subclass: `arg_index = true` must not select index 1.
        if (
            isinstance(arg_index, bool)
            or not isinstance(arg_index, int)
            or arg_index < 0
        ):
            arg_index = 0
        specs.append(StringCallSpec(callee=callee, arg_index=arg_index))

    if specs:
        names = ", ".join(spec.callee for spec in specs)
        logger.info(f"String-call dispatchers declared in {CONFIG_FILENAME}: {names}")
    return tuple(specs)


def _string_literal_value(node: Node) -> str | None:
    """The text of a string literal, or None when the argument is not one.

    A non-literal argument (a variable, a template with substitutions) names a
    target only at runtime; guessing there would invent edges.
    """
    text_bytes = node.text
    if not text_bytes:
        return None
    text = text_bytes.decode("utf-8", "replace").strip()
    if len(text) < 2 or text[0] not in _QUOTES or text[-1] != text[0]:
        return None
    value = text[1:-1].strip()
    if not value or "${" in value or "%s" in value:
        return None
    if "\\" in value:
        # The raw source spelling may escape characters ("usp_invoice")
        # that the runtime string does not contain; resolve the runtime
        # string or the lookup misses the routine it plainly names.
        try:
            value = _BRACED_UNICODE.sub(_decode_braced_escape, value)
            value = value.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, ValueError):
            return None
    return value


def string_call_target(
    call_node: Node, call_name: str, specs: tuple[StringCallSpec, ...]
) -> str | None:
    """The routine named by a declared dispatcher's argument, if any.

    `call_name` is matched on its LAST segment so a dispatcher reached through
    a namespace or an object (`db.callSp`, `this.callSp`) still matches.
    A schema-qualified target (`app.usp_x`) stays qualified: the definition
    side registers routines under their schema-qualified name, and reducing
    the target here would fan the call onto every schema's same-named routine.
    A JDBC escape expression (`{call usp_x(?)}`) is unwrapped to its routine.
    """
    if not specs or not call_name:
        return None
    # The call name can arrive decorated by the surrounding expression
    # (`await callSp`, `new Dispatcher`); the callee is its last token, and the
    # last dotted segment of that.
    tokens = call_name.split()
    if not tokens:
        return None
    last_segment = tokens[-1].rsplit(".", 1)[-1]
    for spec in specs:
        if last_segment != spec.callee:
            continue
        # The arguments FIELD first: a generic call (`callSp<Proc>("usp_x")`)
        # puts a type_arguments child before the value arguments, and a
        # substring scan would pick it up and see no string literal. The scan
        # stays as a fallback for grammars without the field, skipping the
        # type-argument nodes it can now tell apart.
        arguments = call_node.child_by_field_name("arguments") or next(
            (
                child
                for child in call_node.named_children
                if "argument" in child.type and "type" not in child.type
            ),
            None,
        )
        if arguments is None:
            return None
        args = [
            child
            for child in arguments.named_children
            if child.type not in {"comment", "line_comment", "block_comment"}
        ]
        if spec.arg_index >= len(args):
            return None
        value = _string_literal_value(args[spec.arg_index])
        if not value:
            return None
        jdbc = _JDBC_CALL.match(value)
        if jdbc:
            value = jdbc.group(1)
        # The SAME normalizer as the definition side: PostgreSQL folds an
        # unquoted identifier to lowercase, so callSp('MyFunc') must look up
        # myfunc — quoting inside the string ('"MyFunc"') preserves case,
        # exactly as it would in SQL.
        return normalize_sql_reference(value) or None
    return None
