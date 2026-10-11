# Two flaws in the re-export walks #2793 extended (follow-up to #2560):
#
# A barrel's own imports are not its exports. `import { Router } from
# './router'` in `barrel.ts` lets the barrel use `Router`; it does not let
# `import * as r from './barrel'` name `r.Router`, and tsc rejects
# `implements r.Router`. The namespace-member lookup followed the barrel's
# import map anyway and recorded IMPLEMENTS (and OVERRIDES) onto the
# first-party `Router`; the call resolver's type lookup did the same for a
# receiver typed by a name the barrel only imports.
#
# A module reached by two `export *` paths was walked only once: the first
# path marked it visited, so when that path ended ambiguous the second could
# not supply the one export it really provides, and `impl.Base` lost its
# INHERITS and CALLS edges.
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from codebase_rag import constants as cs
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts.commonjs_exports import (
    commonjs_export_bindings,
    is_commonjs_module,
)
from codebase_rag.parsers.js_ts.reexports import JsExport, follow_js_reexports
from codebase_rag.parsers.utils import follow_reexports
from codebase_rag.types_defs import NodeType, PropertyDict, PropertyValue

_PROJECT = "proj"
_PREFIX = f"{_PROJECT}{cs.SEPARATOR_DOT}"
_IMPLEMENTS = cs.RelationshipType.IMPLEMENTS.value
_INHERITS = cs.RelationshipType.INHERITS.value
_OVERRIDES = cs.RelationshipType.OVERRIDES.value
_CALLS = cs.RelationshipType.CALLS.value
_CLASS = cs.NodeLabel.CLASS.value
_INTERFACE = cs.NodeLabel.INTERFACE.value
_EXTERNAL = cs.NodeLabel.EXTERNAL_MODULE.value
_EXACT = cs.EdgeResolution.EXACT.value


class _Edge(NamedTuple):
    source: str
    rel: str
    label: str
    target: str
    resolution: PropertyValue


class _Recorder:
    def __init__(self) -> None:
        self.edges: list[_Edge] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        return None

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        self.edges.append(
            _Edge(
                str(from_spec[2]).removeprefix(_PREFIX),
                str(rel_type),
                str(to_spec[0]),
                str(to_spec[2]).removeprefix(_PREFIX),
                (properties or {}).get(cs.KEY_RESOLUTION),
            )
        )

    def flush_all(self) -> None:
        return None


def _index(root: Path, files: dict[str, str]) -> list[_Edge]:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    parsers, queries = load_parsers()
    recorder = _Recorder()
    GraphUpdater(
        ingestor=recorder,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
    ).run(force=True)
    return recorder.edges


def _heritage(edges: list[_Edge], source: str) -> set[tuple[str, str, str]]:
    return {
        (e.rel, e.label, e.target)
        for e in edges
        if e.source == source and e.rel in (_IMPLEMENTS, _INHERITS)
    }


def _exact_calls(edges: list[_Edge], source: str) -> set[str]:
    return {
        e.target
        for e in edges
        if e.source == source and e.rel == _CALLS and e.resolution == _EXACT
    }


def _overrides(edges: list[_Edge], source: str) -> set[str]:
    return {e.target for e in edges if e.source == source and e.rel == _OVERRIDES}


# --- a namespace member names only what the barrel exports ----------------

ROUTER_TS = """\
export interface Router { route(path: string): void }
export class Svc { go(): number { return 1 } }
"""

# Issue scenario: the barrel imports `Router` (and `Svc`) for its own use.
PRIVATE_IMPORT_BARREL_TS = """\
import { Router, Svc } from './router'
export function make(r: Router): Svc { return new Svc() }
"""

NAMESPACE_USER_TS = """\
import * as r from './barrel'
export class X implements r.Router { route(path: string): void {} }
export class Sub extends r.Svc { go(): number { return super.go() } }
"""

_UNEXPORTED_BARRELS = {
    "private-import": {
        "router.ts": ROUTER_TS,
        "barrel.ts": PRIVATE_IMPORT_BARREL_TS,
    },
    # `export *` passes on what `mid` exports; `mid` only imports them.
    "star-of-a-private-import": {
        "router.ts": ROUTER_TS,
        "mid.ts": PRIVATE_IMPORT_BARREL_TS,
        "barrel.ts": "export * from './mid'\n",
    },
    # A default import binds a local alias, no export of the barrel's.
    "default-import-alias": {
        "iface.ts": "export default interface Router { route(path: string): void }\n",
        "svc.ts": "export default class Svc { go(): number { return 1 } }\n",
        "barrel.ts": (
            "import Router from './iface'\nimport Svc from './svc'\n"
            "export function make(r: Router): Svc { return new Svc() }\n"
        ),
    },
    # Declared in the namespace's own module, but never exported.
    "own-private-declaration": {
        "barrel.ts": (
            "interface Router { route(path: string): void }\n"
            "class Svc { go(): number { return 1 } }\n"
            "export function make(r: Router): Svc { return new Svc() }\n"
        ),
    },
}


@pytest.mark.parametrize(
    "files", list(_UNEXPORTED_BARRELS.values()), ids=list(_UNEXPORTED_BARRELS)
)
def test_a_name_the_barrel_does_not_export_is_not_a_namespace_member(
    tmp_path: Path, files: dict[str, str]
) -> None:
    edges = _index(tmp_path, {**files, "user.ts": NAMESPACE_USER_TS})
    assert _heritage(edges, "user.X") == {(_IMPLEMENTS, _EXTERNAL, "r.Router")}, edges
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _EXTERNAL, "r.Svc")}, edges
    # A first-party base would have its methods taken as overridden.
    assert not _overrides(edges, "user.X.route"), edges
    assert not _overrides(edges, "user.Sub.go"), edges


_EXPORTING_BARRELS = {
    "explicit-re-export": "export { Router, Svc } from './router'\n",
    "export-of-an-import": (
        "import { Router, Svc } from './router'\nexport { Router, Svc }\n"
    ),
    "star": "export * from './router'\n",
}


@pytest.mark.parametrize(
    "barrel", list(_EXPORTING_BARRELS.values()), ids=list(_EXPORTING_BARRELS)
)
def test_a_name_the_barrel_exports_is_still_a_namespace_member(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(
        tmp_path,
        {"router.ts": ROUTER_TS, "barrel.ts": barrel, "user.ts": NAMESPACE_USER_TS},
    )
    assert _heritage(edges, "user.X") == {(_IMPLEMENTS, _INTERFACE, "router.Router")}, (
        edges
    )
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _CLASS, "router.Svc")}, edges
    assert _overrides(edges, "user.X.route") == {"router.Router.route"}, edges


