"""Machine-readable view of the declared node schemas (issue #386).

`NODE_SCHEMAS` in `types_defs` declares each `NodeLabel`'s properties as a
prose string -- `"{qualified_name: string, name: string, path: string?}"` --
consumed by README generation and LLM prompt text. Nothing parses it, so
nothing can check anything against it or generate anything from it.

That is the blocker for an embedded graph backend. Kuzu requires an explicit
table per label and rejects any property not declared in it, so a Kuzu
`CREATE NODE TABLE` cannot be written without a parsed schema. It is also why
`Pattern`, `CodeSmell` and `SecurityIssue` could drift out of
`codec/schema.proto` unnoticed (#1452): the declaration existed but was not in
a form anything could compare.

This parses the existing strings rather than replacing them. The prose is the
source of truth and stays where it is -- a second declaration to keep in sync
is exactly the drift this is meant to make detectable.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .types_defs import NODE_SCHEMAS, NodeLabel

# `{a: string, b: int?}` -- the whole body between the braces.
_BODY = re.compile(r"^\{(.*)\}$", re.S)

# A trailing `?` marks the property optional; everything else is the type.
_OPTIONAL_SUFFIX = "?"

# The closed vocabulary the declarations actually use, measured across all 21
# labels. A type outside this set is a typo or a new kind that needs a
# deliberate mapping decision, so it raises rather than passing through.
_LIST_PREFIX = "list["
_LIST_SUFFIX = "]"
_SCALARS = frozenset({"string", "int", "boolean"})


class PropertySpec(NamedTuple):
    """One declared property: its name, base type, and whether it is optional.

    `element` is the inner type for a list (`list[string]` -> `string`) and
    None otherwise, so a consumer can map a list without re-parsing the type.
    """

    name: str
    type_name: str
    optional: bool
    element: str | None


class SchemaParseError(ValueError):
    """A declaration that does not match the grammar.

    Raised rather than skipped: a property this cannot read is one a generated
    table would silently omit, and a backend that rejects undeclared
    properties would then fail at write time with no indication why.
    """


def _parse_type(raw: str) -> tuple[str, bool, str | None]:
    text = raw.strip()
    optional = text.endswith(_OPTIONAL_SUFFIX)
    if optional:
        text = text[: -len(_OPTIONAL_SUFFIX)].strip()
    if text.startswith(_LIST_PREFIX) and text.endswith(_LIST_SUFFIX):
        element = text[len(_LIST_PREFIX) : -len(_LIST_SUFFIX)].strip()
        if element not in _SCALARS:
            raise SchemaParseError(f"unknown list element type: {element!r}")
        return text, optional, element
    if text not in _SCALARS:
        raise SchemaParseError(f"unknown property type: {text!r}")
    return text, optional, None


def parse_properties(declaration: str) -> list[PropertySpec]:
    """The properties of one declaration string, in declared order.

    Order is preserved because it is meaningful: the primary key is declared
    first by convention, and a generated table should list columns the way the
    schema does rather than in whatever order a dict happened to yield.
    """
    body_match = _BODY.match(declaration.strip())
    if body_match is None:
        raise SchemaParseError(f"declaration is not brace-delimited: {declaration!r}")
    body = body_match.group(1).strip()
    if not body:
        return []
    specs: list[PropertySpec] = []
    for field in body.split(","):
        name, separator, raw_type = field.partition(":")
        if not separator:
            raise SchemaParseError(f"property has no type: {field.strip()!r}")
        type_name, optional, element = _parse_type(raw_type)
        specs.append(
            PropertySpec(
                name=name.strip(),
                type_name=type_name,
                optional=optional,
                element=element,
            )
        )
    return specs


def parsed_node_schemas() -> dict[NodeLabel, list[PropertySpec]]:
    """Every declared node schema, parsed.

    Raises on the first unreadable declaration rather than returning a partial
    map: a caller generating tables from a partial schema would produce a
    backend that silently drops whatever could not be read.
    """
    return {
        schema.label: parse_properties(schema.properties) for schema in NODE_SCHEMAS
    }
