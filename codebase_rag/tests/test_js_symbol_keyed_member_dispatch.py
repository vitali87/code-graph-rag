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


def test_function_local_symbol_pair_is_not_a_constant(tmp_path: Path) -> None:
    # A `k: Symbol(...)` pair INSIDE a function is a local value, not a
    # module symbol constant; an unrelated module-level name spelled the
    # same must keep its generic resolution (here: the unique-class trie
    # CALLS), not get suppressed into a fan.
    files = {
        "ctrl.js": CTRL,
        "main.js": """'use strict'
const { Ctrl } = require('./ctrl.js')
function make () {
  return { kCtrl: Symbol('local') }
}
function use (server, kCtrl) {
  return server[kCtrl].ping()
}
module.exports = { make, use }
""",
    }
    cap = _run(tmp_path, files)
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.ctrl.Ctrl.ping"
        for frm, rel, to in cap.rels
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


def test_module_binding_constructor_install_links(tmp_path: Path) -> None:
    # `const CTP = require('./mod.js')` binding a whole-module CONSTRUCTOR
    # FUNCTION (prototype methods, no class syntax): `[kSym]: new CTP()`
    # must type the install (fastify's kContentTypeParser).
    files = {
        "symbols.js": SYMBOLS,
        "parser.js": """'use strict'
function Parser (opts) {
  this.opts = opts
}
Parser.prototype.ping = function () { return 'pong' }
module.exports = Parser
""",
        "main.js": """'use strict'
const { kCtrl } = require('./symbols.js')
const Parser = require('./parser.js')
function build (options) {
  return { [kCtrl]: new Parser(options) }
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { build, use }
""",
    }
    cap = _run(tmp_path, files)
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.parser.Parser.ping"
        for frm, rel, to in cap.rels
    )


def test_plain_function_nested_helper_never_types_the_install(
    tmp_path: Path,
) -> None:
    # `new makeThing()` where makeThing is a PLAIN function (no prototype
    # members): its nested private helpers share the `module.fn.name`
    # registry namespace with prototype methods, so accepting the function
    # as a constructor would manufacture a confident CALLS to an
    # unreachable helper.
    files = {
        "symbols.js": SYMBOLS,
        "factory.js": """'use strict'
function makeThing (o) {
  function ping () { return 'private helper' }
  ping()
  return {}
}
module.exports = makeThing
""",
        "main.js": """'use strict'
const { kCtrl } = require('./symbols.js')
const makeThing = require('./factory.js')
function build () {
  return { [kCtrl]: new makeThing() }
}
function use (s) {
  return s[kCtrl].ping()
}
module.exports = { build, use }
""",
    }
    cap = _run(tmp_path, files)
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.factory.makeThing.ping"
        for frm, rel, to in cap.rels
    )


def test_prototype_evidence_is_per_member_not_per_function(
    tmp_path: Path,
) -> None:
    # A constructor WITH prototype methods still must not expose its nested
    # private helpers: the evidence has to name the SPECIFIC member.
    files = {
        "symbols.js": SYMBOLS,
        "parser.js": """'use strict'
function Parser (x) {
  function ping () { return 'private helper' }
  ping()
  this.x = x
}
Parser.prototype.other = function () { return 'proto' }
module.exports = Parser
""",
        "main.js": """'use strict'
const { kCtrl } = require('./symbols.js')
const Parser = require('./parser.js')
function build () {
  return { [kCtrl]: new Parser(1) }
}
function use (s) {
  return s[kCtrl].ping()
}
module.exports = { build, use }
""",
    }
    cap = _run(tmp_path, files)
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.parser.Parser.ping"
        for frm, rel, to in cap.rels
    )


def test_class_field_symbol_install_links(tmp_path: Path) -> None:
    # `class Server { [kCtrl] = new Ctrl() }`: the class-field installation
    # feeds the index like an object-literal key.
    files = {
        "symbols.js": SYMBOLS,
        "ctrl.js": CTRL,
        "main.js": """'use strict'
const { kCtrl } = require('./symbols.js')
const { Ctrl } = require('./ctrl.js')
class Server {
  [kCtrl] = new Ctrl()
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { Server, use }
""",
    }
    cap = _run(tmp_path, files)
    assert any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to) == "p.ctrl.Ctrl.ping"
        for frm, rel, to in cap.rels
    )


def test_for_of_var_binding_is_function_scoped(tmp_path: Path) -> None:
    # `for (var ctrl of list)` hoists ctrl to the function; a read AFTER the
    # loop still reads the loop variable, never an outer same-named binding.
    ctrl = CTRL.replace(
        "module.exports = { Ctrl, createCtrl }",
        """class Other {
  ping () { return 'nope' }
}
module.exports = { Ctrl, Other, createCtrl }
""",
    )
    main = """'use strict'
const { kCtrl } = require('./symbols.js')
const { Other } = require('./ctrl.js')
const ctrl = new Other()
function sink (v) { return v }
function build (list) {
  for (var ctrl of list) { sink(ctrl) }
  return { [kCtrl]: ctrl }
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { build, use, ctrl }
"""
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": ctrl, "main.js": main},
    )
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to).endswith(".ping")
        for frm, rel, to in cap.rels
    )