def test_an_exported_declaration_of_the_namespace_module_is_its_member(
    tmp_path: Path,
) -> None:
    edges = _index(
        tmp_path,
        {
            "barrel.ts": ROUTER_TS,
            "user.ts": NAMESPACE_USER_TS,
        },
    )
    assert _heritage(edges, "user.X") == {(_IMPLEMENTS, _INTERFACE, "barrel.Router")}, (
        edges
    )
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _CLASS, "barrel.Svc")}, edges


# The call resolver's type lookup took the same detour: `Svc` imported from a
# barrel that only imports it typed `s`, and `s.go()` bound exactly.
TYPED_USER_TS = """\
import { Svc } from './barrel'
export function use(): number { const s = new Svc(); return s.go() }
"""


@pytest.mark.parametrize(
    "barrel_files",
    [
        {"barrel.ts": PRIVATE_IMPORT_BARREL_TS},
        {"mid.ts": PRIVATE_IMPORT_BARREL_TS, "barrel.ts": "export * from './mid'\n"},
    ],
    ids=["private-import", "star-of-a-private-import"],
)
def test_a_receiver_typed_by_an_unexported_name_binds_no_exact_call(
    tmp_path: Path, barrel_files: dict[str, str]
) -> None:
    edges = _index(
        tmp_path, {"router.ts": ROUTER_TS, **barrel_files, "user.ts": TYPED_USER_TS}
    )
    assert "router.Svc.go" not in _exact_calls(edges, "user.use"), edges


@pytest.mark.parametrize(
    "barrel", list(_EXPORTING_BARRELS.values()), ids=list(_EXPORTING_BARRELS)
)
def test_a_receiver_typed_by_an_exported_name_still_binds_exactly(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(
        tmp_path,
        {"router.ts": ROUTER_TS, "barrel.ts": barrel, "user.ts": TYPED_USER_TS},
    )
    assert _exact_calls(edges, "user.use") == {"router.Svc.go"}, edges


# A CommonJS barrel's `module.exports = { Svc }` is its export table: the
# walk follows the published name to the binding the barrel `require`s.
COMMONJS_FILES = {
    "svc.js": "class Svc { go() { return 1 } }\nmodule.exports = { Svc }\n",
    "barrel.js": "const { Svc } = require('./svc')\nmodule.exports = { Svc }\n",
    "user.js": (
        "const { Svc } = require('./barrel')\n"
        "const r = require('./barrel')\n"
        "function use() { const s = new Svc(); return s.go() }\n"
        "class Sub extends r.Svc { run() { return this.go() } }\n"
        "module.exports = { use, Sub }\n"
    ),
}


_REQUIRE_SVC = "const { Svc } = require('./svc')\n"

# A publication runs at load wherever it sits in an expression, or in a
# function called on the spot (Greptile, PR #3355).
_LOAD_TIME_PUBLICATIONS = {
    "statement": f"{_REQUIRE_SVC}module.exports = {{ Svc }}\n",
    "frozen": f"{_REQUIRE_SVC}Object.freeze(module.exports = {{ Svc }})\n",
    "object-assign": f"{_REQUIRE_SVC}Object.assign(module.exports, {{ Svc }})\n",
    "iife": f"{_REQUIRE_SVC};(function () {{ module.exports = {{ Svc }} }})()\n",
    "arrow-iife": f"{_REQUIRE_SVC};(() => {{ exports.Svc = Svc }})()\n",
    "iife-called-with-this": (
        f"{_REQUIRE_SVC};(function () {{ exports.Svc = Svc }}).call(this)\n"
    ),
    # Parentheses change nothing Node publishes (Greptile, PR #3355).
    "parenthesised-assignment": f"{_REQUIRE_SVC};(module.exports = {{ Svc }})\n",
    "parenthesised-object": f"{_REQUIRE_SVC}module.exports = ({{ Svc }})\n",
    "parenthesised-target": f"{_REQUIRE_SVC};(module.exports) = {{ Svc }}\n",
    "parenthesised-assign-source": (
        f"{_REQUIRE_SVC}Object.assign(module.exports, ({{ Svc }}))\n"
    ),
    # `exports` and `module.exports` are references to objects (Greptile, PR
    # #3355): re-pointing either at the other's object reattaches them.
    "exports-reattached": (
        f"{_REQUIRE_SVC}module.exports = {{}}\nexports = module.exports\n"
        "exports.Svc = Svc\n"
    ),
    "module-exports-set-to-exports": (
        f"{_REQUIRE_SVC}exports.Svc = Svc\nmodule.exports = exports\n"
    ),
}


@pytest.mark.parametrize(
    "barrel",
    list(_LOAD_TIME_PUBLICATIONS.values()),
    ids=list(_LOAD_TIME_PUBLICATIONS),
)
def test_a_commonjs_barrel_is_still_followed(tmp_path: Path, barrel: str) -> None:
    edges = _index(tmp_path, {**COMMONJS_FILES, "barrel.js": barrel})
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _CLASS, "svc.Svc")}, edges
    assert _exact_calls(edges, "user.use") == {"svc.Svc.go"}, edges


# A write outside the modelled subset leaves the module's exports unknown,
# so it records nothing (Greptile, PR #3355): a function that runs later, if
# ever, a write nested in a value, a condition, a `switch`, a computed key.
_UNMODELLED_PUBLICATIONS = {
    "callback": f"{_REQUIRE_SVC}setTimeout(() => {{ module.exports = {{ Svc }} }})\n",
    "uncalled-function": (
        f"{_REQUIRE_SVC}function later() {{ module.exports = {{ Svc }} }}\n"
    ),
    "replacement-inside-the-value": (
        f"{_REQUIRE_SVC}module.exports = {{ Svc, side: (module.exports = {{}}) }}\n"
    ),
    "inside-a-top-level-if": (
        f"{_REQUIRE_SVC}if (Svc) {{ module.exports = {{ Svc }} }}\n"
    ),
    "inside-a-switch": (
        f"{_REQUIRE_SVC}switch (1) {{ case 1: module.exports = {{ Svc }} }}\n"
    ),
    "computed-key-beside-it": (
        f"{_REQUIRE_SVC}module.exports = {{ Svc }}\nmodule.exports[key] = 1\n"
    ),
}


