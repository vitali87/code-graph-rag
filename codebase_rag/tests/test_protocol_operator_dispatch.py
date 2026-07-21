# L3 finding from the evals/ harness: an operator on a Protocol-typed attribute
# (self.ast_cache[k], k in self.ast_cache) must dispatch to the dunder on the
# concrete implementer even when its name does not follow the XxxProtocol convention,
# and even when the dunder (e.g. __len__) is defined only on the implementer, not on
# the stub. Structural conformance (a class defining the Protocol's methods) identifies it.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"

FILES = {
    "pkg/__init__.py": "",
    "pkg/proto.py": (
        "from typing import Protocol\n\n\n"
        "class Cache(Protocol):\n"
        "    def snapshot(self):\n        ...\n\n"
        "    def __getitem__(self, key):\n        ...\n\n"
        "    def __contains__(self, key):\n        ...\n"
    ),
    # MemCache does not match the Cache name convention and adds __len__ (not
    # declared on the Protocol); it conforms via the named method snapshot.
    "pkg/impl.py": (
        "class MemCache:\n"
        "    def snapshot(self):\n        return {}\n\n"
        "    def __getitem__(self, key):\n        return 1\n\n"
        "    def __contains__(self, key):\n        return True\n\n"
        "    def __len__(self):\n        return 0\n"
    ),
    "pkg/user.py": (
        "from .proto import Cache\n\n\n"
        "class User:\n"
        "    def __init__(self, cache: Cache) -> None:\n"
        "        self._cache = cache\n\n"
        "    def _touch(self):\n"
        "        return None\n\n"
        "    def use(self, key):\n"
        "        self._touch()\n"
        "        if key in self._cache:\n"
        "            return self._cache[key]\n"
        "        return len(self._cache)\n"
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


class TestProtocolOperatorDispatch:
    def test_subscript_and_membership_reach_structural_conformer(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.impl.MemCache.__getitem__",
        ) in calls, calls
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.impl.MemCache.__contains__",
        ) in calls, calls

    def test_dunder_only_on_implementer_resolves(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.impl.MemCache.__len__",
        ) in calls, calls

    def test_protocol_stub_not_emitted(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "proj.pkg.user.User.use",
            "proj.pkg.proto.Cache.__getitem__",
        ) not in calls, calls
