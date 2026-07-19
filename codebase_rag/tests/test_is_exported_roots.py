from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _exported_by_qn(mock: MagicMock) -> dict[str, bool]:
    # (H) qualified_name -> is_exported for every Function/Method node ingested.
    out: dict[str, bool] = {}
    for c in mock.ensure_node_batch.call_args_list:
        label, props = c.args[0], c.args[1]
        if label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            out[props[cs.KEY_QUALIFIED_NAME]] = props.get(cs.KEY_IS_EXPORTED, False)
    return out


def _run(tmp_path: Path, files: dict[str, str]) -> dict[str, bool]:
    parsers, queries = load_parsers()
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    return _exported_by_qn(mock)


def _one(exported: dict[str, bool], suffix: str) -> bool:
    matches = [qn for qn in exported if qn.endswith(suffix)]
    assert matches, f"no node ending with {suffix!r}; have {sorted(exported)}"
    return exported[matches[0]]


PY_SRC = """\
class SRGExporter:
    def __init__(self):
        self.x = 1

    def to_srg(self):
        return self._predicates()

    def _predicates(self):
        return 1


def parse(s):
    return _extract(s)


def _extract(s):
    return s
"""

GO_SRC = """\
package main

type Msg struct{}

func (m *Msg) Reset() {}

func (m *Msg) internal() {}

func ExportedFunc() int { return unexportedFunc() }

func unexportedFunc() int { return 1 }
"""

TS_SRC = """\
export function useThing() {
    return 1;
}

function localHelper() {
    return 2;
}

export const Widget = () => 3;
"""

TS_EXPORT_LIST_SRC = """\
function handler() {
    return 1;
}

function renamed() {
    return 2;
}

function neverExported() {
    return 3;
}

export { handler };
export { renamed as publicRenamed };
"""


TS_CLASS_ACCESS_SRC = """\
export class ApiClass {
    publicMethod() {
        return 1;
    }

    private privateMethod() {
        return 2;
    }

    protected protectedMethod() {
        return 3;
    }
}
"""


JS_CLASS_HASH_PRIVATE_SRC = """\
export class Store {
    read() {
        return this.#load();
    }

    #load() {
        return 1;
    }
}
"""


RUST_SRC = """\
pub fn public_api() -> i32 {
    crate_only()
}

pub(crate) fn crate_only() -> i32 {
    1
}

pub(super) fn parent_only() -> i32 {
    2
}

fn private_helper() -> i32 {
    3
}
"""


def test_python_public_symbols_are_exported(tmp_path: Path) -> None:
    if "python" not in load_parsers()[0]:
        pytest.skip("python parser not available")
    exported = _run(tmp_path, {"srg.py": PY_SRC})
    assert _one(exported, ".to_srg") is True
    assert _one(exported, ".parse") is True
    assert _one(exported, ".__init__") is True  # (H) dunder: runtime-invoked
    assert _one(exported, "._predicates") is False
    assert _one(exported, "._extract") is False


def test_nested_python_function_is_not_a_root(tmp_path: Path) -> None:
    # (H) A function nested inside another function is a local closure, never public
    # (H) API, so it must not be seeded as a reachability root even if its name is
    # (H) public -- otherwise an unreachable outer function's helpers look live.
    src = "def outer():\n    def helper():\n        return 1\n    return helper()\n"
    exported = _run(tmp_path, {"m.py": src})
    assert _one(exported, ".outer") is True
    assert _one(exported, ".outer.helper") is False


def test_go_capitalized_symbols_are_exported(tmp_path: Path) -> None:
    if "go" not in load_parsers()[0]:
        pytest.skip("go parser not available")
    exported = _run(tmp_path, {"msg.go": GO_SRC})
    assert _one(exported, ".ExportedFunc") is True
    assert _one(exported, ".Reset") is True
    assert _one(exported, ".unexportedFunc") is False
    assert _one(exported, ".internal") is False


def test_ts_export_keyword_marks_exported(tmp_path: Path) -> None:
    if "typescript" not in load_parsers()[0]:
        pytest.skip("typescript parser not available")
    exported = _run(tmp_path, {"widget.ts": TS_SRC})
    assert _one(exported, ".useThing") is True
    assert _one(exported, ".Widget") is True
    assert _one(exported, ".localHelper") is False