# A name a scope between the publication and the module binds is that
# scope's own (Greptile, PR #3355): it is not the module's `require`d `Svc`.
SHADOW_USER_JS = (
    "const r = require('./barrel')\n"
    "const { Public } = require('./barrel')\n"
    "class Sub extends r.Public { run() { return this.go() } }\n"
    "function use() { const p = new Public(); return p.go() }\n"
    "module.exports = { Sub, use }\n"
)

_SHADOWING_BARRELS = {
    "iife-parameter": (
        f"{_REQUIRE_SVC};(function (Svc) {{ exports.Public = Svc }})"
        "(class Other {})\n"
    ),
    "iife-local": (
        f"{_REQUIRE_SVC};(function () {{ const Svc = class Other {{}}; "
        "exports.Public = Svc })()\n"
    ),
    "iife-hoisted-var": (
        f"{_REQUIRE_SVC};(function () {{ exports.Public = Svc; "
        "var Svc = class Other {} })()\n"
    ),
    "block-const": (
        f"{_REQUIRE_SVC}if (Svc) {{ const Svc = class Other {{}}; "
        "exports.Public = Svc }\n"
    ),
    "catch-parameter": (
        f"{_REQUIRE_SVC}try {{ throw 1 }} catch (Svc) {{ exports.Public = Svc }}\n"
    ),
    "shadowed-exports": (
        f"{_REQUIRE_SVC};(function (exports) {{ exports.Public = Svc }})({{}})\n"
    ),
    # `var` and a sloppy function declaration leave their block for the
    # function.
    "iife-var-in-a-nested-block": (
        f"{_REQUIRE_SVC};(function () {{ if (1) {{ var Svc = 0 }} "
        "exports.Public = Svc })()\n"
    ),
    "iife-function-in-a-nested-block": (
        f"{_REQUIRE_SVC};(function () {{ {{ function Svc() {{}} }} "
        "exports.Public = Svc })()\n"
    ),
    # A `switch` body is one scope for all its cases (Greptile, PR #3355).
    "switch-case-local": (
        f"{_REQUIRE_SVC};(() => {{ switch (1) {{ case 1: "
        "const Svc = class Other {}; exports.Public = Svc; } })();\n"
    ),
    # The target's own sub-expression replaces `module.exports` first, so
    # `exports.Public` lands on the old object (Greptile, PR #3355).
    "replacement-in-a-target-sub-expression": (
        f"{_REQUIRE_SVC}const holder = {{}}\n"
        "holder[(module.exports = {}, 'x')] = exports.Public = Svc;\n"
    ),
    # `Svc` is the barrel's own class; a nested `require` of the same name
    # must not stand in for it (Greptile, PR #3355).
    "nested-require-of-the-name": (
        "class Svc { go() { return 2 } }\n"
        "function later() { const { Svc } = require('./other') }\n"
        "module.exports = { Public: Svc };\n"
    ),
}

_UNSHADOWED_BARRELS = {
    "statement": f"{_REQUIRE_SVC}exports.Public = Svc\n",
    "iife": f"{_REQUIRE_SVC};(function () {{ exports.Public = Svc }})()\n",
    "iife-with-other-locals": (
        f"{_REQUIRE_SVC};(function (other) {{ const local = other; "
        "exports.Public = Svc })(1)\n"
    ),
    # A block's `let`, `const` and `class` stay in that block (Greptile, PR
    # #3355): only a scope that encloses the publication shadows.
    "closed-block-const": (
        f"{_REQUIRE_SVC};(() => {{ {{ const Svc = 0; }} exports.Public = Svc; }})();\n"
    ),
    "closed-block-class": (
        f"{_REQUIRE_SVC};(() => {{ if (1) {{ class Svc {{}} }} "
        "exports.Public = Svc })()\n"
    ),
    "closed-module-block": f"{_REQUIRE_SVC}{{ let Svc = 0 }}\nexports.Public = Svc\n",
}


@pytest.mark.parametrize(
    "barrel", list(_SHADOWING_BARRELS.values()), ids=list(_SHADOWING_BARRELS)
)
def test_a_name_a_scope_shadows_is_not_the_module_binding(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(
        tmp_path,
        {
            **COMMONJS_FILES,
            "other.js": "class Svc { go() { return 3 } }\nmodule.exports = { Svc }\n",
            "barrel.js": barrel,
            "user.js": SHADOW_USER_JS,
        },
    )
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _EXTERNAL, "r.Public")}, edges
    assert not _exact_calls(edges, "user.use"), edges


@pytest.mark.parametrize(
    "barrel", list(_UNSHADOWED_BARRELS.values()), ids=list(_UNSHADOWED_BARRELS)
)
def test_a_module_binding_published_from_an_inner_scope_resolves(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(
        tmp_path,
        {**COMMONJS_FILES, "barrel.js": barrel, "user.js": SHADOW_USER_JS},
    )
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _CLASS, "svc.Svc")}, edges
    assert _exact_calls(edges, "user.use") == {"svc.Svc.go"}, edges


@pytest.mark.parametrize(
    "barrel",
    list(_UNMODELLED_PUBLICATIONS.values()),
    ids=list(_UNMODELLED_PUBLICATIONS),
)
def test_an_unmodelled_commonjs_publication_records_nothing(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(tmp_path, {**COMMONJS_FILES, "barrel.js": barrel})
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _EXTERNAL, "r.Svc")}, edges
    assert "svc.Svc.go" not in _exact_calls(edges, "user.use"), edges


# A CommonJS barrel passes on only what it publishes (CodeRabbit, PR #3355):
# `Hidden` is required for the barrel's own use and never assigned to its
# exports, so `r.Hidden` names nothing first-party.
CJS_LIB_JS = (
    "class Public { go() { return 1 } }\n"
    "class Hidden { go() { return 2 } }\n"
    "module.exports = { Public, Hidden }\n"
)

CJS_PUBLIC_HIDDEN_USER_JS = (
    "const r = require('./barrel')\n"
    "const { Public, Hidden } = require('./barrel')\n"
    "class P extends r.Public { run() { return this.go() } }\n"
    "class H extends r.Hidden { run() { return this.go() } }\n"
    "function usePublic() { const p = new Public(); return p.go() }\n"
    "function useHidden() { const h = new Hidden(); return h.go() }\n"
    "module.exports = { P, H, usePublic, useHidden }\n"
)

