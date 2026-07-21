# L3 finding from the evals/ harness: `if self.function_registry:` tests an object
# for truthiness, which calls __bool__ if defined else __len__. cgr extracted only
# explicit calls, missing these dunder edges when the operand is a first-party object.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/sized.py": ("class Sized:\n    def __len__(self):\n        return 0\n"),
    "pkg/flag.py": (
        "class Flag:\n"
        "    def __bool__(self):\n        return True\n\n"
        "    def __len__(self):\n        return 0\n"
    ),
    "pkg/user.py": (
        "from .sized import Sized\n"
        "from .flag import Flag\n\n\n"
        "class User:\n"
        "    def __init__(self, sized: Sized, flag: Flag) -> None:\n"
        "        self._sized = sized\n"
        "        self._flag = flag\n\n"
        "    def _record(self):\n"
        "        return None\n\n"
        "    def check(self):\n"
        "        self._record()\n"
        "        if self._sized:\n"
        "            return 1\n"
        "        return 0\n\n"
        "    def combined(self, other):\n"
        "        self._record()\n"
        "        if self._sized and other:\n"
        "            return 1\n"
        "        return 0\n\n"
        "    def truthy_flag(self):\n"
        "        self._record()\n"
        "        if self._flag:\n"
        "            return 1\n"
        "        return 0\n"
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


class TestTruthinessDispatchResolution:
    def test_if_truthiness_dispatches_to_len(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.check",
            "proj.pkg.sized.Sized.__len__",
        ) in calls, calls

    def test_boolean_operator_operand_dispatches_to_len(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.combined",
            "proj.pkg.sized.Sized.__len__",
        ) in calls, calls

    def test_bool_takes_precedence_over_len(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.truthy_flag",
            "proj.pkg.flag.Flag.__bool__",
        ) in calls, calls
        assert (
            "proj.pkg.user.User.truthy_flag",
            "proj.pkg.flag.Flag.__len__",
        ) not in calls, calls
