# L3 finding from the evals/ harness: PythonAstAnalyzerMixin._traverse_single_pass
# calls self._infer_instance_variable_types_from_assignments(...), a method defined
# on the sibling PythonVariableAnalyzerMixin. Neither is the other's base; both are
# combined into the concrete PythonTypeInferenceEngine. A same-named stub in another
# class makes the bare-name trie fallback ambiguous, so resolution must go through
# the concrete subclass's MRO to land on the real sibling method.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    # A decoy class declaring the same method name (mirrors a TYPE_CHECKING stub)
    # so the trie fallback alone cannot pick the right target.
    "pkg/decoy.py": ("class Deps:\n    def infer_vars(self):\n        return None\n"),
    "pkg/mixin_a.py": (
        "class AMixin:\n    def traverse(self):\n        return self.infer_vars()\n"
    ),
    "pkg/mixin_b.py": ("class BMixin:\n    def infer_vars(self):\n        return {}\n"),
    "pkg/engine.py": (
        "from .mixin_a import AMixin\n"
        "from .mixin_b import BMixin\n\n\n"
        "class Engine(AMixin, BMixin):\n"
        "    def other(self):\n"
        "        return None\n"
    ),
}


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
    for rel, content in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
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


class TestSiblingMixinResolution:
    def test_self_call_resolves_to_sibling_mixin_method(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.mixin_a.AMixin.traverse",
            "proj.pkg.mixin_b.BMixin.infer_vars",
        ) in calls, calls

    def test_does_not_resolve_to_decoy_class(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.mixin_a.AMixin.traverse",
            "proj.pkg.decoy.Deps.infer_vars",
        ) not in calls, calls
