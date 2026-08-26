# Machine-readable node schemas (issue #386).
#
# NODE_SCHEMAS declares each label's properties as prose consumed by README
# generation and LLM prompts. Nothing parsed it, so nothing could check it or
# generate from it -- which is why three labels drifted out of
# codec/schema.proto unnoticed (#1452), and why a Kuzu backend (which requires
# an explicit table per label) could not be written.
from __future__ import annotations

import pytest

from codebase_rag.schema_parse import (
    SchemaParseError,
    parse_properties,
    parsed_node_schemas,
)
from codebase_rag.types_defs import NODE_SCHEMAS, NodeLabel


def test_every_declared_schema_parses() -> None:
    """All 21 labels, or the parser is not usable for generating anything.

    A parser that handles most declarations is worse than none for this
    purpose: a backend generated from a partial schema silently drops the
    properties that could not be read, and fails at write time with no
    indication why.
    """
    parsed = parsed_node_schemas()

    assert len(parsed) == len(NODE_SCHEMAS)
    assert set(parsed) == {schema.label for schema in NODE_SCHEMAS}


def test_optionality_is_recovered() -> None:
    specs = parse_properties("{a: string, b: string?}")

    assert [(s.name, s.optional) for s in specs] == [("a", False), ("b", True)]


def test_list_types_expose_their_element() -> None:
    """A consumer must be able to map a list without re-parsing the type."""
    specs = parse_properties("{tags: list[string]?}")

    assert specs[0].type_name == "list[string]"
    assert specs[0].element == "string"
    assert specs[0].optional is True


def test_declared_order_is_preserved() -> None:
    """The primary key is declared first by convention.

    A generated table should list columns the way the schema does rather than
    in whatever order a dict happened to yield.
    """
    specs = parse_properties("{qualified_name: string, name: string, path: string?}")

    assert [s.name for s in specs] == ["qualified_name", "name", "path"]


def test_an_unknown_type_raises_rather_than_passing_through() -> None:
    """A property this cannot read is one a generated table would omit.

    Silently accepting an unrecognised type is the failure this whole module
    exists to prevent: the declaration would look complete while the generated
    backend rejected writes to that column.
    """
    with pytest.raises(SchemaParseError):
        parse_properties("{weird: nonesuch}")


def test_a_malformed_declaration_raises() -> None:
    with pytest.raises(SchemaParseError):
        parse_properties("not braced at all")
    with pytest.raises(SchemaParseError):
        parse_properties("{missing_type}")


def test_a_real_label_round_trips_its_known_properties() -> None:
    """A control against the live declarations, not only synthetic strings.

    Without it the parser could pass every unit case and still mis-read the
    actual schemas it exists to read.
    """
    parsed = parsed_node_schemas()
    module = {spec.name: spec for spec in parsed[NodeLabel.MODULE]}

    assert module["qualified_name"].type_name == "string"
    assert module["qualified_name"].optional is False
    assert "name" in module
