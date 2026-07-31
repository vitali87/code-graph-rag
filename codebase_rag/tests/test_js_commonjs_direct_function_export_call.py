# A module exporting a function expression directly (`module.exports =
# function name () {...}`) is consumed as `const f = require('./mod'); f(x)`;
# the call must link to the exported function or it (and everything nested in
# it) reports dead. Found dogfooding fastify: lib/error-serializer.js is
# exactly `module.exports = function anonymous (...) {...}`, consumed by
# lib/error-handler.js as `serializeError({...})`. Issue #991.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"


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


def _run(tmp_path: Path, files: dict[str, str]) -> _Capture:
    for name, src in files.items():
        (tmp_path / name).write_text(src)
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


def _linked(cap: _Capture, caller_leaf: str, target_qn: str) -> bool:
    return any(
        str(frm).rsplit(cs.SEPARATOR_DOT, 1)[-1] == caller_leaf
        and str(to) == target_qn
        and rel != str(cs.RelationshipType.DEFINES)
        for frm, rel, to in cap.rels
    )


def test_direct_function_export_called_via_required_alias(tmp_path: Path) -> None:
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
module.exports = function anonymous (v) { return String(v) }
""",
            "b.js": """'use strict'
const serialize = require('./ser')
function main () { return serialize(1) }
module.exports = { main }
""",
        },
    )
    assert _linked(cap, "main", "p.ser.anonymous")


def test_direct_arrow_export_called_via_required_alias(tmp_path: Path) -> None:
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
module.exports = (v) => String(v)
""",
            "b.js": """'use strict'
const serialize = require('./ser')
function main () { return serialize(1) }
module.exports = { main }
""",
        },
    )
    # The anonymous arrow registers positionally under the module; any
    # non-DEFINES edge from main into a ser-module function keeps it alive.
    assert any(
        str(frm).rsplit(cs.SEPARATOR_DOT, 1)[-1] == "main"
        and str(to).startswith("p.ser.")
        and rel != str(cs.RelationshipType.DEFINES)
        for frm, rel, to in cap.rels
    )


def test_object_export_call_still_links(tmp_path: Path) -> None:
    # The already-working shape: `module.exports = { fn }` consumed via
    # destructuring keeps its edge.
    cap = _run(
        tmp_path,
        {
            "lib.js": """'use strict'
function fn (v) { return v }
module.exports = { fn }
""",
            "b.js": """'use strict'
const { fn } = require('./lib')
function main () { return fn(1) }
module.exports = { main }
""",
        },
    )
    assert _linked(cap, "main", "p.lib.fn")


def test_iife_module_export_calls_the_wrapped_function(tmp_path: Path) -> None:
    # The generated fast-json-stringify shape (fastify's error-serializer):
    # the export is the RESULT of immediately invoking the function, so the
    # module must gain a CALLS edge onto the wrapped function; its returned
    # inner callable stays alive transitively through the internal reference.
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
const validator = null
const serializer = null
module.exports = function anonymous(validator, serializer) {
  function anonymous0 (input) { return String(input) }
  const main = anonymous0
  return main
}(validator, serializer)
""",
        },
    )
    assert any(
        str(to) == "p.ser.anonymous" and rel == str(cs.RelationshipType.CALLS)
        for _frm, rel, to in cap.rels
    )
