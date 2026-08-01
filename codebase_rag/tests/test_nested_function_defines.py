# Finding #2 from the evals/ harness: a function nested inside a METHOD was
# attributed to the Module via DEFINES (flattened), producing false-positive
# module-level edges. A nested function must be DEFINES'd by its enclosing
# scope: the method for function-in-method, the function for function-in-function.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "nestproj"

MODULE_SRC = """class C:
    def find_x(self) -> int:
        def dfs(n: int) -> int:
            return n

        return dfs(1)


def outer() -> int:
    def inner() -> int:
        return 1

    return inner()
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


def _build(tmp_path: Path, src: str = MODULE_SRC) -> _Capture:
    (tmp_path / "m.py").write_text(src)
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


def _defines_sources(cap: _Capture, target_suffix: str) -> list[tuple[str, str]]:
    return [
        (from_label, str(from_val))
        for (from_label, from_val, rel_type, _tl, target) in cap.rels
        if rel_type == cs.RelationshipType.DEFINES
        and str(target).endswith(target_suffix)
    ]


class TestNestedFunctionDefines:
    def test_function_in_method_defined_by_method(self, tmp_path: Path) -> None:
        cap = _build(tmp_path)
        sources = _defines_sources(cap, ".find_x.dfs")
        assert len(sources) == 1, sources
        label, qn = sources[0]
        assert label == cs.NodeLabel.METHOD, sources
        assert qn.endswith(".C.find_x"), sources

    def test_function_in_function_defined_by_function(self, tmp_path: Path) -> None:
        cap = _build(tmp_path)
        sources = _defines_sources(cap, ".outer.inner")
        assert len(sources) == 1, sources
        label, qn = sources[0]
        assert label == cs.NodeLabel.FUNCTION, sources
        assert qn.endswith(".outer"), sources


CLASS_IN_METHOD_SRC = """class Holder:
    def make(self) -> object:
        class Local:
            pass

        return Local()
"""


class TestNestedClassDefines:
    def test_class_in_method_defined_by_method(self, tmp_path: Path) -> None:
        cap = _build(tmp_path, CLASS_IN_METHOD_SRC)
        sources = _defines_sources(cap, ".make.Local")
        assert len(sources) == 1, sources
        label, qn = sources[0]
        assert label == cs.NodeLabel.METHOD, sources
        assert qn.endswith(".Holder.make"), sources
