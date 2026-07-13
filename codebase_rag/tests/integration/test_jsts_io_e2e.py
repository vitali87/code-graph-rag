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


def test_expression_bodied_arrow_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) An expression-bodied arrow has no statement block -- its body IS the call
    # (H) expression, which must still be walked for sinks (issue #714 recall).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        'const load = () => fetch("https://api.example.com/data");\n',
    )
    assert (_READS, "resource::NETWORK::https://api.example.com/data") in _io_edges(
        memgraph_ingestor
    )


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


def test_aliased_builtin_import_still_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `const myfs = require('fs')` aliases the real builtin under a new name;
    # (H) import normalization resolves myfs.writeFileSync -> fs.writeFileSync, so
    # (H) it must still emit (the alias is not a shadowing local module).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "const myfs = require('fs');\n\n\n"
        "function save(d) {\n  myfs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_module_import_plus_local_shadow_emits_no_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `fs` imported module-wide, but a function-local `const fs = {}` shadows it;
    # (H) the local object is not Node's fs, so no FILE edge (a module import must
    # (H) not blanket-cancel a genuine local shadow of the same name).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from 'fs';\n\n\n"
        "function save(d) {\n  const fs = {};\n  fs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") not in _io_edges(memgraph_ingestor)


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


def test_destructured_local_shadows_sink(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A destructured local binding (`const { fetch } = obj`) is not the global
    # (H) builtin, so `fetch(...)` in that scope must not emit a NETWORK edge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function run(obj) {\n  const { fetch } = obj;\n  fetch('https://n/a');\n}\n",
    )
    assert (_READS, "resource::NETWORK::https://n/a") not in _io_edges(
        memgraph_ingestor
    )


def test_destructured_param_shadows_sink(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A destructured parameter (`function run({ http }) {}`) binds `http`
    # (H) locally, so `http.get(...)` must not emit a NETWORK edge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function run({ http }) {\n  http.get('https://n/b');\n}\n",
    )
    assert (_READS, "resource::NETWORK::https://n/b") not in _io_edges(
        memgraph_ingestor
    )


def test_block_scoped_local_does_not_over_shadow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `const`/`let` are block-scoped: a `const fs` inside a nested block does
    # (H) NOT shadow a `fs.writeFileSync` outside that block, so the edge stands.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function f(d) {\n"
        "  {\n    const fs = {};\n  }\n"
        "  fs.writeFileSync('out.txt', d);\n"
        "}\n",
    )
    assert (_WRITES, "resource::FILE::out.txt") in _io_edges(memgraph_ingestor)


def test_block_local_shadows_within_its_block(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A `const fs` used WITHIN its own block is shadowed there: no FILE edge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function f(d) {\n"
        "  if (d) {\n"
        "    const fs = {};\n"
        "    fs.writeFileSync('out.txt', d);\n"
        "  }\n"
        "}\n",
    )
    assert (_WRITES, "resource::FILE::out.txt") not in _io_edges(memgraph_ingestor)


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


def test_esm_named_import_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `import { writeFileSync } from 'fs'` binds the bare name to fs.writeFileSync.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import { writeFileSync } from 'fs';\n\n\n"
        "function save(d) {\n  writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_node_prefixed_named_import_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `import { writeFileSync } from 'node:fs'` maps to node:fs.writeFileSync;
    # (H) the node: scheme must be stripped for the sink match.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import { writeFileSync } from 'node:fs';\n\n\n"
        "function save(d) {\n  writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_destructured_require_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `const { writeFileSync } = require('fs')` is a destructured CommonJS import.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "const { writeFileSync } = require('fs');\n\n\n"
        "function save(d) {\n  writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_renamed_destructured_require_emits(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `const { writeFileSync: wf } = require('fs')` binds the local wf.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "const { writeFileSync: wf } = require('fs');\n\n\n"
        "function save(d) {\n  wf('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") in _io_edges(memgraph_ingestor)


def test_scoped_package_ending_in_builtin_name_no_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `import fs from 'memfs/fs'` is a third-party package, not Node's fs; its
    # (H) path ending in `fs` must NOT make fs.writeFileSync match (JS imports are
    # (H) not path-resolved to a package name like Go's).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from 'memfs/fs';\n\n\n"
        "function save(d) {\n  fs.writeFileSync('a.txt', d);\n}\n",
    )
    assert (_WRITES, "resource::FILE::a.txt") not in _io_edges(memgraph_ingestor)