_REQUIRE_LIB = "const { Public, Hidden } = require('./lib')\n"

_PUBLISHING_BARRELS = {
    "object-literal": (
        "barrel.js",
        f"{_REQUIRE_LIB}const check = (x) => x instanceof Hidden\n"
        "module.exports = { Public, check }\n",
    ),
    "renamed-binding-under-a-string-key": (
        "barrel.js",
        "const { Public: Shown, Hidden } = require('./lib')\n"
        "module.exports = { 'Public': Shown }\n",
    ),
    "exports-member": ("barrel.js", f"{_REQUIRE_LIB}exports.Public = Public\n"),
    "module-exports-member": (
        "barrel.js",
        f"{_REQUIRE_LIB}module.exports.Public = Public\n",
    ),
    "exports-string-key": ("barrel.js", f"{_REQUIRE_LIB}exports['Public'] = Public\n"),
    "frozen": (
        "barrel.js",
        f"{_REQUIRE_LIB}Object.freeze(module.exports = {{ Public }})\n",
    ),
    "object-assign": (
        "barrel.js",
        f"{_REQUIRE_LIB}Object.assign(module.exports, {{ Public }})\n",
    ),
    # Replacing `module.exports` discards what the old object held
    # (CodeRabbit, PR #3355), and leaves `exports` on that old object.
    "exports-member-before-a-replacement": (
        "barrel.js",
        f"{_REQUIRE_LIB}exports.Hidden = Hidden\nmodule.exports = {{ Public }}\n",
    ),
    "exports-member-after-a-replacement": (
        "barrel.js",
        f"{_REQUIRE_LIB}module.exports = {{ Public }}\nexports.Hidden = Hidden\n",
    ),
    # A second replacement discards the first (Greptile, PR #3355).
    "replaced-twice": (
        "barrel.js",
        f"{_REQUIRE_LIB}module.exports = {{ Hidden }}\nmodule.exports = {{ Public }}\n",
    ),
    # `exports = module.exports = ...` keeps `exports` on the new object.
    "re-aliased-exports": (
        "barrel.js",
        f"{_REQUIRE_LIB}exports = module.exports = function app() {{}}\n"
        "exports.Public = Public\n",
    ),
    "export-assignment": (
        "barrel.ts",
        "import { Public, Hidden } from './lib'\nexport = { Public }\n",
    ),
}


@pytest.mark.parametrize(
    "barrel", list(_PUBLISHING_BARRELS.values()), ids=list(_PUBLISHING_BARRELS)
)
def test_a_commonjs_barrel_passes_on_only_the_names_it_publishes(
    tmp_path: Path, barrel: tuple[str, str]
) -> None:
    name, body = barrel
    edges = _index(
        tmp_path,
        {"lib.js": CJS_LIB_JS, name: body, "user.js": CJS_PUBLIC_HIDDEN_USER_JS},
    )
    assert _heritage(edges, "user.P") == {(_INHERITS, _CLASS, "lib.Public")}, edges
    assert _exact_calls(edges, "user.usePublic") == {"lib.Public.go"}, edges
    assert _heritage(edges, "user.H") == {(_INHERITS, _EXTERNAL, "r.Hidden")}, edges
    assert "lib.Hidden.go" not in _exact_calls(edges, "user.useHidden"), edges


# A whole value assigned to the exports names no binding per member: a
# missing edge, never one to a binding the barrel keeps to itself. None of
# these resolved before either.
_OPAQUE_BARRELS = {
    "require-of-a-module": "module.exports = require('./lib')\n",
    "spread-of-a-require": "module.exports = { ...require('./lib') }\n",
    "module-alias": "const lib = require('./lib')\nmodule.exports = lib\n",
    # An opaque replacement discards the names published before it.
    "replaced-by-a-require": (
        f"{_REQUIRE_LIB}exports.Public = Public\nexports.Hidden = Hidden\n"
        "module.exports = require('./lib')\n"
    ),
}


@pytest.mark.parametrize(
    "barrel", list(_OPAQUE_BARRELS.values()), ids=list(_OPAQUE_BARRELS)
)
def test_an_opaque_commonjs_publication_passes_on_nothing(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(
        tmp_path,
        {
            "lib.js": CJS_LIB_JS,
            "barrel.js": barrel,
            "user.js": CJS_PUBLIC_HIDDEN_USER_JS,
        },
    )
    assert _heritage(edges, "user.P") == {(_INHERITS, _EXTERNAL, "r.Public")}, edges
    assert _heritage(edges, "user.H") == {(_INHERITS, _EXTERNAL, "r.Hidden")}, edges


# What a module's import statements say is not what it exports (Greptile, PR
# #3355): a TypeScript barrel that `import type`s, imports a value, or exports
# only types, and publishes through `module.exports` or `export =`, is still a
# CommonJS barrel whose bindings are its exports.
TS_COMMONJS_USER_TS = """\
import * as r from './barrel'
import { Svc } from './barrel'
export class Sub extends r.Svc { run(): number { return this.go() } }
export function use(): number { const s = new Svc(); return s.go() }
"""

_TS_COMMONJS_BARRELS = {
    "type-only-import": (
        "import type { T } from './types'\n"
        "const { Svc } = require('./svc')\nmodule.exports = { Svc }\n"
    ),
    "value-import": (
        "import { helper } from './helper'\nconst { Svc } = require('./svc')\n"
        "helper()\nmodule.exports = { Svc }\n"
    ),
    "type-only-export": (
        "export interface Opts { x: number }\n"
        "const { Svc } = require('./svc')\nmodule.exports = { Svc }\n"
    ),
    "export-assignment": "import { Svc } from './svc'\nexport = { Svc }\n",
    # Every specifier `type`: erased like `export type { T }` (CodeRabbit,
    # PR #3355).
    "inline-type-only-export": (
        "import type { T } from './types'\nexport { type T }\n"
        "const { Svc } = require('./svc')\nmodule.exports = { Svc }\n"
    ),
}


def _ts_barrel_files(barrel: str) -> dict[str, str]:
    return {
        "svc.ts": "export class Svc { go(): number { return 1 } }\n",
        "types.ts": "export interface T { x: number }\n",
        "helper.ts": "export function helper(): number { return 2 }\n",
        "barrel.ts": barrel,
        "user.ts": TS_COMMONJS_USER_TS,
    }


@pytest.mark.parametrize(
    "barrel", list(_TS_COMMONJS_BARRELS.values()), ids=list(_TS_COMMONJS_BARRELS)
)
def test_a_typescript_commonjs_barrel_is_followed(tmp_path: Path, barrel: str) -> None:
    edges = _index(tmp_path, _ts_barrel_files(barrel))
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _CLASS, "svc.Svc")}, edges
    assert _exact_calls(edges, "user.use") == {"svc.Svc.go"}, edges
    assert _exact_calls(edges, "user.Sub.run") == {"svc.Svc.go"}, edges


