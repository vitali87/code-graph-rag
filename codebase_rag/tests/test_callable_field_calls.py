# (H) L3 finding from the evals/ harness: fqn_config.get_name(node) invokes a
# (H) function stored in a NamedTuple Callable field (FQNSpec), where fqn_config
# (H) comes from LANGUAGE_FQN_SPECS.get(language). Every function bound to that
# (H) field at a construction site is a possible callee, so resolving to all of
# (H) them is a sound call graph and captures the traced (Python) edge.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

# (H) fetch_name is a callable field of exactly one NamedTuple, mirroring how
# (H) get_name is unique to FQNSpec, so it resolves without a receiver type.
MODULE_SRC = """from typing import Callable, NamedTuple


def py_name() -> str:
    return "py"


def js_name() -> str:
    return "js"


class Spec(NamedTuple):
    fetch_name: Callable[[], str]


PY_SPEC = Spec(fetch_name=py_name)
JS_SPEC = Spec(fetch_name=js_name)

SPECS = {"py": PY_SPEC, "js": JS_SPEC}


def use(lang: str) -> str:
    spec = SPECS.get(lang)
    return spec.fetch_name()
"""

# (H) Two classes share the field name, so with no receiver type the targets are
# (H) ambiguous and must NOT be emitted (precision guard).
AMBIGUOUS_SRC = """from typing import Callable, NamedTuple


def a_name() -> str:
    return "a"


def b_name() -> str:
    return "b"


class SpecA(NamedTuple):
    shared_cb: Callable[[], str]


class SpecB(NamedTuple):
    shared_cb: Callable[[], str]


A = SpecA(shared_cb=a_name)
B = SpecB(shared_cb=b_name)


def run(flag: bool):
    chosen = A if flag else B
    return chosen.shared_cb()
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


def _calls(tmp_path: Path, src: str) -> set[tuple[PropertyValue, PropertyValue]]:
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
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestCallableFieldCalls:
    def test_resolves_to_first_bound_function(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path, MODULE_SRC)
        assert ("proj.m.use", "proj.m.py_name") in calls, calls

    def test_resolves_to_all_bound_functions(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path, MODULE_SRC)
        assert ("proj.m.use", "proj.m.js_name") in calls, calls

    def test_ambiguous_field_name_not_resolved(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path, AMBIGUOUS_SRC)
        assert ("proj.m.run", "proj.m.a_name") not in calls, calls
        assert ("proj.m.run", "proj.m.b_name") not in calls, calls
