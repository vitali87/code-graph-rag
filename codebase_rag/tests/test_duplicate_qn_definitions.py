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


CLASS_SRC = """import os


if os.environ.get("FLAG"):

    class Widget:
        def render(self) -> str:
            return "real"

else:

    class Widget:
        def render(self) -> str:
            return "stub"
"""


class TestDuplicateQualifiedNameClasses:
    def test_both_branch_classes_become_distinct_nodes(self, tmp_path: Path) -> None:
        cap = _build(tmp_path, CLASS_SRC)
        widget_start_lines = sorted(
            int(props[cs.KEY_START_LINE])
            for (label, _uid), props in cap.nodes.items()
            if label == cs.NodeLabel.CLASS
            and props.get(cs.KEY_NAME) == "Widget"
            and props.get(cs.KEY_START_LINE) is not None
        )
        assert widget_start_lines == [6, 12], widget_start_lines

    def test_methods_of_both_branch_classes_survive(self, tmp_path: Path) -> None:
        cap = _build(tmp_path, CLASS_SRC)
        render_start_lines = sorted(
            int(props[cs.KEY_START_LINE])
            for (label, _uid), props in cap.nodes.items()
            if label == cs.NodeLabel.METHOD
            and props.get(cs.KEY_NAME) == "render"
            and props.get(cs.KEY_START_LINE) is not None
        )
        assert render_start_lines == [7, 13], render_start_lines


METHOD_DUP_SRC = """import os


class Service:

    if os.environ.get("FLAG"):

        def run(self) -> str:
            return "real"

    else:

        def run(self) -> str:
            return "stub"
"""


class TestDuplicateQualifiedNameMethodsInOneClass:
    def test_both_branch_methods_in_one_class_survive(self, tmp_path: Path) -> None:
        cap = _build(tmp_path, METHOD_DUP_SRC)
        run_start_lines = sorted(
            int(props[cs.KEY_START_LINE])
            for (label, _uid), props in cap.nodes.items()
            if label == cs.NodeLabel.METHOD
            and props.get(cs.KEY_NAME) == "run"
            and props.get(cs.KEY_START_LINE) is not None
        )
        assert run_start_lines == [8, 13], run_start_lines


FACTORY_SRC = """def factory(value):
    if callable(value):

        def handler(arg):
            return value()

    else:

        def handler(arg):
            return value

    return handler
"""


class TestDuplicateQualifiedNameReturnedClosures:
    def test_returned_closure_links_both_duplicate_definitions(
        self, tmp_path: Path
    ) -> None:
        # (H) `return handler` hands back whichever branch defined handler, so the
        # (H) producer edge must link BOTH twins; linking one leaves the other
        # (H) unreachable and falsely dead (django's SET.set_on_delete@60).
        cap = _build(tmp_path, FACTORY_SRC)
        returned_edges = sorted(
            str(target)
            for (_fl, from_val, rel_type, _tl, target) in cap.rels
            if rel_type == cs.RelationshipType.CALLS
            and str(from_val).endswith(".factory")
            and ".handler" in str(target)
        )
        assert returned_edges == [
            f"{PROJECT}.m.factory.handler",
            f"{PROJECT}.m.factory.handler{cs.DUP_QN_MARKER}9",
        ], returned_edges