def test_ts_export_list_marks_exported(tmp_path: Path) -> None:
    # (H) A separate `export { handler }` list (and `export { x as y }` rename) is
    # (H) the common JS/TS export form: the declaration is not wrapped by `export`,
    # (H) so its name must be matched against module-level export clauses instead.
    if "typescript" not in load_parsers()[0]:
        pytest.skip("typescript parser not available")
    exported = _run(tmp_path, {"api.ts": TS_EXPORT_LIST_SRC})
    assert _one(exported, ".handler") is True
    assert _one(exported, ".renamed") is True  # (H) exported under an alias
    assert _one(exported, ".neverExported") is False


def test_ts_private_method_of_exported_class_is_not_exported(tmp_path: Path) -> None:
    # (H) A `private` method is not part of the class's public API even when the
    # (H) class is exported, so it must not seed a reachability root; `protected`
    # (H) stays exported (an inheritance surface, mirroring the Java rule).
    if "typescript" not in load_parsers()[0]:
        pytest.skip("typescript parser not available")
    exported = _run(tmp_path, {"api.ts": TS_CLASS_ACCESS_SRC})
    assert _one(exported, ".ApiClass.publicMethod") is True
    assert _one(exported, ".ApiClass.privateMethod") is False
    assert _one(exported, ".ApiClass.protectedMethod") is True


def test_rust_only_unrestricted_pub_is_exported(tmp_path: Path) -> None:
    # (H) Only bare `pub` is an external API root. `pub(crate)`/`pub(super)` are
    # (H) visible only within the crate/parent module, so an uncalled one is
    # (H) genuinely dead and must not be seeded as a reachability root.
    if "rust" not in load_parsers()[0]:
        pytest.skip("rust parser not available")
    exported = _run(tmp_path, {"m.rs": RUST_SRC})
    assert _one(exported, ".public_api") is True
    assert _one(exported, ".crate_only") is False
    assert _one(exported, ".parent_only") is False
    assert _one(exported, ".private_helper") is False


def test_js_hash_private_method_of_exported_class_is_not_exported(
    tmp_path: Path,
) -> None:
    # (H) An ECMAScript `#name` method is private to the class even when the class
    # (H) is exported, so it must not seed a reachability root.
    if "javascript" not in load_parsers()[0]:
        pytest.skip("javascript parser not available")
    exported = _run(tmp_path, {"store.js": JS_CLASS_HASH_PRIVATE_SRC})
    assert _one(exported, ".Store.read") is True
    assert _one(exported, ".Store.#load") is False


SCRIPT_JS_SRC = """\
class MapWidget {
    constructor(options) {
        this.options = options;
    }

    createMap() {
        return 1;
    }
}

function quickElement() {
    return 1;
}

function pageInit() {
    const localFn = function () {
        return 2;
    };
    return localFn();
}
"""

COMMONJS_SRC = """\
const util = require("./util");

function helper() {
    return util;
}
"""


def test_browser_script_top_level_symbols_are_exported(tmp_path: Path) -> None:
    # (H) A JS file with no import/export/require runs in page scope: every
    # (H) top-level declaration (and its class members) is a page-global the
    # (H) HTML can call, so it is a reachability root (django's OLMapWidget).
    exported = _run(tmp_path, {"widget.js": SCRIPT_JS_SRC})
    assert _one(exported, "widget.MapWidget.constructor") is True
    assert _one(exported, "widget.MapWidget.createMap") is True
    assert _one(exported, "widget.quickElement") is True
    assert _one(exported, "widget.pageInit") is True


def test_browser_script_function_locals_stay_private(tmp_path: Path) -> None:
    # (H) Declarations inside a function body are locals reached only through
    # (H) their enclosing scope, even in a page-scope script.
    exported = _run(tmp_path, {"widget.js": SCRIPT_JS_SRC})
    assert _one(exported, "pageInit.localFn") is False


