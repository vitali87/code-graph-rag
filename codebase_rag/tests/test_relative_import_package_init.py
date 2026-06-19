# (H) L2 residual from the evals/ harness: relative imports inside an __init__.py
# (H) resolved one level too high. A package's qualified name IS the package, so
# (H) `from . import sub` in pkg/__init__.py must target pkg.sub, not the parent.
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


def _import_edges(
    tmp_path: Path,
) -> set[tuple[PropertyValue, PropertyValue]]:
    (tmp_path / "__init__.py").touch()
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    pkg.joinpath("__init__.py").write_text("from . import sub\n\nuse = sub\n")
    pkg.joinpath("sub.py").write_text("X = 1\n")
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


class TestRelativeImportPackageInit:
    def test_from_dot_import_in_package_init_targets_own_submodule(
        self, tmp_path: Path
    ) -> None:
        edges = _import_edges(tmp_path)
        assert ("proj.pkg", "proj.pkg.sub") in edges, edges
        assert ("proj.pkg", "proj.sub") not in edges, edges
