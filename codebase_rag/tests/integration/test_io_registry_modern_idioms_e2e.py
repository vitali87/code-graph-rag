from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

# (H) issue #723: modern-idiom I/O (httpx / aiohttp network, pathlib.Path file)
# (H) must produce READS_FROM / WRITES_TO edges, mirroring requests / open.


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "io_project"
    project.mkdir()
    (project / "io_mod.py").write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _io_edges(ingestor: MemgraphIngestor) -> set[tuple[str, str]]:
    rows = ingestor.fetch_all(
        f"MATCH ()-[r:{cs.RelationshipType.READS_FROM.value}|"
        f"{cs.RelationshipType.WRITES_TO.value}]->(res:{cs.NodeLabel.RESOURCE.value}) "
        "RETURN type(r) AS rel, res.qualified_name AS qn"
    )
    return {(str(row["rel"]), str(row["qn"])) for row in rows}


_READS = cs.RelationshipType.READS_FROM.value
_WRITES = cs.RelationshipType.WRITES_TO.value


def test_httpx_module_level_get_and_post(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "import httpx\n\n\n"
        "def fetch():\n"
        "    return httpx.get('https://api.example.com/data')\n\n\n"
        "def send(payload):\n"
        "    httpx.post('https://api.example.com/upload')\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::NETWORK::https://api.example.com/upload") in edges


def test_httpx_client_instance_methods(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "import httpx\n\n\n"
        "class Api:\n"
        "    def __init__(self):\n"
        "        self._http = httpx.Client(timeout=30.0)\n\n"
        "    def pull(self):\n"
        "        return self._http.get('x')\n\n"
        "    def push(self):\n"
        "        self._http.post('x')\n",
    )
    edges = _io_edges(memgraph_ingestor)
    kinds = {(rel, qn.split("::")[1]) for rel, qn in edges}
    assert (_READS, "NETWORK") in kinds
    assert (_WRITES, "NETWORK") in kinds


def test_pathlib_read_and_write(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "from pathlib import Path\n\n\n"
        "def save(data):\n"
        "    p = Path('out.txt')\n"
        "    p.write_text(data)\n\n\n"
        "def load():\n"
        "    p = Path('in.txt')\n"
        "    return p.read_text()\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_READS, "resource::FILE::in.txt") in edges


def test_pathlib_directory_listing_and_touch(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "from pathlib import Path\n\n\n"
        "def scan():\n"
        "    d = Path('data')\n"
        "    return list(d.iterdir())\n\n\n"
        "def stamp():\n"
        "    f = Path('flag')\n"
        "    f.touch()\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_READS, "resource::FILE::data") in edges
    assert (_WRITES, "resource::FILE::flag") in edges
