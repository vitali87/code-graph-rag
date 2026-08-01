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
        self.exported_nodes: list[str] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if properties.get(cs.KEY_IS_EXPORTED) and (
            qn := properties.get(cs.KEY_QUALIFIED_NAME)
        ):
            self.exported_nodes.append(str(qn))

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


def test_iife_wrapper_collision_targets_the_wrapper_variant(tmp_path: Path) -> None:
    # The wrapper's own name collides with an unrelated top-level function;
    # the module CALLS edge must target the qn actually minted for the
    # WRAPPER (a duplicate variant), never the namesake.
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
function build (v) { return v }
module.exports = function build (validator) {
  function inner (x) { return x }
  return inner
}(1)
""",
        },
    )
    calls = str(cs.RelationshipType.CALLS)
    module_calls = {
        str(to) for frm, rel, to in cap.rels if rel == calls and str(frm) == "p.ser"
    }
    assert "p.ser.build" not in module_calls
    assert any(t.startswith("p.ser.build") for t in module_calls)


def test_export_inside_function_emits_nothing(tmp_path: Path) -> None:
    # `module.exports = ...` INSIDE a function is not a module-load export;
    # no module CALLS edge and no phantom qn may be emitted.
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
function setup () {
  module.exports = function wrapped (v) { return v }(1)
}
setup()
""",
        },
    )
    calls = str(cs.RelationshipType.CALLS)
    assert not any(
        rel == calls and str(frm) == "p.ser" and "wrapped" in str(to)
        for frm, rel, to in cap.rels
    )


def test_inside_function_export_does_not_map_alias(tmp_path: Path) -> None:
    # The refused registration must not leave a guessed alias mapping behind
    # that points a consumer call at an unrelated top-level namesake.
    cap = _run(
        tmp_path,
        {
            "m.js": """'use strict'
function handler (v) { return 'top' }
function setup () {
  module.exports = function handler (v) { return 'inner' }
}
setup()
""",
            "b.js": """'use strict'
const h = require('./m')
function main () { return h(1) }
module.exports = { main }
""",
        },
    )
    assert not any(
        str(frm).endswith(".main") and str(to) == "p.m.handler"
        for frm, _rel, to in cap.rels
    )


def test_reindex_clears_direct_export_map(tmp_path: Path) -> None:
    # A re-parsed module that no longer directly exports a function must not
    # leave the stale alias mapping behind (watch mode).
    (tmp_path / "ser.js").write_text("""'use strict'
module.exports = function serialize (v) { return String(v) }
""")
    (tmp_path / "b.js").write_text("""'use strict'
const s = require('./ser')
function main () { return s(1) }
module.exports = { main }
""")
    parsers, queries = load_parsers()
    cap = _Capture()
    updater = GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    (tmp_path / "ser.js").write_text("""'use strict'
function serialize (v) { return String(v) }
module.exports = { other: (v) => v }
""")
    cap.rels.clear()
    updater.run(force=True)
    assert not any(
        str(frm).endswith(".main") and str(to) == "p.ser.serialize"
        for frm, _rel, to in cap.rels
    )


def test_parenthesised_iife_export_links(tmp_path: Path) -> None:
    # The canonical hand-written IIFE: `(function wrapped () {...})(1)`.
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
module.exports = (function wrapped (v) { return v })(1)
""",
        },
    )
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and str(frm) == "p.ser" and str(to).endswith(".wrapped")
        for frm, rel, to in cap.rels
    )


def test_parenthesised_arrow_iife_export_links(tmp_path: Path) -> None:
    cap = _run(
        tmp_path,
        {
            "ser.js": """'use strict'
module.exports = ((v) => v)(1)
""",
        },
    )
    calls = str(cs.RelationshipType.CALLS)
    assert any(
        rel == calls and str(frm) == "p.ser" and str(to).startswith("p.ser.")
        for frm, rel, to in cap.rels
    )


def test_reindex_does_not_resurrect_sparse_export_node(tmp_path: Path) -> None:
    # A reused updater re-parses the file; the stale span claim refuses
    # re-registration, so finalisation must NOT write an exported node (or
    # repopulate the alias map) for a qn absent from the registry.
    (tmp_path / "ser.js").write_text("module.exports = (v) => String(v)\n")
    (tmp_path / "b.js").write_text("""'use strict'
const s = require('./ser')
function main () { return s(1) }
module.exports = { main }
""")
    parsers, queries = load_parsers()
    cap = _Capture()
    updater = GraphUpdater(
        ingestor=cap,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    (tmp_path / "ser.js").write_text("module.exports = (v) => String(v) + ''\n")
    updater.remove_file_from_state(tmp_path / "ser.js")
    cap.exported_nodes.clear()
    updater.run(force=True)
    registry = updater.factory.call_processor._resolver.function_registry
    for qn in cap.exported_nodes:
        assert qn in registry