_EXPORT_TABLE_BARRELS = {
    # An ES export makes the export table the module's whole API, a
    # type-only import beside it notwithstanding.
    "es-export-beside-a-type-only-import": (
        "import type { T } from './types'\nimport { Svc } from './svc'\n"
        "export function make(t: T): Svc { return new Svc() }\n"
    ),
    # Both systems: under ES module semantics `module` is not the export
    # object, and following the bindings could pass on a private import.
    "es-export-beside-module-exports": (
        "export const version = 1\nconst { Svc } = require('./svc')\n"
        "module.exports = { Svc }\n"
    ),
    # Neither: the module exports nothing, so nothing it binds is a member.
    "import-only": "import { Svc } from './svc'\nnew Svc().go()\n",
    "require-only": "const { Svc } = require('./svc')\nnew Svc().go()\n",
    # One value specifier makes the clause a value export.
    "inline-type-beside-a-value-specifier": (
        "import { T } from './types'\nconst { Svc } = require('./svc')\n"
        "const U = 1\nexport { type T, U }\nmodule.exports = { Svc }\n"
    ),
    # Only an assignment that runs at load publishes; the text `module.exports`
    # in a string (Greptile, PR #3355), a comment or a function body does not.
    "module-exports-in-a-string": (
        "import { Svc } from './svc'\nconsole.log('module.exports')\n"
    ),
    "module-exports-in-a-comment": (
        "import { Svc } from './svc'\n// module.exports = { Svc }\n"
    ),
    "module-exports-in-a-function-body": (
        "import { Svc } from './svc'\nfunction later() { module.exports = { Svc } }\n"
    ),
}


@pytest.mark.parametrize(
    "barrel", list(_EXPORT_TABLE_BARRELS.values()), ids=list(_EXPORT_TABLE_BARRELS)
)
def test_a_barrel_without_commonjs_exports_passes_on_only_its_es_exports(
    tmp_path: Path, barrel: str
) -> None:
    edges = _index(tmp_path, _ts_barrel_files(barrel))
    assert _heritage(edges, "user.Sub") == {(_INHERITS, _EXTERNAL, "r.Svc")}, edges
    assert "svc.Svc.go" not in _exact_calls(edges, "user.use"), edges


@pytest.mark.parametrize(
    ("source", "commonjs"),
    [
        ("module.exports = { Svc }\n", True),
        ("exports.Svc = Svc\n", True),
        ("exports['Svc'] = Svc\n", True),
        ("const S = (module.exports = { Svc })\n", True),
        ("if (ok) { module.exports = { Svc } }\n", True),
        ("Object.freeze(module.exports = { Svc })\n", True),
        ("Object.assign(module.exports, { Svc })\n", True),
        ("Object.assign(exports, { Svc })\n", True),
        ("(0, module.exports = { Svc })\n", True),
        ("ok ? (module.exports = { Svc }) : null\n", True),
        ("(function () { module.exports = { Svc } })()\n", True),
        ("(() => { exports.Svc = Svc })()\n", True),
        ("!function () { module.exports = {} }()\n", True),
        ("(function () { exports.Svc = Svc }).call(this)\n", True),
        ("setTimeout(() => { module.exports = { Svc } })\n", False),
        ("const later = function () { module.exports = {} }\n", False),
        ("Object.assign({}, module.exports)\n", False),
        ("(module.exports) = { Svc }\n", True),
        ("(function (exports) { exports.Svc = Svc })({})\n", False),
        ("(function (module) { module.exports = {} })({})\n", False),
        # Re-pointing `exports` alone publishes nothing.
        ("exports = { Svc }\n", False),
        ("exports = module.exports = app\n", True),
        ("console.log('module.exports')\n", False),
        ("// module.exports = { Svc }\n", False),
        ("/* exports.Svc = Svc */\n", False),
        ("function later() { module.exports = {} }\n", False),
        ("const later = () => { exports.Svc = Svc }\n", False),
        ("const m = module.exports\n", False),
        ("console.log(module.exports)\n", False),
        ("export = { Svc }\n", True),
        ("import type { T } from './t'\nmodule.exports = {}\n", True),
        ("import { T } from './t'\nmodule.exports = { T }\n", True),
        ("export interface I { x: number }\nmodule.exports = {}\n", True),
        ("export type A = number\nmodule.exports = {}\n", True),
        ("export type { T } from './t'\nmodule.exports = {}\n", True),
        ("export { type T }\nmodule.exports = {}\n", True),
        ("export { type T } from './t'\nmodule.exports = {}\n", True),
        # `export {}` names no value either.
        ("export {}\nmodule.exports = {}\n", True),
        ("export { type T, U }\nmodule.exports = {}\n", False),
        ("export const v = 1\nmodule.exports = {}\n", False),
        ("export default 1\nmodule.exports = {}\n", False),
        ("export * from './a'\nmodule.exports = {}\n", False),
        ("export { Svc }\nmodule.exports = {}\n", False),
        ("import { Svc } from './svc'\n", False),
        ("const { Svc } = require('./svc')\n", False),
        ("export class Svc {}\n", False),
    ],
)
def test_a_module_is_commonjs_only_by_what_it_exports(
    source: str, commonjs: bool
) -> None:
    parser = load_parsers()[0][cs.SupportedLanguage.TS]
    assert is_commonjs_module(parser.parse(source.encode()).root_node) is commonjs


# Every name these sources publish is bound at the top by a `require`.
_PUBLISHED_HEADER = "const { A, B, C, a, b, h, p, r, x, y, S } = require('./m');\n"


