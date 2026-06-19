# (H) Regression tests for the duplicate-qualified-name finding surfaced by the
# (H) evals/ harness: the `if has_x(): <real impl> else: <stub>` import-fallback
# (H) idiom defines one qualified name twice. cgr used to collapse the two into a
# (H) single node (last-writer-wins kept the else-branch stub). Both definitions
# (H) must survive as distinct nodes, and a call must link to BOTH.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "dupproj"

MODULE_SRC = """import os


if os.environ.get("FLAG"):

    def impl() -> str:
        return "real"

else:

    def impl() -> str:
        return "stub"


def caller() -> str:
    return impl()
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


class TestDuplicateQualifiedNameDefinitions:
    def test_both_branch_definitions_become_distinct_nodes(
        self, tmp_path: Path
    ) -> None:
        cap = _build(tmp_path)
        impl_start_lines = sorted(
            int(props[cs.KEY_START_LINE])
            for (label, _uid), props in cap.nodes.items()
            if label == cs.NodeLabel.FUNCTION
            and props.get(cs.KEY_NAME) == "impl"
            and props.get(cs.KEY_START_LINE) is not None
        )
        assert impl_start_lines == [6, 11], impl_start_lines

    def test_call_links_to_both_duplicate_definitions(self, tmp_path: Path) -> None:
        cap = _build(tmp_path)
        calls_to_impl = [
            target
            for (_fl, from_val, rel_type, _tl, target) in cap.rels
            if rel_type == cs.RelationshipType.CALLS
            and str(from_val).endswith(".caller")
            and ".impl" in str(target)
        ]
        assert len(calls_to_impl) == 2, calls_to_impl
