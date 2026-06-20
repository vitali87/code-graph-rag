# (H) L3 finding from the evals/ harness: a mixin declares an @abstractmethod stub
# (H) for a method a sibling mixin implements; self.method() dispatches to the
# (H) concrete sibling at runtime. cgr's ambiguous-name tiebreak preferred the
# (H) same-module abstract stub by import distance. A concrete implementation must
# (H) win over an abstract stub of the same name.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "pkg"

READER_SRC = """from abc import abstractmethod


class ReaderMixin:
    @abstractmethod
    def parse(self) -> str: ...

    def read(self) -> str:
        return self.parse()
"""

PARSER_SRC = """class ParserMixin:
    def parse(self) -> str:
        return "parsed"
"""

ENGINE_SRC = """from pkg.reader import ReaderMixin
from pkg.parser import ParserMixin


class Engine(ReaderMixin, ParserMixin):
    pass
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
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "reader.py").write_text(READER_SRC)
    (pkg / "parser.py").write_text(PARSER_SRC)
    (pkg / "engine.py").write_text(ENGINE_SRC)
    parsers, queries = load_parsers()
    cap = _Capture()
    GraphUpdater(
        ingestor=cap,
        repo_path=pkg,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.CALLS
    }


class TestAbstractMethodOverrideResolution:
    def test_self_call_resolves_to_concrete_sibling_not_abstract_stub(
        self, tmp_path: Path
    ) -> None:
        calls = _calls(tmp_path)
        assert (
            "pkg.reader.ReaderMixin.read",
            "pkg.parser.ParserMixin.parse",
        ) in calls, calls

    def test_abstract_stub_is_not_the_call_target(self, tmp_path: Path) -> None:
        calls = _calls(tmp_path)
        assert (
            "pkg.reader.ReaderMixin.read",
            "pkg.reader.ReaderMixin.parse",
        ) not in calls, calls