@pytest.mark.parametrize(
    ("source", "published"),
    [
        # The modelled forms.
        (
            "module.exports = { A, B: x, 'C': y }\n",
            {"A": "A", "B": "x", "C": "y"},
        ),
        ("exports.X = y\n", {"X": "y"}),
        ("module.exports.X = y\n", {"X": "y"}),
        ("exports['X'] = y\n", {"X": "y"}),
        ("module.exports['X'] = y\n", {"X": "y"}),
        ("export = { A, B: x }\n", {"A": "A", "B": "x"}),
        ("Object.freeze(module.exports = { A })\n", {"A": "A"}),
        (
            "Object.assign(module.exports, { A, B: x }, { C })\n",
            {"A": "A", "B": "x", "C": "C"},
        ),
        ("(function () { exports.X = y })()\n", {"X": "y"}),
        ("exports.A = a, exports.B = b\n", {"A": "a", "B": "b"}),
        # Opaque values publish nothing.
        ("module.exports = lib\n", {}),
        ("module.exports = require('./x')\n", {}),
        ("module.exports = class Svc {}\n", {}),
        ("module.exports = { [key]: v }\n", {}),
        ("module.exports = { run() {} }\n", {}),
        ("module.exports = { ...require('./a'), A }\n", {}),
        ("exports.X = require('./x').X\n", {}),
        ("exports.X = function () {}\n", {}),
        ("export = lib\n", {}),
        ("Object.assign(exports, require('./x'))\n", {}),
        # Not a load-time publication, or none at all.
        ("console.log('exports.X = y')\n", {}),
        ("const m = module.exports.X\n", {}),
        # Publication order: a replacement of `module.exports` discards the
        # old object and leaves `exports` on it; `exports` and
        # `module.exports` are references to objects.
        ("exports.Hidden = h\nmodule.exports = { A }\n", {"A": "A"}),
        ("module.exports = { A }\nexports.X = x\n", {"A": "A"}),
        ("module.exports = { A }\nmodule.exports.B = b\n", {"A": "A", "B": "b"}),
        ("module.exports = {}\nObject.assign(module.exports, { A })\n", {"A": "A"}),
        ("module.exports = {}\nObject.assign(exports, { A })\n", {}),
        ("exports.A = a\nexports.B = b\n", {"A": "a", "B": "b"}),
        ("exports.A = exports.B = a\n", {"A": "a", "B": "a"}),
        ("exports.A = a\nmodule.exports = require('./x')\n", {}),
        ("Object.freeze(module.exports = { A })\nexports.X = x\n", {"A": "A"}),
        ("(function () { exports.H = h; module.exports = { A } })()\n", {"A": "A"}),
        ("exports = module.exports = app\nexports.R = r\n", {"R": "r"}),
        ("module.exports = exports = { A }\nexports.B = b\n", {"A": "A", "B": "b"}),
        ("exports = {}\nexports.X = x\n", {}),
        ("exports.X = x\nexports.X = function () {}\n", {}),
        ("export = { A }\nexports.X = x\n", {"A": "A"}),
        (
            "module.exports = { Shown: a }\nmodule.exports = { Other: b }\n",
            {"Other": "b"},
        ),
        ("exports.A = a\nObject.assign(module.exports, { A: lib.A })\n", {}),
        ("module.exports = {}\nexports = module.exports\nexports.A = a\n", {"A": "a"}),
        ("exports.A = a\nmodule.exports = exports\n", {"A": "a"}),
        ("exports = { A }\nmodule.exports = exports\n", {"A": "A"}),
        ("exports = {}\nexports.A = a\nmodule.exports = exports\n", {"A": "a"}),
        ("exports.self = exports\n", {}),
        # Parentheses on either side change nothing.
        ("(module.exports = { A })\n", {"A": "A"}),
        ("module.exports = ({ A })\n", {"A": "A"}),
        ("(module.exports) = { A }\n", {"A": "A"}),
        ("(exports.X) = y\n", {"X": "y"}),
        ("exports.X = (y)\n", {"X": "y"}),
        ("module.exports = { A: (a) }\n", {"A": "a"}),
        ("Object.assign(module.exports, ({ A }))\n", {"A": "A"}),
        # A name a scope enclosing the publication binds is not the module's.
        ("(function (S) { exports.P = S })(o)\n", {}),
        ("(function ({ S }) { exports.P = S })(o)\n", {}),
        ("(S => { exports.P = S })(o)\n", {}),
        ("(function () { const S = 1; exports.P = S })()\n", {}),
        ("(function () { exports.P = S; var S = 1 })()\n", {}),
        ("(function () { module.exports = { S }; let S = 1 })()\n", {}),
        ("(function S() { exports.P = S })()\n", {}),
        ("(function () { if (1) { var S = 0 } exports.P = S })()\n", {}),
        ("(function () { { function S() {} } exports.P = S })()\n", {}),
        ("(function () { for (var S of xs) {} exports.P = S })()\n", {}),
        ("(function (exports) { exports.P = p })({})\n", {}),
        ("(function (module) { module.exports = { P: p } })({})\n", {}),
        # A closed scope keeps its names.
        ("(function () { exports.P = S })()\n", {"P": "S"}),
        ("(function () { function f(S) {} exports.P = S })()\n", {"P": "S"}),
        ("(() => { { const S = 0 } exports.P = S })()\n", {"P": "S"}),
        ("(() => { if (1) { class S {} } exports.P = S })()\n", {"P": "S"}),
        ("(() => { for (let S of xs) {} exports.P = S })()\n", {"P": "S"}),
        ("{ const S = 0 }\nexports.P = S\n", {"P": "S"}),
        # Outside the modelled subset the module records nothing at all
        # (Greptile, PR #3355): a write in a condition, a loop, a `switch`, a
        # `try`, a function body, a nested value, a computed key or an
        # assignment target's sub-expression.
        ("exports.A = a\nif (ok) { exports.B = b }\n", {}),
        ("exports.A = a\ntry {} catch (S) { exports.P = S }\n", {}),
        ("exports.A = a\nfor (const S of xs) { exports.P = S }\n", {}),
        (
            "exports.A = a\n(() => { switch (1) { case 1: "
            "const S = 0; exports.P = S } })()\n",
            {},
        ),
        ("exports.A = a\nfunction later() { exports.X = y }\n", {}),
        ("exports.A = a\nsetTimeout(() => { exports.X = y })\n", {}),
        ("module.exports = { A, side: (module.exports = {}) }\n", {}),
        ("module.exports.A = (module.exports = {}, a)\n", {}),
        ("exports.A = a\nexports[key] = b\n", {}),
        ("holder[(module.exports = {}, 'x')] = exports.P = p\n", {}),
        ("cond && (module.exports = { A })\n", {}),
        # Every other reference must read: `exports` aliased, passed, deleted,
        # called through at load, the module's `this`, `eval`.
        ("exports.A = a\nconst e = exports\ne.B = b\n", {}),
        ("exports.A = a\ninit(module)\n", {}),
        ("exports.A = a\ndelete exports.A\n", {}),
        ("exports.A = a\nexports.A()\n", {}),
        ("exports.A = a\nthis.B = b\n", {}),
        ("exports.A = a\neval('')\n", {}),
        # Reads leave the module's exports known.
        (
            "exports.A = a\nexports.B = function () { return exports.A() }\n",
            {"A": "a"},
        ),
        ("exports.A = a\nif (require.main === module) { a() }\n", {"A": "a"}),
        ("exports.A = a\nif (typeof module !== 'undefined') {}\n", {"A": "a"}),
    ],
)
def test_a_commonjs_module_publishes_only_load_time_named_bindings(
    source: str, published: dict[str, str]
) -> None:
    parser = load_parsers()[0][cs.SupportedLanguage.TS]
    root = parser.parse((_PUBLISHED_HEADER + source).encode()).root_node
    found = commonjs_export_bindings(root)
    assert {name: b.binding for name, b in found.items()} == published


