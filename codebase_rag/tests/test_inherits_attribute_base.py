# (H) L2 finding from the evals/ harness: cgr captured INHERITS for direct-name
# (H) bases (class C(Base)) but dropped attribute-style bases (class C(mod.Base),
# (H) e.g. class UniXcoder(nn.Module)). Those inheritance edges must be captured.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "inhproj"

MODULE_SRC = """from collections import abc


class C(abc.Mapping):
    pass
"""

_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


class _Capture:
    def __init__(self) -> None:
        self.nodes: dict[tuple[str, PropertyValue], PropertyDict] = {}
        self.rels: list[_RelTuple] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        uid = properties[cs.NODE_UNIQUE_CONSTRAINTS[label]]
        self.nodes[(str(label), uid)] = dict(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append(
            (
                str(from_spec[0]),
                from_spec[2],
                str(rel_type),
                str(to_spec[0]),
                to_spec[2],
            )
        )

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _build(tmp_path: Path) -> _Capture:
    (tmp_path / "m.py").write_text(MODULE_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return cap


class TestInheritsAttributeBase:
    def test_attribute_base_class_creates_inherits_edge(self, tmp_path: Path) -> None:
        cap = _build(tmp_path)
        targets = [
            str(target).rsplit(cs.SEPARATOR_DOT, 1)[-1]
            for (_fl, from_val, rel_type, _tl, target) in cap.rels
            if rel_type == cs.RelationshipType.INHERITS and str(from_val).endswith(".C")
        ]
        assert targets == ["Mapping"], targets
