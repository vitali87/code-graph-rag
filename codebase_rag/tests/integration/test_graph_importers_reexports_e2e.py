# Real-Memgraph check of `importers_through_reexports` (issue #2573): the
# unit tests drive a fake that answers the hop query in Python, so only a real
# index proves the edges the parsers write for a Python package facade, a
# Rust `pub use` and a TS barrel carry what the walk reads, and that the hop
# query itself runs on the Memgraph the stack ships.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

PROJECT = "reexp"

FILES = {
    # Python: the facade names Client; a sibling imports it for its own use.
    "shop/__init__.py": "from ._client import Client\nfrom ._models import *\n",
    "shop/_client.py": "class Client:\n    pass\n",
    "shop/_models.py": "class Response:\n    pass\n",
    "shop/_api.py": "from ._client import Client\n\n\ndef request():\n"
    "    return Client()\n",
    "app.py": "import shop\n\n\ndef main():\n    return shop.Client()\n",
    "cli.py": "from shop import Client\n",
    "report.py": "from shop import Response\n",
    "direct.py": "from shop._client import Client\nimport shop\n",
    "uses_api.py": "from shop._api import request\n",
    # Rust: `pub use` in the crate root.
    "Cargo.toml": '[package]\nname = "reexp"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub mod frame;\npub mod connection;\npub mod server;\n"
    "pub use crate::frame::Frame;\n",
    "src/frame.rs": "pub struct Frame;\n",
    "src/connection.rs": "use crate::Frame;\n\npub fn read() -> Frame {\n    Frame\n}\n",
    "src/server.rs": "use crate::frame::Frame;\n\npub fn serve() -> Frame {\n"
    "    Frame\n}\n",
    # TS: a barrel re-exporting by name.
    "web/frame.ts": "export class Frame {}\n",
    "web/codec.ts": 'export function encode(): string {\n  return "";\n}\n',
    "web/index.ts": "export { Frame } from './frame';\n"
    "export { encode } from './codec';\n"
    "export function index(): number {\n  return 1;\n}\n",
    "web/conn.ts": "import { Frame } from './index';\nexport const f = new Frame();\n",
    "web/wire.ts": "import { encode } from './index';\nexport const e = encode();\n",
    # The barrel's own `index` export, named like the barrel itself.
    "web/named.ts": "import { index } from './index';\nexport const n = index();\n",
}


@pytest.fixture
def indexed(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    repo = tmp_path / PROJECT
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run()
    return memgraph_ingestor


def _hops(ingestor: MemgraphIngestor, target: str) -> set[tuple[str, tuple[str, ...]]]:
    rows = graph_query.importers_through_reexports(
        ingestor.fetch_all, PROJECT, f"{PROJECT}.{target}"
    )
    return {
        (
            r["module"].removeprefix(f"{PROJECT}."),
            tuple(h["path"] or "" for h in r["via"]),
        )
        for r in rows
    }


def test_a_python_package_facade_is_followed(indexed: MemgraphIngestor) -> None:
    assert _hops(indexed, "shop._client") == {
        ("shop", ()),
        ("shop._api", ()),
        ("direct", ()),
        ("app", ("shop/__init__.py",)),
        ("cli", ("shop/__init__.py",)),
    }


def test_a_rust_pub_use_is_followed(indexed: MemgraphIngestor) -> None:
    assert _hops(indexed, "src.frame") == {
        ("src.lib", ()),
        ("src.server", ()),
        ("src.connection", ("src/lib.rs",)),
    }


def test_a_ts_barrel_is_followed(indexed: MemgraphIngestor) -> None:
    assert _hops(indexed, "web.frame") == {
        ("web.index", ()),
        ("web.conn", ("web/index.ts",)),
    }


def test_a_ts_named_import_sharing_the_barrels_name_is_not_followed(
    indexed: MemgraphIngestor,
) -> None:
    # `import { index } from './index'` takes one export of the barrel, not
    # the barrel, so it does not reach what the barrel re-exports.
    direct = graph_query.importers(indexed.fetch_all, PROJECT, f"{PROJECT}.web.index")
    named = [r for r in direct if r["module"] == f"{PROJECT}.web.named"]
    assert [r["imported_name"] for r in named] == ["index"]
    assert ("web.named", ("web/index.ts",)) not in _hops(indexed, "web.frame")


def test_the_direct_query_still_lists_only_direct_importers(
    indexed: MemgraphIngestor,
) -> None:
    rows = graph_query.importers(indexed.fetch_all, PROJECT, f"{PROJECT}.shop._client")
    assert [r["module"] for r in rows] == [
        f"{PROJECT}.direct",
        f"{PROJECT}.shop",
        f"{PROJECT}.shop._api",
    ]