@pytest.mark.parametrize(
    ("source", "published"),
    [
        # A top-level `require` leads to what it imports.
        ("const { K } = require('./k')\nexports.P = K\n", {"P": ("K", True)}),
        ("import { K } from './k'\nexport = { P: K }\n", {"P": ("K", True)}),
        # A top-level declaration of the module's own is followed in the
        # registry.
        ("class K {}\nexports.P = K\n", {"P": ("K", False)}),
        ("function K() {}\nmodule.exports = { K }\n", {"K": ("K", False)}),
        # A nested `require` of the same name could stand in for it in the
        # import map (Greptile, PR #3355).
        (
            "class K {}\nfunction later() { const { K } = require('./o') }\n"
            "module.exports = { P: K }\n",
            {},
        ),
        (
            "const { K } = require('./k')\nfunction later() { const { K } = "
            "require('./o') }\nexports.P = K\n",
            {},
        ),
        # A nested class of the same name only matters for the registry.
        (
            "const { K } = require('./k')\n;(() => { { class K {} } })()\n"
            "exports.P = K\n",
            {"P": ("K", True)},
        ),
        ("const K = 1\n;(() => { class K {} })()\nexports.P = K\n", {}),
        # A name declared twice, reassigned, or not declared at all.
        ("var K = 1\nvar K = 2\nexports.P = K\n", {}),
        ("let K = require('./k').K\nK = class Other {}\nexports.P = K\n", {}),
        ("var K = 1\nif (ok) { var K = 2 }\nexports.P = K\n", {}),
        ("exports.P = undeclared\n", {}),
        ("exports.P = require\n", {}),
    ],
)
def test_a_published_name_is_kept_only_when_its_binding_is_certain(
    source: str, published: dict[str, tuple[str, bool]]
) -> None:
    parser = load_parsers()[0][cs.SupportedLanguage.TS]
    found = commonjs_export_bindings(parser.parse(source.encode()).root_node)
    assert {name: (b.binding, b.imported) for name, b in found.items()} == published


# --- a module reached by two star paths is walked on each ------------------

BASE_TS = "export class Base { go(): number { return 1 } }\n"

STAR_USER_TS = """\
import { Base } from './index'
import * as r from './index'
export class Sub extends Base { run(): number { return this.go() } }
export class NsSub extends r.Base { run(): number { return this.go() } }
export function use(): number { const b = new Base(); return b.go() }
"""

# Issue scenario: `a` and `b` both reach `mid`, which star-exports `impl`.
# `a` also star-exports `other`, whose `Base` conflicts with it, so `a`
# exports no `Base`; `b` exports `impl.Base`, the one `index` passes on.
SHARED_STAR_FILES = {
    "impl.ts": BASE_TS,
    "other.ts": BASE_TS,
    "mid.ts": "export * from './impl'\n",
    "a.ts": "export * from './mid'\nexport * from './other'\n",
    "b.ts": "export * from './mid'\n",
    "index.ts": "export * from './a'\nexport * from './b'\n",
    "user.ts": STAR_USER_TS,
}


def test_a_star_path_through_a_shared_module_keeps_its_export(
    tmp_path: Path,
) -> None:
    edges = _index(tmp_path, SHARED_STAR_FILES)
    for child in ("user.Sub", "user.NsSub"):
        assert _heritage(edges, child) == {(_INHERITS, _CLASS, "impl.Base")}, edges
    for caller in ("user.use", "user.Sub.run", "user.NsSub.run"):
        assert _exact_calls(edges, caller) == {"impl.Base.go"}, (caller, edges)


def _first_party_bases(edges: list[_Edge]) -> set[str]:
    return {
        e.target
        for e in edges
        if e.source in ("user.Sub", "user.NsSub")
        and e.rel == _INHERITS
        and e.label != _EXTERNAL
    }


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {
                "index.ts": "export * from './a'\nexport * from './impl'\n",
                "a.ts": "export * from './index'\n",
            },
            {"impl.Base"},
            id="cycle-still-resolves",
        ),
        pytest.param(
            {
                "index.ts": "export * from './a'\n",
                "a.ts": "export * from './index'\n",
            },
            set(),
            id="cycle-without-the-name-terminates",
        ),
        pytest.param(
            {
                "index.ts": "export * from './a'\nexport * from './b'\n",
                "a.ts": "export * from './impl'\n",
                "b.ts": "export * from './other'\n",
            },
            set(),
            id="two-exported-targets-stay-ambiguous",
        ),
        pytest.param(
            {
                "index.ts": "export * from './a'\nexport * from './b'\n",
                "a.ts": "export * from './hidden'\nexport * from './mid'\n",
                "b.ts": "export * from './mid'\n",
                "hidden.ts": (
                    "class Base { go(): number { return 3 } }\nexport const z = 1\n"
                ),
                "mid.ts": "export * from './impl'\n",
            },
            {"impl.Base"},
            id="a-private-definition-is-still-ignored",
        ),
        pytest.param(
            {
                "index.ts": "export * from './a'\nexport * from './b'\n",
                "a.ts": "export * from './mid'\n",
                "b.ts": "export * from './mid'\n",
                "mid.ts": "export * from './impl'\n",
            },
            {"impl.Base"},
            id="one-binding-on-two-paths-is-no-conflict",
        ),
    ],
)
def test_star_paths_keep_their_rules(
    tmp_path: Path, files: dict[str, str], expected: set[str]
) -> None:
    edges = _index(
        tmp_path,
        {"impl.ts": BASE_TS, "other.ts": BASE_TS, "user.ts": STAR_USER_TS, **files},
    )
    assert _first_party_bases(edges) == expected, edges


