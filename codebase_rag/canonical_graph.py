"""The reference graphs' line format (issue #3629).

A graph export, as `cgr start -o` writes it, becomes sorted text: one node or
relationship per line, keyed by what the graph keys it on, with the
checkout's directory written as `<root>` and the time of the run left out.
Two indexes of the same code give the same lines wherever and whenever they
ran, so a difference between two sets of lines is a difference in the graph.

`scripts/reference_graphs.py` keeps the pinned repositories' graphs in this
form. The Rust rewrite's parity checks write and compare the two engines'
graphs and hand-offs in it too (docs/reports/PHASE1_SEAMS.md), and code under
`codebase_rag` cannot import from `scripts/`, so it lives here.

An export that is not one (no node or relationship list, a node without its
row number, an edge to a node it lacks) raises `SystemExit` naming what is
wrong: the scripts that read one cannot go on without it.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import constants as cs
from . import exceptions as ex
from .types_defs import ExportRecord, JsonValue


def root_spellings(source: Path) -> list[str]:
    """Every way an export may write the directory `source`, longest first,
    so a longer spelling is replaced before a shorter one inside it."""
    # Native and forward-slash spellings of both: on Windows the export
    # writes `C:/cache/x/...` while `str(Path)` gives `C:\cache\x`.
    return sorted(
        {
            spelling
            for path in (source, source.resolve())
            for spelling in (str(path), path.as_posix())
        },
        key=len,
        reverse=True,
    )


def with_root_placeholder(value: JsonValue, roots: Sequence[str]) -> JsonValue:
    """`value` with every path under one of `roots` written from `<root>`,
    with `/`, inside lists and maps too."""
    if isinstance(value, str):
        for root in roots:
            if value == root or value.startswith(
                (f"{root}{cs.SEPARATOR_SLASH}", f"{root}{os.sep}")
            ):
                rest = value[len(root) :].replace(os.sep, cs.SEPARATOR_SLASH)
                return f"{cs.REFERENCE_ROOT_PLACEHOLDER}{rest}"
        return value
    if isinstance(value, list):
        return [with_root_placeholder(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: with_root_placeholder(item, roots) for key, item in value.items()}
    return value


def canonical_json(value: JsonValue) -> str:
    """`value` as a line writes JSON: sorted keys, no ASCII escaping, no
    spaces."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=cs.REFERENCE_JSON_SEPARATORS,
    )


def _properties(record: ExportRecord, roots: Sequence[str]) -> dict[str, JsonValue]:
    properties = record.get(cs.KEY_PROPERTIES) or {}
    if not isinstance(properties, dict):
        raise SystemExit(ex.CANONICAL_NO_PROPERTIES.format(record=record))
    return {
        name: with_root_placeholder(value, roots)
        for name, value in properties.items()
        if name not in cs.REFERENCE_VOLATILE_PROPERTIES
    }


def canonical(export: ExportRecord, source: Path) -> list[str]:
    """The export as sorted lines, the same wherever and whenever it ran.

    A node is named by its labels and the property the graph keys that label
    on (`qualified_name`, `absolute_path`, `name`), not by the export's
    `node_id`, which is only a row number of this run's database. Duplicate
    lines are kept: an edge recorded twice is a difference.
    """
    roots = root_spellings(source)
    sep = cs.REFERENCE_FIELD_SEPARATOR
    names: dict[int, str] = {}
    lines: list[str] = []
    # Both lists must be there, even empty: an export missing one would read
    # as an empty graph and `record` would write it over a full one.
    nodes = export.get(cs.KEY_NODES)
    relationships = export.get(cs.KEY_RELATIONSHIPS)
    if not isinstance(nodes, list) or not isinstance(relationships, list):
        raise SystemExit(ex.CANONICAL_NO_LISTS)
    for node in nodes:
        node_id = node.get(cs.KEY_NODE_ID) if isinstance(node, dict) else None
        if not isinstance(node, dict) or not isinstance(node_id, int):
            raise SystemExit(ex.CANONICAL_NO_NODE_ID.format(node=node))
        raw_labels = node.get(cs.KEY_LABELS) or []
        labels = (
            sorted(str(label) for label in raw_labels)
            if isinstance(raw_labels, list)
            else []
        )
        properties = _properties(node, roots)
        key_name = next(
            (
                cs.NODE_UNIQUE_CONSTRAINTS[label]
                for label in labels
                if label in cs.NODE_UNIQUE_CONSTRAINTS
            ),
            None,
        )
        key = properties.pop(key_name, None) if key_name else None
        label_text = cs.REFERENCE_LABEL_SEPARATOR.join(labels)
        names[node_id] = sep.join((label_text, canonical_json(key)))
        lines.append(
            sep.join(
                (
                    cs.REFERENCE_NODE_TAG,
                    label_text,
                    canonical_json(key),
                    canonical_json(properties),
                )
            )
        )
    for relationship in relationships:
        if not isinstance(relationship, dict):
            raise SystemExit(
                ex.CANONICAL_RELATIONSHIP_NOT_A_RECORD.format(relationship=relationship)
            )
        from_id = relationship.get(cs.KEY_FROM_ID)
        to_id = relationship.get(cs.KEY_TO_ID)
        source_name = names.get(from_id) if isinstance(from_id, int) else None
        target_name = names.get(to_id) if isinstance(to_id, int) else None
        if source_name is None or target_name is None:
            raise SystemExit(
                ex.CANONICAL_UNKNOWN_ENDPOINT.format(relationship=relationship)
            )
        lines.append(
            sep.join(
                (
                    cs.REFERENCE_EDGE_TAG,
                    str(relationship.get(cs.KEY_TYPE)),
                    source_name,
                    target_name,
                    canonical_json(_properties(relationship, roots)),
                )
            )
        )
    return sorted(lines)


def read_graph(path: Path) -> list[str]:
    """The lines of a file `write_graph` wrote."""
    # Split on the newlines `write_graph` writes, not `splitlines()`'s wider
    # set: a docstring may hold U+2028, which JSON leaves unescaped.
    text = path.read_text(encoding=cs.ENCODING_UTF8)
    return [
        line.removesuffix(cs.CARRIAGE_RETURN)
        for line in text.split(cs.LINE_FEED)
        if line
    ]


def write_graph(path: Path, lines: Iterable[str]) -> None:
    """`lines` as UTF-8 text, one per line, for `read_graph` to read back."""
    path.write_text(
        "".join(f"{line}{cs.LINE_FEED}" for line in lines),
        encoding=cs.ENCODING_UTF8,
    )


def difference(expected: Sequence[str], actual: Sequence[str]) -> list[str]:
    """The lines only one side has, `-` for `expected`'s and `+` for `actual`'s.

    Compared as multisets: order does not count, repetition does.
    """
    missing = Counter(expected) - Counter(actual)
    extra = Counter(actual) - Counter(expected)
    removed = [
        cs.REFERENCE_ONLY_EXPECTED.format(line=line)
        for line in sorted(missing.elements())
    ]
    added = [
        cs.REFERENCE_ONLY_ACTUAL.format(line=line) for line in sorted(extra.elements())
    ]
    return removed + added
