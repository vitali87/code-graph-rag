# (H) L3 finding from the evals/ harness: Python operator syntax dispatches to dunder
# (H) methods at runtime: `k in reg` -> reg.__contains__, `reg[k]` -> reg.__getitem__,
# (H) `reg[k] = v` -> reg.__setitem__, `len(reg)` -> reg.__len__. cgr only extracts
# (H) call expressions, so these first-party method calls were never captured. They are
# (H) emitted only when the operand's type resolves to a first-party class that defines
# (H) the dunder, so builtin containers (dict/list) produce no spurious edges.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/registry.py": (
        "class Registry:\n"
        "    def __contains__(self, key):\n        return True\n\n"
        "    def __getitem__(self, key):\n        return 1\n\n"
        "    def __setitem__(self, key, value):\n        return None\n\n"
        "    def __len__(self):\n        return 0\n"
    ),
    "pkg/user.py": (
        "from .registry import Registry\n\n\n"
        "class User:\n"
        "    def __init__(self, reg: Registry) -> None:\n"
        "        self._reg = reg\n\n"
        "    def use(self, key):\n"
        "        if key in self._reg:\n"
        "            value = self._reg[key]\n"
        "        self._reg[key] = 1\n"
        "        return len(self._reg)\n\n"
        "    def builtin(self):\n"
        "        data = {}\n"
        "        data['x'] = 1\n"
        "        return data['x']\n"
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


class TestOperatorDispatchResolution:
    def test_contains_operator_dispatches_to_dunder(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.registry.Registry.__contains__",
        ) in calls, calls

    def test_subscript_read_dispatches_to_getitem(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.registry.Registry.__getitem__",
        ) in calls, calls

    def test_subscript_write_dispatches_to_setitem(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.registry.Registry.__setitem__",
        ) in calls, calls

    def test_len_dispatches_to_dunder(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.registry.Registry.__len__",
        ) in calls, calls

    def test_builtin_container_produces_no_dunder_edge(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        dunder_targets = {
            to for (frm, to) in calls if frm == "proj.pkg.user.User.builtin"
        }
        assert dunder_targets == set(), dunder_targets
