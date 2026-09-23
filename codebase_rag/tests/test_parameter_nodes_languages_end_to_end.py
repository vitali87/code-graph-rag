"""Parameter nodes for the non-Python languages through a real index.

Issue #1804. `test_parameter_nodes_languages.py` proves each enumerator on a
tree-sitter node; this proves the node reaches the graph through each
language's own ingest path (the JS/TS module, the deferred C++ out-of-class
method, the Dart signature wrapper), that `OF_TYPE` resolves a declared type
to the project's class in every language, and that no Parameter is ever
emitted without its `HAS_PARAMETER` edge.

One fixture, twelve languages, one index: a language whose ingest path
bypassed the shared emitter would show up here as a file with no rows.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = {
    "go/go.mod": "module lib\n\ngo 1.21\n",
    "go/lib.go": (
        "package lib\n"
        "type Widget struct{}\n"
        "func Build(name string, w *Widget, opts ...int) int { return 0 }\n"
        "func (w *Widget) Use(x int) {}\n"
    ),
    "js/app.js": (
        "class Widget {}\n"
        "function build(name, w = null, ...rest) {}\n"
        "class Factory { make(w, {y}) {} static s(a) {} }\n"
        "const arrow = (p) => p;\n"
        "module.exports = { build };\n"
    ),
    "ts/app.ts": (
        "export class Widget {}\n"
        "export function build(this: Window, name: string, w: Widget,"
        " ...rest: string[]): void {}\n"
        "export class Factory { make(w: Widget): Widget { return w; } }\n"
        "const arrow = (p: number) => p;\n"
    ),
    "java/App.java": (
        "package app;\n"
        "class Widget {}\n"
        "class App { void build(String name, Widget w, int... xs) {}"
        " App(Widget w) {} }\n"
    ),
    "cs/App.cs": (
        "namespace App { class Widget {} class App {"
        " void Build(string name, Widget w, params int[] xs) {}"
        " App(Widget w) {} } }\n"
    ),
    "rust/Cargo.toml": '[package]\nname = "w"\nversion = "0.1.0"\n',
    "rust/src/lib.rs": (
        "pub struct Widget;\n"
        "pub fn build(name: &str, w: &Widget) {}\n"
        "impl Widget { pub fn make(&self, x: i32) {} }\n"
    ),
    "c/lib.c": (
        "struct widget { int a; };\n"
        "int build(const char *name, struct widget *w, ...) { return 0; }\n"
    ),
    "cpp/lib.cpp": (
        "class Widget {};\n"
        "int build(const std::string& name, Widget& w, int = 0) { return 0; }\n"
        "class Factory { void make(Widget& w); };\n"
        "void Factory::make(Widget& w) {}\n"
    ),
    "php/app.php": (
        "<?php\n"
        "class Widget {}\n"
        "function build(string $name, Widget $w, ...$rest) {}\n"
        "class Factory { function __construct(private Widget $w) {}"
        " function make(Widget $w) {} }\n"
    ),
    "lua/app.lua": (
        "local M = {}\nfunction M.build(name, w, ...) end\n"
        "function M:make(x) end\nreturn M\n"
    ),
    "scala/App.scala": (
        "package app\n"
        "class Widget\n"
        "object App { def build(name: String, w: Widget)(implicit ctx: Int):"
        " Unit = {} }\n"
        "class Factory { def make(w: Widget): Unit = {} }\n"
    ),
    "dart/lib/app.dart": (
        "class Widget {}\n"
        "void build(String name, Widget w, [int n = 0]) {}\n"
        "class Factory { void make(Widget w) {} }\n"
    ),
}

Row = tuple[str, int, str | None, bool, bool]


def _index(tmp_path: Path) -> _StatefulIngestor:
    repo = tmp_path / "proj"
    for rel, src in _FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+parameters"]),
    ).run(force=True)
    return store


def _parameters(store: _StatefulIngestor) -> dict[str, dict]:
    return {
        str(props[cs.KEY_QUALIFIED_NAME]): props
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.PARAMETER.value
    }


def _rows(store: _StatefulIngestor, owner_qn: str) -> list[Row]:
    prefix = f"{owner_qn}{cs.SEPARATOR_DOT}"
    owned = [
        p
        for qn, p in _parameters(store).items()
        if qn.startswith(prefix) and qn[len(prefix) :].isdigit()
    ]
    return [
        (
            str(p[cs.KEY_NAME]),
            int(p[cs.KEY_INDEX]),
            p.get(cs.KEY_TYPE_NAME),
            bool(p[cs.KEY_IS_VARIADIC]),
            bool(p[cs.KEY_HAS_DEFAULT]),
        )
        for p in sorted(owned, key=lambda p: int(p[cs.KEY_INDEX]))
    ]


def _edges(store: _StatefulIngestor, rel: str) -> set[tuple[str, str]]:
    return {(str(s), str(t)) for _sl, s, r, _tl, t in store.edges if r == rel}


def test_every_language_emits_its_declared_parameters(tmp_path: Path) -> None:
    store = _index(tmp_path)
    expected: dict[str, list[Row]] = {
        "proj.go.lib.Build": [
            ("name", 0, "string", False, False),
            ("w", 1, "*Widget", False, False),
            ("opts", 2, "...int", True, False),
        ],
        "proj.go.lib.Widget.Use": [("x", 0, "int", False, False)],
        "proj.js.app.build": [
            ("name", 0, None, False, False),
            ("w", 1, None, False, True),
            ("rest", 2, None, True, False),
        ],
        # `{y}` binds no single name: position 1 is consumed, nothing emitted.
        "proj.js.app.Factory.make": [("w", 0, None, False, False)],
        "proj.js.app.Factory.s": [("a", 0, None, False, False)],
        "proj.js.app.arrow": [("p", 0, None, False, False)],
        # `this: Window` takes no slot.
        "proj.ts.app.build": [
            ("name", 0, "string", False, False),
            ("w", 1, "Widget", False, False),
            ("rest", 2, "string[]", True, False),
        ],
        "proj.ts.app.Factory.make": [("w", 0, "Widget", False, False)],
        "proj.java.App.App.build(String,Widget,int...)": [
            ("name", 0, "String", False, False),
            ("w", 1, "Widget", False, False),
            ("xs", 2, "int...", True, False),
        ],
        "proj.java.App.App.App(Widget)": [("w", 0, "Widget", False, False)],
        "proj.cs.App.App.App.Build(string, Widget, int[])": [
            ("name", 0, "string", False, False),
            ("w", 1, "Widget", False, False),
            ("xs", 2, "params int[]", True, False),
        ],
        "proj.rust.src.lib.build": [
            ("name", 0, "&str", False, False),
            ("w", 1, "&Widget", False, False),
        ],
        # `&self` takes no slot.
        "proj.rust.src.lib.Widget.make": [("x", 0, "i32", False, False)],
        # The C `...` keeps position 2 and emits nothing.
        "proj.c.lib.build": [
            ("name", 0, "char", False, False),
            ("w", 1, "struct widget", False, False),
        ],
        # The unnamed `int = 0` keeps position 2 and emits nothing.
        "proj.cpp.lib.build": [
            ("name", 0, "std::string", False, False),
            ("w", 1, "Widget", False, False),
        ],
        # Out-of-class definition, bound to the class by the deferred pass.
        "proj.cpp.lib.Factory.make": [("w", 0, "Widget", False, False)],
        "proj.php.app.build": [
            ("name", 0, "string", False, False),
            ("w", 1, "Widget", False, False),
            ("rest", 2, None, True, False),
        ],
        "proj.php.app.Factory.__construct": [("w", 0, "Widget", False, False)],
        # The Lua `...` keeps position 2; the `:` receiver is implicit.
        "proj.lua.app.M.build": [
            ("name", 0, None, False, False),
            ("w", 1, None, False, False),
        ],
        "proj.lua.app.M:make": [("x", 0, None, False, False)],
        # Every curried list, in order.
        "proj.scala.App.App.build": [
            ("name", 0, "String", False, False),
            ("w", 1, "Widget", False, False),
            ("ctx", 2, "Int", False, False),
        ],
        "proj.dart.lib.app.build": [
            ("name", 0, "String", False, False),
            ("w", 1, "Widget", False, False),
            ("n", 2, "int", False, True),
        ],
        "proj.dart.lib.app.Factory.make": [("w", 0, "Widget", False, False)],
    }
    got = {owner: _rows(store, owner) for owner in expected}
    assert got == expected


def test_of_type_resolves_the_declared_type_in_every_language(tmp_path: Path) -> None:
    store = _index(tmp_path)
    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    assert of_type == {
        ("proj.c.lib.build.1", "proj.c.lib.widget"),
        ("proj.cpp.lib.Factory.make.0", "proj.cpp.lib.Widget"),
        ("proj.cpp.lib.build.1", "proj.cpp.lib.Widget"),
        ("proj.cs.App.App.App.App(Widget).0", "proj.cs.App.App.Widget"),
        (
            "proj.cs.App.App.App.Build(string, Widget, int[]).1",
            "proj.cs.App.App.Widget",
        ),
        ("proj.dart.lib.app.Factory.make.0", "proj.dart.lib.app.Widget"),
        ("proj.dart.lib.app.build.1", "proj.dart.lib.app.Widget"),
        ("proj.go.lib.Build.1", "proj.go.lib.Widget"),
        ("proj.java.App.App.App(Widget).0", "proj.java.App.Widget"),
        ("proj.java.App.App.build(String,Widget,int...).1", "proj.java.App.Widget"),
        ("proj.php.app.Factory.__construct.0", "proj.php.app.Widget"),
        ("proj.php.app.Factory.make.0", "proj.php.app.Widget"),
        ("proj.php.app.build.1", "proj.php.app.Widget"),
        ("proj.rust.src.lib.build.1", "proj.rust.src.lib.Widget"),
        ("proj.scala.App.App.build.1", "proj.scala.App.Widget"),
        ("proj.scala.App.Factory.make.0", "proj.scala.App.Widget"),
        ("proj.ts.app.Factory.make.0", "proj.ts.app.Widget"),
        ("proj.ts.app.build.1", "proj.ts.app.Widget"),
    }


def test_every_file_contributes_and_no_parameter_is_orphaned(tmp_path: Path) -> None:
    store = _index(tmp_path)
    params = _parameters(store)
    files_with_rows = {str(p[cs.KEY_PATH]) for p in params.values()}
    assert files_with_rows == {
        rel for rel in _FILES if not rel.endswith((".mod", ".toml"))
    }
    owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_PARAMETER.value)}
    assert set(params) == owned
