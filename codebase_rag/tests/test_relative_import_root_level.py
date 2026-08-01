# L2 finding from the evals/ harness: `from . import <submodule>` at the
# package root (e.g. cli.py doing `from . import constants as cs`) produced
# no IMPORTS edge, because relative-import resolution dropped the project
# name and computed an empty base module. In a subpackage it worked.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "proj"


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


def _imports(
    tmp_path: Path, importer: str, src: str
) -> set[tuple[PropertyValue, PropertyValue]]:
    (tmp_path / "__init__.py").touch()
    (tmp_path / "constants.py").write_text("X = 1\n")
    (tmp_path / importer).write_text(src)
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
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.IMPORTS
    }


class TestRelativeImportRootLevel:
    def test_from_dot_import_submodule_at_root(self, tmp_path: Path) -> None:
        edges = _imports(
            tmp_path, "cli.py", "from . import constants as cs\n\nuse = cs\n"
        )
        assert ("proj.cli", "proj.constants") in edges, edges