def test_commonjs_module_symbols_stay_private(tmp_path: Path) -> None:
    # (H) A require() call marks the file as a CommonJS module, so an
    # (H) unexported top-level function is module-private, not a page-global.
    exported = _run(tmp_path, {"mod.js": COMMONJS_SRC})
    assert _one(exported, "mod.helper") is False


PROTO_SCRIPT_JS_SRC = """\
String.prototype.strptime = function (format) {
    return format;
};
"""

PROTO_COMMONJS_SRC = """\
const util = require("./util");

String.prototype.strptime = function (format) {
    return util.parse(format);
};
"""


def test_browser_script_prototype_method_is_exported(tmp_path: Path) -> None:
    # (H) A prototype-assigned method in a page-scope script (django admin's
    # (H) core.js String.prototype.strptime) extends a global builtin, so it is
    # (H) callable from any other script or template: a reachability root.
    exported = _run(tmp_path, {"core.js": PROTO_SCRIPT_JS_SRC})
    assert _one(exported, "core.String.strptime") is True


def test_commonjs_prototype_method_stays_private(tmp_path: Path) -> None:
    exported = _run(tmp_path, {"mod.js": PROTO_COMMONJS_SRC})
    assert _one(exported, "mod.String.strptime") is False


BLOCK_SCRIPT_JS_SRC = """\
{
    String.prototype.strptime = function (format) {
        return format;
    };
}

function helper() {
    return 1;
}
"""


def test_bare_block_in_script_is_not_a_scope_boundary(tmp_path: Path) -> None:
    # (H) django admin's core.js wraps its prototype extensions in bare
    # (H) top-level `{ ... }` blocks. A block is not a function scope: the
    # (H) prototype mutation still lands on the page-global builtin, so the
    # (H) method must stay a reachability root.
    exported = _run(tmp_path, {"core.js": BLOCK_SCRIPT_JS_SRC})
    assert _one(exported, "core.String.strptime") is True
    assert _one(exported, "core.helper") is True


def test_script_module_scan_runs_once_per_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # (H) The module-construct scan walks every top-level statement; it must
    # (H) run once per FILE (memoized on the tree root), not once per symbol,
    # (H) or export detection on a bundle with thousands of top-level
    # (H) declarations goes quadratic.
    from unittest.mock import patch

    from codebase_rag import constants as cs
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers import export_detection

    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.JS].parse(b"function a() {}\nfunction b() {}\n")
    declarations = [
        c for c in tree.root_node.children if c.type == cs.TS_FUNCTION_DECLARATION
    ]
    with patch.object(
        export_detection,
        "_is_module_construct",
        wraps=export_detection._is_module_construct,
    ) as spy:
        for declaration in declarations:
            assert export_detection._is_script_global(declaration) is True
    assert spy.call_count <= len(declarations)


DART_SRC = """\
void publicFn() {}
void _privateFn() {}

class Command {
  void run() {}
  void _wrap() {}
  Command.named() {}
}

class _Internal {
  void doThing() {}
}

extension on String {
  void shout() {}
}

extension PublicExt on String {
  void boom() {}
}
"""


def test_dart_visibility_seeds_exported_roots(tmp_path: Path) -> None:
    # (H) Dart privacy is purely lexical: a leading underscore on the symbol OR
    # (H) any enclosing type means library-private (not externally reachable),
    # (H) everything else is public API and must seed a dead-code root. Without
    # (H) this every public Dart symbol read as private and a library's whole
    # (H) public surface flagged dead (dart-lang/args: 89 false positives).
    exported = _run(tmp_path, {"lib.dart": DART_SRC})

    assert _one(exported, ".lib.publicFn") is True
    assert _one(exported, ".lib._privateFn") is False
    assert _one(exported, ".Command.run") is True
    assert _one(exported, ".Command._wrap") is False
    # (H) a public method of a PRIVATE class is not reachable from outside the
    # (H) library, so it is not an export root on its own
    assert _one(exported, "._Internal.doThing") is False
    # (H) an unnamed extension is visible only in its declaring library, so its
    # (H) members are not export roots; a public NAMED extension is importable
    assert _one(exported, ".shout") is False
    assert _one(exported, ".PublicExt.boom") is True
