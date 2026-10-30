"""Read a SCIP index (`index.scip`) without generated protobuf code.

Only the fields the call oracle needs are decoded; everything else is
skipped by wire type, so a newer index with fields this reader does not know
still parses. Field numbers are those of sourcegraph/scip's `scip.proto`:

    Index:             1 metadata, 2 documents, 3 external_symbols
    Metadata:          2 tool_info, 3 project_root
    ToolInfo:          1 name, 2 version
    Document:          1 relative_path, 2 occurrences, 3 symbols, 4 language
    Occurrence:        1 range, 2 symbol, 3 symbol_roles, 7 enclosing_range
    SymbolInformation: 1 symbol, 4 relationships, 5 kind, 6 display_name,
                       8 enclosing_symbol
    Relationship:      1 symbol, 2 is_reference, 3 is_implementation,
                       4 is_type_definition, 5 is_definition
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from codebase_rag import constants as cs

from . import constants as sc
from .types_defs import (
    ScipDocument,
    ScipIndex,
    ScipOccurrence,
    ScipRange,
    ScipRelationship,
    ScipSymbol,
)

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LENGTH = 2
_WIRE_FIXED32 = 5
_FIXED64_BYTES = 8
_FIXED32_BYTES = 4
_SHORT_RANGE = 3
_FULL_RANGE = 4

_Field = tuple[int, int, int | memoryview]


class ScipDecodeError(ValueError):
    pass


def _varint(buf: memoryview, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ScipDecodeError(sc.TRUNCATED_VARINT)
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(buf: memoryview) -> Iterator[_Field]:
    pos = 0
    end = len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 0x7
        match wire:
            case 0:
                value, pos = _varint(buf, pos)
                yield number, wire, value
            case 2:
                length, pos = _varint(buf, pos)
                if pos + length > end:
                    raise ScipDecodeError(sc.TRUNCATED_FIELD.format(field=number))
                yield number, wire, buf[pos : pos + length]
                pos += length
            case 1:
                pos += _FIXED64_BYTES
            case 5:
                pos += _FIXED32_BYTES
            case _:
                raise ScipDecodeError(sc.UNKNOWN_WIRE.format(wire=wire, field=number))


def _text(value: int | memoryview) -> str:
    if isinstance(value, int):
        raise ScipDecodeError(sc.EXPECTED_BYTES)
    return bytes(value).decode(cs.ENCODING_UTF8)


def _int(value: int | memoryview) -> int:
    if not isinstance(value, int):
        raise ScipDecodeError(sc.EXPECTED_VARINT)
    # int32 fields carry negatives as 64-bit two's complement varints.
    return value - (1 << 64) if value >= 1 << 63 else value


def _ints(value: int | memoryview, wire: int) -> list[int]:
    # proto3 packs repeated scalars, but a reader must accept both encodings.
    if wire == _WIRE_VARINT:
        return [_int(value)]
    if isinstance(value, int):
        raise ScipDecodeError(sc.EXPECTED_BYTES)
    out: list[int] = []
    pos = 0
    while pos < len(value):
        item, pos = _varint(value, pos)
        out.append(_int(item))
    return out


def _range(values: list[int]) -> ScipRange | None:
    # A three-element range sits on one line: [line, start_char, end_char].
    if len(values) == _SHORT_RANGE:
        return ScipRange(values[0], values[1], values[0], values[2])
    if len(values) == _FULL_RANGE:
        return ScipRange(values[0], values[1], values[2], values[3])
    return None


def _message(value: int | memoryview) -> memoryview:
    if isinstance(value, int):
        raise ScipDecodeError(sc.EXPECTED_BYTES)
    return value


def _occurrence(buf: memoryview) -> ScipOccurrence | None:
    range_values: list[int] = []
    enclosing_values: list[int] = []
    symbol = ""
    roles = 0
    for number, wire, value in _fields(buf):
        match number:
            case 1:
                range_values.extend(_ints(value, wire))
            case 2:
                symbol = _text(value)
            case 3:
                roles = _int(value)
            case 7:
                enclosing_values.extend(_ints(value, wire))
            case _:
                pass
    occurrence_range = _range(range_values)
    if occurrence_range is None or not symbol:
        return None
    return ScipOccurrence(
        range=occurrence_range,
        symbol=symbol,
        roles=roles,
        enclosing=_range(enclosing_values),
    )


def _relationship(buf: memoryview) -> ScipRelationship:
    symbol = ""
    flags = {2: False, 3: False, 4: False, 5: False}
    for number, _wire, value in _fields(buf):
        if number == 1:
            symbol = _text(value)
        elif number in flags:
            flags[number] = bool(_int(value))
    return ScipRelationship(
        symbol=symbol,
        is_reference=flags[2],
        is_implementation=flags[3],
        is_type_definition=flags[4],
        is_definition=flags[5],
    )


def _symbol(buf: memoryview) -> ScipSymbol:
    symbol = display_name = enclosing_symbol = ""
    kind = 0
    relationships: list[ScipRelationship] = []
    for number, _wire, value in _fields(buf):
        match number:
            case 1:
                symbol = _text(value)
            case 4:
                relationships.append(_relationship(_message(value)))
            case 5:
                kind = _int(value)
            case 6:
                display_name = _text(value)
            case 8:
                enclosing_symbol = _text(value)
            case _:
                pass
    return ScipSymbol(
        symbol=symbol,
        kind=kind,
        display_name=display_name,
        relationships=tuple(relationships),
        enclosing_symbol=enclosing_symbol,
    )


def _document(buf: memoryview) -> ScipDocument:
    path = language = ""
    occurrences: list[ScipOccurrence] = []
    symbols: list[ScipSymbol] = []
    for number, _wire, value in _fields(buf):
        match number:
            case 1:
                path = _text(value)
            case 2:
                if (occurrence := _occurrence(_message(value))) is not None:
                    occurrences.append(occurrence)
            case 3:
                symbols.append(_symbol(_message(value)))
            case 4:
                language = _text(value)
            case _:
                pass
    return ScipDocument(
        path=path,
        language=language,
        occurrences=tuple(occurrences),
        symbols=tuple(symbols),
    )


def _metadata(buf: memoryview) -> tuple[str, str, str]:
    tool = version = root = ""
    for number, _wire, value in _fields(buf):
        if number == 2:
            for tool_number, _tool_wire, tool_value in _fields(_message(value)):
                if tool_number == 1:
                    tool = _text(tool_value)
                elif tool_number == 2:
                    version = _text(tool_value)
        elif number == 3:
            root = _text(value)
    return tool, version, root


def decode_index(data: bytes) -> ScipIndex:
    buf = memoryview(data)
    tool = version = root = ""
    documents: list[ScipDocument] = []
    external: list[ScipSymbol] = []
    for number, _wire, value in _fields(buf):
        match number:
            case 1:
                tool, version, root = _metadata(_message(value))
            case 2:
                documents.append(_document(_message(value)))
            case 3:
                external.append(_symbol(_message(value)))
            case _:
                pass
    return ScipIndex(
        tool=tool,
        version=version,
        project_root=root,
        documents=tuple(documents),
        external_symbols=tuple(external),
    )


def read_index(path: Path) -> ScipIndex:
    return decode_index(path.read_bytes())