def test_parameter_installation_is_unknowable(tmp_path: Path) -> None:
    # The installed value is a PARAMETER; a same-named module-level binding
    # must not type it (the decorator pattern this feature targets). The
    # call site degrades to REFERENCES over name-matching methods so
    # liveness survives without a wrong-class CALLS.
    main = """'use strict'
const { kCtrl } = require('./symbols.js')
const { Other } = require('./ctrl.js')
const ctrl = new Other()
function install (server, ctrl) {
  return { [kCtrl]: ctrl }
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { install, use, ctrl }
"""
    ctrl = CTRL.replace(
        "module.exports = { Ctrl, createCtrl }",
        """class Other {
  ping () { return 'nope' }
}
module.exports = { Ctrl, Other, createCtrl }
""",
    )
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": ctrl, "main.js": main},
    )
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to).endswith(".ping")
        for frm, rel, to in cap.rels
    )
    refs = str(cs.RelationshipType.REFERENCES)
    for target in ("p.ctrl.Ctrl.ping", "p.ctrl.Other.ping"):
        assert any(
            rel == refs and str(frm).endswith(".use") and str(to) == target
            for frm, rel, to in cap.rels
        )


def test_parameter_subscript_installation_is_unknowable(tmp_path: Path) -> None:
    main = """'use strict'
const { kCtrl } = require('./symbols.js')
const { Other } = require('./ctrl.js')
const controller = new Other()
function decorate (instance, controller) {
  instance[kCtrl] = controller
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { decorate, use, controller }
"""
    ctrl = CTRL.replace(
        "module.exports = { Ctrl, createCtrl }",
        """class Other {
  ping () { return 'nope' }
}
module.exports = { Ctrl, Other, createCtrl }
""",
    )
    cap = _run(
        tmp_path,
        {"symbols.js": SYMBOLS, "ctrl.js": ctrl, "main.js": main},
    )
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to).endswith(".ping")
        for frm, rel, to in cap.rels
    )
    refs = str(cs.RelationshipType.REFERENCES)
    for target in ("p.ctrl.Ctrl.ping", "p.ctrl.Other.ping"):
        assert any(
            rel == refs and str(frm).endswith(".use") and str(to) == target
            for frm, rel, to in cap.rels
        )


def test_block_shadow_does_not_type_the_installation(tmp_path: Path) -> None:
    # The outer `const ctrl = new Ctrl()` is the installed binding; a
    # nested-block shadow (before OR after the install site) must not
    # replace it.
    ctrl = CTRL.replace(
        "module.exports = { Ctrl, createCtrl }",
        """class Other {
  ping () { return 'nope' }
}
module.exports = { Ctrl, Other, createCtrl }
""",
    )
    for shadow_first in (True, False):
        install = "  const ctrl = new Ctrl()\n"
        shadow = "  if (options) { const ctrl = new Other(); ctrl.ping() }\n"
        body = (shadow + install) if shadow_first else (install + shadow)
        main = (
            """'use strict'
const { kCtrl } = require('./symbols.js')
const { Ctrl, Other } = require('./ctrl.js')
function build (options) {
"""
            + body
            + """  return { [kCtrl]: ctrl }
}
function use (server) {
  return server[kCtrl].ping()
}
module.exports = { build, use }
"""
        )
        sub = tmp_path / ("a" if shadow_first else "b")
        sub.mkdir()
        cap = _run(
            sub,
            {"symbols.js": SYMBOLS, "ctrl.js": ctrl, "main.js": main},
        )
        assert any(
            rel == str(cs.RelationshipType.CALLS)
            and str(frm).endswith(".use")
            and str(to) == "p.ctrl.Ctrl.ping"
            for frm, rel, to in cap.rels
        ), f"shadow_first={shadow_first}"
        assert not any(
            str(frm).endswith(".use") and str(to).endswith("Other.ping")
            for frm, _rel, to in cap.rels
        ), f"shadow_first={shadow_first}"


def test_real_static_method_never_falls_back_to_free_function(
    tmp_path: Path,
) -> None:
    # `Registry.build(o)` has a REAL static in the class body; an unrelated
    # module-level `function build` must never type the result.
    files = {
        "symbols.js": SYMBOLS,
        "ctrl.js": CTRL.replace(
            "module.exports = { Ctrl, createCtrl }",
            """class Other {
  ping () { return 'nope' }
}
class Registry {
  static build (o) { return o.cached }
}
function build (o) { return new Other() }
module.exports = { Ctrl, Other, Registry, build, createCtrl }
""",
        ),
        "main.js": MAIN.replace(
            "const { createCtrl } = require('./ctrl.js')",
            "const { Registry } = require('./ctrl.js')",
        ).replace(
            "  const ctrl = createCtrl(options)\n  return { [kCtrl]: ctrl }",
            "  return { [kCtrl]: Registry.build(options) }",
        ),
    }
    cap = _run(tmp_path, files)
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to).endswith(".ping")
        for frm, rel, to in cap.rels
    )


def test_divergent_destructured_returns_do_not_guess(tmp_path: Path) -> None:
    files = {
        "symbols.js": SYMBOLS,
        "ctrl.js": CTRL.replace(
            "module.exports = { Ctrl, createCtrl }",
            """class Other {
  ping () { return 'nope' }
}
function makeCtrl (opts) {
  if (opts.legacy) { return { ctrl: new Other() } }
  return { ctrl: new Ctrl() }
}
module.exports = { Ctrl, Other, makeCtrl, createCtrl }
""",
        ),
        "main.js": MAIN.replace(
            "const { createCtrl } = require('./ctrl.js')",
            "const { makeCtrl } = require('./ctrl.js')",
        ).replace(
            "  const ctrl = createCtrl(options)",
            "  const { ctrl } = makeCtrl(options)",
        ),
    }
    cap = _run(tmp_path, files)
    assert not any(
        rel == str(cs.RelationshipType.CALLS)
        and str(frm).endswith(".use")
        and str(to).endswith(".ping")
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
