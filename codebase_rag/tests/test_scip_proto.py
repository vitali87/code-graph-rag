"""The SCIP reader decodes the fields the call oracle uses, and only those.

The index is built here byte by byte from the protobuf wire format, so the
test pins the field numbers of sourcegraph/scip's scip.proto independently of
any generated code: if the reader used a wrong number, the decoded index
would come back empty or mislabelled.
"""

from __future__ import annotations

import pytest

from evals.scip.proto import ScipDecodeError, decode_index
from evals.scip.symbols import (
    is_method_symbol,
    is_type_symbol,
    member_symbol,
    parse_symbol,
)
from evals.scip.types_defs import Descriptor, ScipRange


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(number: int, wire: int) -> bytes:
    return _varint(number << 3 | wire)


def _bytes(number: int, payload: bytes) -> bytes:
    return _key(number, 2) + _varint(len(payload)) + payload


def _str(number: int, text: str) -> bytes:
    return _bytes(number, text.encode())


def _int(number: int, value: int) -> bytes:
    return _key(number, 0) + _varint(value)


def _packed(number: int, values: list[int]) -> bytes:
    return _bytes(number, b"".join(_varint(v) for v in values))


def _index() -> bytes:
    method = "scip-python python pkg 0 `pkg.mod`/f()."
    base = "scip-python python pkg 0 `pkg.mod`/Base#run()."
    definition = _packed(1, [3, 4, 5]) + _str(2, method) + _int(3, 1)
    definition += _packed(7, [3, 0, 6, 10])
    # An unpacked range, and an unknown field (99) the reader must skip.
    reference = b"".join(_int(1, v) for v in (8, 2, 8, 3)) + _str(2, method)
    reference += _str(99, "ignored")
    relationship = _str(1, base) + _int(3, 1)
    info = _str(1, method) + _bytes(4, relationship) + _int(5, 17) + _str(6, "f")
    document = _str(1, "pkg/mod.py") + _str(4, "python")
    document += _bytes(2, definition) + _bytes(2, reference) + _bytes(3, info)
    tool = _str(1, "scip-python") + _str(2, "0.6.6")
    metadata = _bytes(2, tool) + _str(3, "file:///repo")
    return _bytes(1, metadata) + _bytes(2, document)


def test_decodes_documents_occurrences_and_symbols() -> None:
    index = decode_index(_index())

    assert (index.tool, index.version, index.project_root) == (
        "scip-python",
        "0.6.6",
        "file:///repo",
    )
    [document] = index.documents
    assert (document.path, document.language) == ("pkg/mod.py", "python")
    definition, reference = document.occurrences
    # A three-element range sits on one line.
    assert definition.range == ScipRange(3, 4, 3, 5)
    assert definition.enclosing == ScipRange(3, 0, 6, 10)
    assert definition.roles == 1
    assert reference.range == ScipRange(8, 2, 8, 3)
    assert reference.enclosing is None
    [info] = document.symbols
    assert (info.kind, info.display_name) == (17, "f")
    [relationship] = info.relationships
    assert relationship.is_implementation and not relationship.is_reference


def test_truncated_input_raises() -> None:
    data = _index()
    with pytest.raises(ScipDecodeError):
        decode_index(data[:-3])


def test_parses_global_symbols() -> None:
    parsed = parse_symbol("scip-typescript npm . . `main.ts`/Square#`<constructor>`().")

    assert parsed is not None
    assert parsed.scheme == "scip-typescript"
    assert parsed.descriptors == (
        Descriptor("main.ts", "/", ""),
        Descriptor("Square", "#", ""),
        Descriptor("<constructor>", "().", ""),
    )


def test_parses_disambiguators_parameters_and_escaped_spaces() -> None:
    parsed = parse_symbol("scip-java maven g  a 1 pkg/T#m(+1).(x)[U]")

    assert parsed is not None
    assert parsed.manager == "maven"
    # A doubled space is one literal space inside a header field.
    assert parsed.package == "g a"
    assert parsed.descriptors == (
        Descriptor("pkg", "/", ""),
        Descriptor("T", "#", ""),
        Descriptor("m", "().", "+1"),
        Descriptor("x", "()", ""),
        Descriptor("U", "[]", ""),
    )


def test_local_and_malformed_symbols() -> None:
    local = parse_symbol("local 7")
    assert local is not None and local.local == "7"
    assert parse_symbol("scip-python python pkg 0 `unterminated") is None
    assert parse_symbol("too few fields") is None


def test_symbol_kind_predicates() -> None:
    cls = "scip-python python pkg 0 `pkg.mod`/Dog#"
    assert is_type_symbol(cls) and not is_method_symbol(cls)
    assert is_method_symbol(member_symbol(cls, "__init__"))
    assert not is_method_symbol("local 3")
