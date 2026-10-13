"""Static value types the compiler tracks, and the schema they come from.

A value's type decides how it travels through SQL: nodes and relationships
as integer ids, typed scalars as native SQL values, and lists, maps and
unknowns as JSON text. Property types come from cgr's own declared schema
(`schema_parse`), so a boolean comes back as `True`, not SQLite's `1`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import cache

from ..constants.embedded_graph import (
    EG_SCHEMA_BOOLEAN,
    EG_SCHEMA_FLOAT,
    EG_SCHEMA_INT,
    EG_SCHEMA_LIST_CLOSE,
    EG_SCHEMA_LIST_OPEN,
    EG_SCHEMA_STRING,
)
from ..constants.embedded_graph import (
    EgKind as Kind,
)
from ..schema_parse import parse_properties, parsed_node_schemas
from ..types_defs import RELATIONSHIP_PROPERTY_SCHEMAS


@dataclass(frozen=True)
class T:
    kind: Kind
    elem: T | None = None

    @property
    def is_entity(self) -> bool:
        return self.kind in (Kind.NODE, Kind.REL)

    @property
    def is_json(self) -> bool:
        return self.kind in (Kind.LIST, Kind.MAP, Kind.ANY)

    @property
    def is_native(self) -> bool:
        return self.kind in (Kind.BOOL, Kind.INT, Kind.FLOAT, Kind.STRING, Kind.NULL)


NODE = T(Kind.NODE)
REL = T(Kind.REL)
MAP = T(Kind.MAP)
BOOL = T(Kind.BOOL)
INT = T(Kind.INT)
FLOAT = T(Kind.FLOAT)
STRING = T(Kind.STRING)
ANY = T(Kind.ANY)
NULL = T(Kind.NULL)


def list_of(elem: T | None) -> T:
    return T(Kind.LIST, elem or ANY)


def unify(types: list[T]) -> T:
    """The one type all of `types` share, NULL aside; ANY when they differ."""
    real = [t for t in types if t.kind != Kind.NULL]
    if not real:
        return NULL
    first = real[0]
    if all(t == first for t in real):
        return first
    if all(t.kind in (Kind.INT, Kind.FLOAT) for t in real):
        return FLOAT
    if all(t.kind == Kind.LIST for t in real):
        return list_of(unify([t.elem or ANY for t in real]))
    return ANY


_DECLARED: dict[str, T] = {
    EG_SCHEMA_STRING: STRING,
    EG_SCHEMA_INT: INT,
    EG_SCHEMA_BOOLEAN: BOOL,
    EG_SCHEMA_FLOAT: FLOAT,
}


def _declared_type(type_name: str) -> T:
    if type_name.startswith(EG_SCHEMA_LIST_OPEN) and type_name.endswith(
        EG_SCHEMA_LIST_CLOSE
    ):
        inner = type_name[len(EG_SCHEMA_LIST_OPEN) : -len(EG_SCHEMA_LIST_CLOSE)]
        return list_of(_DECLARED.get(inner, ANY))
    return _DECLARED.get(type_name, ANY)


@cache
def _node_property_types() -> dict[str, dict[str, T]]:
    return {
        label: {spec.name: _declared_type(spec.type_name) for spec in specs}
        for label, specs in parsed_node_schemas().items()
    }


@cache
def _rel_property_types() -> dict[str, dict[str, T]]:
    out: dict[str, dict[str, T]] = {}
    for schema in RELATIONSHIP_PROPERTY_SCHEMAS:
        props = {
            spec.name: _declared_type(spec.type_name)
            for spec in parse_properties(schema.properties)
        }
        for rel_type in schema.rel_types:
            name = rel_type.value if isinstance(rel_type, Enum) else str(rel_type)
            out.setdefault(name, {}).update(props)
    return out


def _resolve(table: dict[str, dict[str, T]], names: tuple[str, ...], key: str) -> T:
    # A property declared with one type everywhere it is declared keeps that
    # type even when the variable's label is unknown; otherwise its values
    # travel as exact JSON.
    pool = [table[n] for n in names if n in table] if names else list(table.values())
    found = {props[key] for props in pool if key in props}
    if len(found) == 1:
        return found.pop()
    return ANY


def node_property_type(labels: tuple[str, ...], key: str) -> T:
    return _resolve(_node_property_types(), labels, key)


def rel_property_type(types: tuple[str, ...], key: str) -> T:
    return _resolve(_rel_property_types(), types, key)


# A value as SQLite hands it back: the engine never stores blobs.
type SqlValue = int | float | str | None
# A literal's value, as the lexer reads it from the query text.
type LiteralValue = str | int | float | bool | None