def _registry(*qns: str) -> FunctionRegistryTrie:
    registry = FunctionRegistryTrie()
    for qn in qns:
        registry[qn] = NodeType.CLASS
    return registry


def _stars(*modules: str) -> dict[str, str]:
    return {f"{cs.GLOB_ALL}{module}": module for module in modules}


class TestFollowReexportsStarPaths:
    # The language-neutral walk (Python's `from .mod import *` too) had the
    # same shared visited set.

    def test_a_shared_module_is_walked_on_each_path(self) -> None:
        mapping = {
            "p.index": _stars("p.a", "p.b"),
            "p.a": _stars("p.mid", "p.other"),
            "p.b": _stars("p.mid"),
            "p.mid": _stars("p.impl"),
        }
        registry = _registry("p.impl.Base", "p.other.Base")
        assert follow_reexports("p.index.Base", mapping, registry) == "p.impl.Base"

    def test_a_cycle_back_to_the_start_terminates(self) -> None:
        mapping = {"p.index": _stars("p.a"), "p.a": _stars("p.index")}
        registry = _registry("p.other.Base")
        assert follow_reexports("p.index.Base", mapping, registry) == "p.index.Base"

    def test_two_exported_targets_stay_ambiguous(self) -> None:
        mapping = {
            "p.index": _stars("p.a", "p.b"),
            "p.a": _stars("p.mid"),
            "p.b": _stars("p.other"),
            "p.mid": _stars("p.impl"),
        }
        registry = _registry("p.impl.Base", "p.other.Base")
        assert follow_reexports("p.index.Base", mapping, registry) == "p.index.Base"

    def test_layered_shared_modules_are_walked_once_each(self) -> None:
        # Twenty layers of two modules that both star-export the next layer
        # reach the bottom along 2**20 paths; each module is still settled
        # once per lookup.
        layers = 20
        mapping: dict[str, dict[str, str]] = {
            "p.index": _stars("p.l0a", "p.l0b"),
        }
        for layer in range(layers):
            below = (
                _stars(f"p.l{layer + 1}a", f"p.l{layer + 1}b")
                if layer + 1 < layers
                else _stars("p.impl")
            )
            mapping[f"p.l{layer}a"] = below
            mapping[f"p.l{layer}b"] = below
        registry = _CountingRegistry("p.impl.Base")
        assert follow_reexports("p.index.Base", mapping, registry) == "p.impl.Base"
        assert registry.lookups < 50 * layers, registry.lookups


class _CountingRegistry(FunctionRegistryTrie):
    def __init__(self, *qns: str) -> None:
        super().__init__()
        for qn in qns:
            self[qn] = NodeType.CLASS
        self.lookups = 0

    def __contains__(self, qualified_name: str) -> bool:
        self.lookups += 1
        return super().__contains__(qualified_name)


def _js_follow(
    qn: str,
    imports: dict[str, dict[str, str]],
    registry: FunctionRegistryTrie,
    exports: dict[str, dict[str, JsExport]] | None = None,
) -> str:
    modules = set(imports) | {
        qn.rpartition(cs.SEPARATOR_DOT)[0] for qn, _kind in registry.items()
    }
    paths = {module: Path(f"{module}.ts") for module in modules}
    return follow_js_reexports(qn, imports, exports or {}, paths, registry)


class TestFollowJsReexportsStarPaths:
    def test_a_shared_module_is_walked_on_each_path(self) -> None:
        imports = {
            "p.index": _stars("p.a", "p.b"),
            "p.a": _stars("p.mid", "p.other"),
            "p.b": _stars("p.mid"),
            "p.mid": _stars("p.impl"),
        }
        registry = _registry("p.impl.Base", "p.other.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.impl.Base"

    def test_a_private_import_of_an_es_module_is_not_its_export(self) -> None:
        imports = {"p.index": {"Base": "p.impl.Base"}}
        registry = _registry("p.impl.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.index.Base"

    def test_a_published_name_leads_to_the_binding_it_names(self) -> None:
        # `module.exports = { Base: Impl }` records `Base` as the binding
        # `Impl`; the barrel's other binding is not an export.
        imports = {"p.index": {"Impl": "p.impl.Base", "Other": "p.impl.Other"}}
        registry = _registry("p.impl.Base", "p.impl.Other")
        exports = {"p.index": {"Base": JsExport("p.index.Impl", local=True)}}
        assert _js_follow("p.index.Base", imports, registry, exports) == "p.impl.Base"
        assert (
            _js_follow("p.index.Other", imports, registry, exports) == "p.index.Other"
        )

    def test_a_private_declaration_in_a_star_source_is_not_passed_on(self) -> None:
        imports = {"p.index": _stars("p.a", "p.b")}
        registry = _registry("p.a.Base", "p.b.Base")
        registry.mark_module_private("p.a.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.b.Base"

    def test_two_exported_targets_stay_ambiguous(self) -> None:
        imports = {"p.index": _stars("p.a", "p.b")}
        registry = _registry("p.a.Base", "p.b.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.index.Base"

    def test_a_cycle_back_to_the_start_terminates(self) -> None:
        imports = {"p.index": _stars("p.a"), "p.a": _stars("p.index")}
        registry = _registry("p.other.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.index.Base"

    def test_layered_shared_barrels_are_walked_once_each(self) -> None:
        layers = 20
        imports: dict[str, dict[str, str]] = {"p.index": _stars("p.l0a", "p.l0b")}
        for layer in range(layers):
            below = (
                _stars(f"p.l{layer + 1}a", f"p.l{layer + 1}b")
                if layer + 1 < layers
                else _stars("p.impl")
            )
            imports[f"p.l{layer}a"] = below
            imports[f"p.l{layer}b"] = below
        registry = _CountingRegistry("p.impl.Base")
        assert _js_follow("p.index.Base", imports, registry) == "p.impl.Base"
        assert registry.lookups < 50 * layers, registry.lookups
