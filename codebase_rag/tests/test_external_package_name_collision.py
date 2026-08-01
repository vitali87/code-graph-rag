# L2 residual from the evals/ harness: when cgr is pointed at a directory that
# is itself a package (has __init__.py), a bare absolute import like
# `from mcp.server import X` is the EXTERNAL top-level package, not the internal
# sibling subpackage `<project>.mcp` (which is reachable only as that dotted name
# or relatively). cgr used to mis-resolve it to the internal package.
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


def _build(tmp_path: Path, importer: str, src: str) -> _Capture:
    (tmp_path / "__init__.py").touch()
    mcp = tmp_path / "mcp"
    mcp.mkdir()
    mcp.joinpath("__init__.py").touch()
    mcp.joinpath("server.py").write_text("Thing = 1\n")
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
    return cap


def _imports(cap: _Capture) -> set[tuple[PropertyValue, PropertyValue]]:
    return {
        (frm, to) for (frm, rel, to) in cap.rels if rel == cs.RelationshipType.IMPORTS
    }


class TestExternalPackageNameCollision:
    def test_bare_absolute_import_is_external_not_internal(
        self, tmp_path: Path
    ) -> None:
        cap = _build(
            tmp_path, "client.py", "from mcp.server import Thing\n\nx = Thing\n"
        )
        edges = _imports(cap)
        assert ("proj.client", "proj.mcp.server") not in edges, edges
        assert ("proj.client", "proj.mcp") not in edges, edges

    def test_relative_import_to_subpackage_still_internal(self, tmp_path: Path) -> None:
        cap = _build(
            tmp_path, "client.py", "from .mcp.server import Thing\n\nx = Thing\n"
        )
        edges = _imports(cap)
        assert ("proj.client", "proj.mcp.server") in edges, edges
