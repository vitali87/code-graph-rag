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

_READS = cs.RelationshipType.READS_FROM.value
_WRITES = cs.RelationshipType.WRITES_TO.value


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "js_project"
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
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
        f"MATCH ()-[r:{_READS}|{_WRITES}]->(res:{cs.NodeLabel.RESOURCE.value}) "
        "RETURN type(r) AS rel, res.qualified_name AS qn"
    )
    return {(str(row["rel"]), str(row["qn"])) for row in rows}


_JS_CODE = """\
function leak(data) {
  console.log(data);
  fetch("https://api.example.com/data");
  fs.writeFileSync("out.txt", data);
  axios.post("https://api.example.com/upload");
}
"""


def test_javascript_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(memgraph_ingestor, tmp_path, "app.js", _JS_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_WRITES, "resource::NETWORK::https://api.example.com/upload") in edges


def test_typescript_direct_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(memgraph_ingestor, tmp_path, "app.ts", _JS_CODE)
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in edges
    assert (_WRITES, "resource::FILE::out.txt") in edges
    assert (_WRITES, "resource::NETWORK::https://api.example.com/upload") in edges


def test_module_level_js_io_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Top-level (module-scope) I/O: the caller is the module root, which has no
    # (H) body field, so the walk must seed from its own statements (issue #714 P1).
    _build(
        memgraph_ingestor,
        tmp_path,
        "main.js",
        'console.log("boot");\nfs.writeFileSync("cfg.json", data);\n',
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::STDOUT::<dynamic>") in edges
    assert (_WRITES, "resource::FILE::cfg.json") in edges


def test_shadowed_local_import_emits_no_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `fs` imported from a LOCAL module is not Node's fs: fs.writeFileSync must
    # (H) NOT emit a FILE edge (the raw-dotted registry fallback would otherwise
    # (H) misfire on the shadowed name). Issue #714 precision.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from './fake';\n\n\n"
        "function save(d) {\n  fs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") not in _io_edges(memgraph_ingestor)


def test_real_builtin_import_still_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) The genuine `import fs from 'fs'` (maps to fs.default) must still emit.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from 'fs';\n\n\n"
        "function save(d) {\n  fs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_commonjs_require_still_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `const fs = require('fs')` binds fs via a declarator, but it is an import
    # (H) alias (tracked in import_map), not a shadowing local -- it must still emit.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function save(d) {\n"
        "  const fs = require('fs');\n"
        "  fs.writeFileSync('a.txt', d);\n"
        "}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_node_prefixed_builtin_import_still_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `import fs from 'node:fs'` is a genuine builtin (the modern form); the
    # (H) node: prefix must not be mistaken for a shadowing local module.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from 'node:fs';\n\n\n"
        "function save(d) {\n  fs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_local_declarations_shadow_sinks(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Names bound locally are not the builtins: a local `const fs`, a local
    # (H) `function fetch`, and a `http` parameter must all suppress the sink match
    # (H) (issue #714 precision -- no import entry exists for these).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function run(http) {\n"
        "  const fs = {};\n"
        "  function fetch(u) {}\n"
        "  fs.writeFileSync('x.txt', d);\n"
        "  fetch('https://n/a');\n"
        "  http.get('https://n/b');\n"
        "}\n",
    )
    edges = _io_edges(memgraph_ingestor)
    assert (_WRITES, "resource::FILE::x.txt") not in edges
    assert (_READS, "resource::NETWORK::https://n/a") not in edges
    assert (_READS, "resource::NETWORK::https://n/b") not in edges
