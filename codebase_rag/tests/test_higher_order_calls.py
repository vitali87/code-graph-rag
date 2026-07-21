# L3 finding from the evals/ harness: a function passed as an argument and
# invoked via a parameter name (extract_decorators_func(node) inside
# ingest_method) or handed to an eager higher-order builtin (sorted(...,
# key=_span_key)). The traced CALLS edge points from the function that
# actually invokes the callable: the callee for a parameter it calls, the
# enclosing function for a synchronous builtin. Sibling-class methods of the
# same name make the callback targets ambiguous so trie uniqueness cannot
# accidentally mask a real miss.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

MODULE_SRC = """def helper(node):
    return node


def keyfn(n):
    return n.start


def apply_cb(cb, value):
    return cb(value)


def driver(items):
    return apply_cb(helper, items)


def do_sort(items):
    return sorted(items, key=keyfn)


class Other:
    def helper(self) -> int:
        return 1

    def keyfn(self) -> int:
        return 2
"""


class _Capture:
    def __init__(self) -> None:
        self.rels: list[tuple[PropertyValue, str, PropertyValue]] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append((from_spec[2], str(rel_type), to_spec[2]))

    def flush_all(self) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        return None


def _calls(tmp_path: Path) -> set[tuple[PropertyValue, PropertyValue]]:
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
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestHigherOrderCalls:
    def test_callable_parameter_resolves_to_argument_at_call_site(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.apply_cb", "proj.m.helper") in calls, calls

    def test_callback_attributed_to_invoking_callee_not_caller(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        # driver passes helper but never invokes it; apply_cb does.
        assert ("proj.m.driver", "proj.m.helper") not in calls, calls

    def test_callable_parameter_prefers_module_function_over_sibling_method(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.apply_cb", "proj.m.Other.helper") not in calls, calls

    def test_sorted_key_attributed_to_enclosing_function(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.do_sort", "proj.m.keyfn") in calls, calls

    def test_normal_call_edge_to_callee_still_present(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert ("proj.m.driver", "proj.m.apply_cb") in calls, calls
