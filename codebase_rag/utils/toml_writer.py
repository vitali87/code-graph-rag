"""TOML text exactly as tomli-w writes it, for cgr's workspace files.

The Rust extension writes it (rust/cgr_graph_rs/src/formats/toml.rs) when a
table holds only dicts, lists, strings, booleans and 64-bit integers. This
module is the reference: it writes whatever the extension declines, and
everything where the extension is not installed.

The layout is tomli-w's with its defaults (four-space indent, no multi-line
strings): a table's plain values first, then its sub-tables; an array of
tables stays inline unless one of its tables renders longer than a
100-character line, in which case every one becomes a `[[name]]` section.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import TypeGuard

from .. import exceptions as ex
from ..constants.embedded_graph import EG_RUST_TOML_DUMPS
from ..embedded_graph.extension import rust_extension

__all__ = ["dumps", "dumps_python"]

_INDENT = "    "
_MAX_LINE_LENGTH = 100
_BARE_KEY_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
# Control characters other than tab, as `\uXXXX` unless TOML has a short
# escape for them; quotes and backslashes too.
_ESCAPES = {code: f"\\u{code:04x}" for code in [*range(0x20), 0x7F] if code != 0x09}
_ESCAPES |= {
    0x08: "\\b",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def dumps(table: Mapping[str, object]) -> str:
    """The TOML document whose top-level table is `table`."""
    native = rust_extension()
    native_dumps = getattr(native, EG_RUST_TOML_DUMPS, None)
    if native_dumps is not None and (text := native_dumps(table)) is not None:
        return text
    return dumps_python(table)


def dumps_python(table: Mapping[str, object]) -> str:
    """`dumps`, written by the reference writer."""
    return "".join(_section(table, name="", inside_aot=False))


def _section(
    table: Mapping[str, object], *, name: str, inside_aot: bool
) -> Iterator[str]:
    # Each plain value is rendered once, here: an array of tables is
    # rendered to decide whether it stays inline, and that text is kept.
    literals: list[tuple[str, str]] = []
    sections: list[tuple[str, Mapping[str, object], bool]] = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            sections.append((key, value, False))
        elif _is_array_of_tables(value):
            rendered = [_inline_table(sub) for sub in value]
            if all(map(_fits_inline, rendered)):
                items = "".join(f"{_INDENT}{text},\n" for text in rendered)
                literals.append((key, f"[\n{items}]"))
            else:
                sections.extend((key, sub, True) for sub in value)
        else:
            literals.append((key, _literal(value)))

    written = False
    if inside_aot or (name and (literals or not sections)):
        written = True
        yield f"[[{name}]]\n" if inside_aot else f"[{name}]\n"
    for key, text in literals:
        written = True
        yield f"{_key(key)} = {text}\n"
    for key, sub, in_aot in sections:
        if written:
            yield "\n"
        written = True
        child = f"{name}.{_key(key)}" if name else _key(key)
        yield from _section(sub, name=child, inside_aot=in_aot)


def _is_array_of_tables(value: object) -> TypeGuard[Sequence[Mapping[str, object]]]:
    return (
        isinstance(value, list | tuple)
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
    )


def _fits_inline(rendered: str) -> bool:
    # As the line `    { ... },` inside the array.
    return len(_INDENT) + len(rendered) + 1 <= _MAX_LINE_LENGTH and "\n" not in rendered


def _literal(value: object, nest_level: int = 0) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list | tuple):
        return _inline_array(value, nest_level)
    if isinstance(value, Mapping):
        return _inline_table(value)
    raise TypeError(ex.TOML_UNSUPPORTED_VALUE.format(type=type(value).__qualname__))


def _inline_array(items: list | tuple, nest_level: int) -> str:
    if not items:
        return "[]"
    item_indent = _INDENT * (nest_level + 1)
    body = ",\n".join(item_indent + _literal(item, nest_level + 1) for item in items)
    return f"[\n{body},\n{_INDENT * nest_level}]"


def _inline_table(table: Mapping[str, object]) -> str:
    if not table:
        return "{}"
    body = ", ".join(f"{_key(key)} = {_literal(value)}" for key, value in table.items())
    return f"{{ {body} }}"


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise TypeError(
            ex.TOML_KEY_NOT_STR.format(key=key, type=type(key).__qualname__)
        )
    if key and _BARE_KEY_CHARS.issuperset(key):
        return key
    return _string(key)


def _string(text: str) -> str:
    return f'"{text.translate(_ESCAPES)}"'
