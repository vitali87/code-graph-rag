"""Parse SCIP symbol strings (the grammar documented in scip.proto).

    <symbol>     ::= <scheme> ' ' <manager> ' ' <package> ' ' <version> ' ' <descriptor>+
                   | 'local ' <local-id>
    <descriptor> ::= <name> '/'                 namespace
                   | <name> '#'                 type
                   | <name> '.'                 term
                   | <name> ':'                 meta
                   | <name> '!'                 macro
                   | <name> '(' <disamb>? ').'  method
                   | '[' <name> ']'             type parameter
                   | '(' <name> ')'             parameter

A space inside a header field is written twice; a name that is not a simple
identifier is wrapped in backticks, with a literal backtick doubled.
"""

from __future__ import annotations

from functools import lru_cache

from . import constants as sc
from .types_defs import Descriptor, ParsedSymbol

_HEADER_FIELDS = 4
_SIMPLE = frozenset("_+-$")


def _is_simple(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in _SIMPLE)


def _split_header(symbol: str) -> tuple[list[str], str] | None:
    fields: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(symbol) and len(fields) < _HEADER_FIELDS:
        char = symbol[index]
        if char == " ":
            if symbol.startswith("  ", index):
                current.append(" ")
                index += 2
                continue
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    if len(fields) < _HEADER_FIELDS:
        return None
    return fields, symbol[index:]


def _identifier(text: str, index: int) -> tuple[str, int]:
    if index < len(text) and text[index] == "`":
        out: list[str] = []
        index += 1
        while index < len(text):
            if text[index] == "`":
                if text.startswith("``", index):
                    out.append("`")
                    index += 2
                    continue
                return "".join(out), index + 1
            out.append(text[index])
            index += 1
        raise ValueError(sc.UNTERMINATED_ESCAPE)
    start = index
    while index < len(text) and _is_simple(text[index]):
        index += 1
    return text[start:index], index


def _descriptors(text: str) -> tuple[Descriptor, ...]:
    out: list[Descriptor] = []
    index = 0
    while index < len(text):
        opener = text[index]
        if opener in "[(":
            closer = "]" if opener == "[" else ")"
            name, index = _identifier(text, index + 1)
            if index >= len(text) or text[index] != closer:
                raise ValueError(sc.BAD_DESCRIPTOR.format(text=text))
            suffix = sc.TYPE_PARAMETER if opener == "[" else sc.PARAMETER
            out.append(Descriptor(name, suffix, ""))
            index += 1
            continue
        name, index = _identifier(text, index)
        if index >= len(text):
            raise ValueError(sc.BAD_DESCRIPTOR.format(text=text))
        marker = text[index]
        if marker == "(":
            disambiguator, index = _identifier(text, index + 1)
            if not text.startswith(").", index):
                raise ValueError(sc.BAD_DESCRIPTOR.format(text=text))
            out.append(Descriptor(name, sc.METHOD, disambiguator))
            index += 2
            continue
        if marker not in sc.SIMPLE_SUFFIXES:
            raise ValueError(sc.BAD_DESCRIPTOR.format(text=text))
        out.append(Descriptor(name, marker, ""))
        index += 1
    return tuple(out)


@lru_cache(maxsize=sc.SYMBOL_CACHE_SIZE)
def parse_symbol(symbol: str) -> ParsedSymbol | None:
    """The parsed symbol, or None when it does not follow the grammar."""
    if symbol.startswith(sc.LOCAL_PREFIX):
        return ParsedSymbol("", "", "", "", (), symbol[len(sc.LOCAL_PREFIX) :])
    header = _split_header(symbol)
    if header is None:
        return None
    (scheme, manager, package, version), rest = header
    try:
        descriptors = _descriptors(rest)
    except ValueError:
        return None
    if not descriptors:
        return None
    return ParsedSymbol(scheme, manager, package, version, descriptors, "")


def is_local(symbol: str) -> bool:
    return symbol.startswith(sc.LOCAL_PREFIX)


def last_descriptor(symbol: str) -> Descriptor | None:
    parsed = parse_symbol(symbol)
    if parsed is None or not parsed.descriptors:
        return None
    return parsed.descriptors[-1]


def is_method_symbol(symbol: str) -> bool:
    descriptor = last_descriptor(symbol)
    return descriptor is not None and descriptor.suffix == sc.METHOD


def is_type_symbol(symbol: str) -> bool:
    descriptor = last_descriptor(symbol)
    return descriptor is not None and descriptor.suffix == sc.TYPE


def member_symbol(type_symbol: str, method: str) -> str:
    """The symbol of method `method` declared directly in type `type_symbol`."""
    return f"{type_symbol}{method}{sc.METHOD_SUFFIX}"
