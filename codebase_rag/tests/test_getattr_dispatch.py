# L3 finding from the evals/ harness: JavaTypeResolverMixin._find_registry_entries_under
# does `finder = getattr(self.function_registry, cs.METHOD_FIND_WITH_PREFIX, None)` then
# calls finder(...). The call dispatches to FunctionRegistryTrie.find_with_prefix at
# runtime. Resolving it needs getattr(recv, name) modelled as recv.<name>, where the
# name argument is a string literal or a module constant resolved to its string value.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/names.py": 'METHOD_DO = "do"\n',
    "pkg/helper.py": (
        "class Helper:\n    def do(self, value):\n        return value\n"
    ),
    "pkg/worker.py": (
        "from . import names\n"
        "from .helper import Helper\n\n\n"
        "class Worker:\n"
        "    def __init__(self) -> None:\n"
        "        self._helper = Helper()\n\n"
        "    def via_constant(self, value):\n"
        "        fn = getattr(self._helper, names.METHOD_DO, None)\n"
        "        if callable(fn):\n"
        "            return fn(value)\n"
        "        return None\n\n"
        "    def via_literal(self, value):\n"
        '        fn = getattr(self._helper, "do", None)\n'
        "        return fn(value)\n"
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


class TestGetattrDispatch:
    def test_getattr_with_constant_name_resolves(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.worker.Worker.via_constant",
            "proj.pkg.helper.Helper.do",
        ) in calls, calls

    def test_getattr_with_string_literal_resolves(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.worker.Worker.via_literal",
            "proj.pkg.helper.Helper.do",
        ) in calls, calls
