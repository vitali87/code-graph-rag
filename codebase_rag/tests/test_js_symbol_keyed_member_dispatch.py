# A method call through a symbol-keyed computed member,
# `obj[kSymbol].method(...)`, emits no CALLS edge when the symbol and the
# class live in different modules from the call site; every method of a
# class installed this way reports dead. Found dogfooding fastify
# (LogController via kLogController, SchemaController via
# kSchemaController: seventeen candidates). Issue #989.
from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyDict, PropertyValue, ResultRow

PROJECT = "p"

SYMBOLS = """'use strict'
module.exports = { kCtrl: Symbol('ctrl') }
"""

CTRL = """'use strict'
class Ctrl {
  ping () { return 'pong' }
}
function createCtrl (options) { return new Ctrl() }
module.exports = { Ctrl, createCtrl }
"""

MAIN = """'use strict'
const { kCtrl } = require('./symbols.js')
const { createCtrl } = require('./ctrl.js')
function build (options) {
  const ctrl = createCtrl(options)
  return { [kCtrl]: ctrl }
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { build, use }
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


def test_cross_module_symbol_keyed_method_call_links(tmp_path: Path) -> None:
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": CTRL, "main.js": MAIN},
    )
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.ctrl.Ctrl.ping"
        for frm, rel, to in cap.rels
    )


def test_subscript_assignment_installation_links(tmp_path: Path) -> None:
    # The `obj[kSym] = value` installation form (fastify installs onto
    # `this`-like receivers) feeds the same index.
    main = MAIN.replace(
        "  return { [kCtrl]: ctrl }",
        "  const server = {}\n  server[kCtrl] = ctrl\n  return server",
    )
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": CTRL, "main.js": main},
    )
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.ctrl.Ctrl.ping"
        for frm, rel, to in cap.rels
    )


def test_two_installed_classes_reference_without_guessing(tmp_path: Path) -> None:
    # Two DIFFERENT classes installed under one symbol project-wide: no
    # single CALLS edge may be guessed (the bare-name fallback used to pick
    # one arbitrarily); instead each candidate's matching method is
    # REFERENCED so liveness holds.
    ctrl = CTRL.replace(
        "module.exports = { Ctrl, createCtrl }",
        """class Other {
  ping () { return 'nope' }
}
module.exports = { Ctrl, Other, createCtrl }
""",
    )
    main = MAIN.replace(
        "const { createCtrl } = require('./ctrl.js')",
        "const { createCtrl, Other } = require('./ctrl.js')",
    ).replace(
        "module.exports = { build, use }",
        """function shadow (o) {
  return { [kCtrl]: new Other() }
}
module.exports = { build, use, shadow }
""",
    )
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": ctrl, "main.js": main},
    )
    calls = str(cs.RelationshipType.CALLS)
    refs = str(cs.RelationshipType.REFERENCES)
    assert not any(
        rel == calls and str(frm).endswith(".use") and str(to).endswith(".ping")
        for frm, rel, to in cap.rels
    )
    for target in ("p.ctrl.Ctrl.ping", "p.ctrl.Other.ping"):
        assert any(
            rel == refs and str(frm).endswith(".use") and str(to) == target
            for frm, rel, to in cap.rels
        )


def test_non_symbol_computed_key_never_joins_the_index(tmp_path: Path) -> None:
    # `rows[i] = new Row()` with a plain loop variable must not let the
    # SYMBOL index type an unrelated `cells[i].render()`. Two render-bearing
    # classes keep the pre-existing name-trie fallback out of the picture,
    # so any Row.render edge here could only come from the index.
    files = {
        "rows.js": """'use strict'
class Row {
  render () { return 1 }
}
class Panel {
  render () { return 2 }
}
function fill (rows, cells, i) {
  rows[i] = new Row()
  return cells[i].render()
}
function decorate (p) { return new Panel().render() }
module.exports = { Row, Panel, fill, decorate }
""",
    }
    cap = _run(tmp_path, files)
    assert not any(
        str(frm).endswith(".fill") and str(to) == "p.rows.Row.render"
        for frm, _rel, to in cap.rels
    )


def test_symbol_for_registry_constant_links(tmp_path: Path) -> None:
    symbols = SYMBOLS.replace("Symbol('ctrl')", "Symbol.for('app.ctrl')")
    cap = _run(
        tmp_path,
        {"symbols.js": symbols, "ctrl.js": CTRL, "main.js": MAIN},
    )
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.ctrl.Ctrl.ping"
        for frm, rel, to in cap.rels
    )


def test_unknown_method_on_installed_class_emits_nothing(tmp_path: Path) -> None:
    main = MAIN.replace("server[kCtrl].ping()", "server[kCtrl].vanish()")
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": CTRL, "main.js": main},
    )
    assert not any(
        str(frm).endswith(".use") and "vanish" in str(to) for frm, _rel, to in cap.rels
    )
