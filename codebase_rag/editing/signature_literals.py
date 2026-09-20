"""Literal/type compatibility checks for change_signature."""

from __future__ import annotations

import ast
import re

_KIND_OF = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    list: "list",
    dict: "dict",
    set: "set",
    tuple: "tuple",
    type(None): "None",
}
_ACCEPTS: dict[str, frozenset[str]] = {
    "int": frozenset({"int"}),
    "float": frozenset({"int", "float"}),
    "str": frozenset({"str"}),
    "bool": frozenset({"bool"}),
    "list": frozenset({"list"}),
    "dict": frozenset({"dict"}),
    "set": frozenset({"set"}),
    "tuple": frozenset({"tuple"}),
    "bytes": frozenset(),
}


def _literal_kind(literal: str) -> str | None:
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        return None
    return _KIND_OF.get(type(value))


def _compatible(kind: str, annotation: str) -> bool:
    """Whether a literal of `kind` fits `annotation` (builtin names only)."""
    text = annotation.strip()
    if kind == "None":
        return "None" in text or text.startswith("Optional")
    members = [m.strip() for m in re.split(r"\s*\|\s*", text)]
    if len(members) > 1:
        return any(_compatible(kind, member) for member in members)
    base = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", text)
    if base is None:
        return True
    accepted = _ACCEPTS.get(base.group(1))
    if accepted is None:
        # Not a builtin: the graph cannot tell, so do not accuse.
        return True
    return kind in accepted
