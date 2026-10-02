# Dead-code rooted a TS/JS class's public members only when the class
# declaration itself carried `export`. A class exported separately --
# `export { X }`, `export { X as Y }`, `export default X`, CommonJS
# `module.exports = X` / `exports.X = X` -- was recognised as exported, but its
# methods were reported dead: honojs/hono declares `class Hono` without
# `export` and publishes it as `export { Hono as HonoBase }`, so
# `Hono.fire`/`mount`/`notFound`/`onError` were listed. Issue #2591.
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers import export_detection
from codebase_rag.types_defs import PropertyDict, PropertyParams, ResultRow

PROJECT = "p"
_PREFIX = PROJECT + cs.SEPARATOR_DOT
_DEAD_CODE_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.MODULE.value,
    }
)


class _Graph:
    """What the parser writes, served back to dead-code the way Memgraph
    would answer its two scans (a node's sparse updates merge into it)."""

    def __init__(self) -> None:
        self.nodes: dict[str, ResultRow] = {}
        self.rels: list[ResultRow] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        # Project/Folder/File nodes are keyed by name or path; dead-code
        # never reads them.
        qn = properties.get(cs.KEY_QUALIFIED_NAME)
        if qn is None:
            return
        row = self.nodes.setdefault(str(qn), {cs.KEY_LABEL: str(label)})
        row.update(properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None:
        self.rels.append(
            {
                cs.KEY_FROM_LABEL: str(from_spec[0]),
                cs.KEY_FROM_QN: str(from_spec[2]),
                cs.KEY_REL_TYPE: str(rel_type),
                cs.KEY_TO_LABEL: str(to_spec[0]),
                cs.KEY_TO_QN: str(to_spec[2]),
            }
        )

    def flush_all(self) -> None:
        return None

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        return None

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return [
                row
                for row in self.nodes.values()
                if row[cs.KEY_LABEL] in _DEAD_CODE_LABELS
            ]
        if query == cq.CYPHER_DEAD_CODE_RELS:
            return self.rels
        return []

    def exported(self, qn: str) -> bool:
        row = self.nodes.get(_PREFIX + qn)
        assert row is not None, f"no node {qn!r}; have {sorted(self.nodes)}"
        return row.get(cs.KEY_IS_EXPORTED) is True

    def members(self, class_qn: str) -> dict[str, bool]:
        # member name -> is_exported for every node directly under the class
        prefix = _PREFIX + class_qn + cs.SEPARATOR_DOT
        return {
            qn.removeprefix(prefix): row.get(cs.KEY_IS_EXPORTED) is True
            for qn, row in self.nodes.items()
            if qn.startswith(prefix)
        }


def _index(tmp_path: Path, files: dict[str, str]) -> _Graph:
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    parsers, queries = load_parsers()
    graph = _Graph()
    GraphUpdater(
        ingestor=graph,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return graph


def _dead(graph: _Graph) -> set[str]:
    config = default_dead_code_config(include_tests=True, include_classes=True)
    return {
        str(row[cs.KEY_QUALIFIED_NAME]).removeprefix(_PREFIX)
        for row in collect_dead_code(graph, PROJECT, config)
    }


ISSUE_REPRO_TS = """\
class Direct0 { m(): number { return 0 } }
export class Direct { m(): number { return 1 } }

class Clause { m(): number { return 2 } }
export { Clause }

class Aliased { m(): number { return 3 } }
export { Aliased as Renamed }

class Defaulted { m(): number { return 4 } }
export default Defaulted

function fnClause(): number { return 5 }
function fnAliased(): number { return 6 }
export { fnClause, fnAliased as fnRenamed }
"""


def test_issue_repro_reports_only_the_unexported_class(tmp_path: Path) -> None:
    dead = _dead(_index(tmp_path, {"src/a.ts": ISSUE_REPRO_TS}))
    assert dead == {"src.a.Direct0", "src.a.Direct0.m"}


HONO_TS = """\
class Hono {
  fire(): void {}
  mount(path: string): Hono { return this }
  notFound(handler: () => void): Hono { return this }
  onError(handler: () => void): Hono { return this }
}

export { Hono as HonoBase }
"""


def test_hono_base_public_api_is_not_dead(tmp_path: Path) -> None:
    dead = _dead(_index(tmp_path, {"src/hono-base.ts": HONO_TS}))
    assert dead == set()


# Members spanning every kind a class body ingests: constructor, method,
# static method, accessor and a property-arrow (a Method node in TS only).
_WIDGET_BODY = """\
class Widget {
  constructor() {}
  render() { return 1 }
  static create() { return 2 }
  get size() { return 3 }
  onClick = () => 4
}
"""
_WIDGET_MEMBERS = frozenset({"constructor", "render", "create", "size"})

_SEPARATE_EXPORT_FORMS = [
    pytest.param("w.ts", _WIDGET_BODY + "export { Widget }\n", id="ts-clause"),
    pytest.param("w.ts", _WIDGET_BODY + "export { Widget as Public }\n", id="ts-alias"),
    pytest.param("w.ts", _WIDGET_BODY + "export default Widget\n", id="ts-default"),
    pytest.param("w.ts", _WIDGET_BODY + "export = Widget\n", id="ts-export-assign"),
    pytest.param("w.js", _WIDGET_BODY + "export { Widget }\n", id="js-clause"),
    pytest.param("w.js", _WIDGET_BODY + "module.exports = Widget\n", id="cjs-direct"),
    pytest.param(
        "w.js", _WIDGET_BODY + "module.exports = { Widget }\n", id="cjs-object"
    ),
    pytest.param(
        "w.js",
        _WIDGET_BODY + "module.exports = { Public: Widget }\n",
        id="cjs-object-pair",
    ),
    pytest.param("w.js", _WIDGET_BODY + "exports.Widget = Widget\n", id="cjs-exports"),
    pytest.param(
        "w.js",
        _WIDGET_BODY + "module.exports.Widget = Widget\n",
        id="cjs-module-exports-member",
    ),
]


@pytest.mark.parametrize(("file_name", "source"), _SEPARATE_EXPORT_FORMS)
def test_separately_exported_class_matches_export_class(
    tmp_path: Path, file_name: str, source: str
) -> None:
    # The same decision `export class Widget` gets: the class and every
    # public member are exported, so none of them is reported dead.
    class_qn = Path(file_name).stem + ".Widget"
    inline = _index(tmp_path / "inline", {file_name: "export " + _WIDGET_BODY})
    separate = _index(tmp_path / "separate", {file_name: source})
    inline_members = inline.members(class_qn)
    assert _WIDGET_MEMBERS <= inline_members.keys()
    assert all(inline_members.values()), inline_members
    assert separate.exported(class_qn) is True
    assert separate.members(class_qn) == inline_members
    assert _dead(separate) == set()


def test_class_expression_bound_to_an_exported_const(tmp_path: Path) -> None:
    graph = _index(
        tmp_path,
        {"w.ts": "const Widget = class { render() { return 1 } }\nexport { Widget }\n"},
    )
    assert graph.exported("w.Widget.render") is True


def test_reexported_class_is_rooted_by_its_declaring_module(tmp_path: Path) -> None:
    # A barrel re-export needs the declaring module to export the class first;
    # that export is what roots the members, whichever file consumers import.
    graph = _index(
        tmp_path,
        {
            "src/impl.ts": "class Store { read() { return 1 } }\nexport { Store }\n",
            "src/index.ts": "export { Store } from './impl'\nexport * from './impl'\n",
        },
    )
    assert graph.exported("src.impl.Store.read") is True
    assert "src.impl.Store.read" not in _dead(graph)


def test_commonjs_identifier_export_marks_function_exported(tmp_path: Path) -> None:
    # The CommonJS forms name a module binding exactly like an export clause,
    # so a function exported through them is exported the same way.
    graph = _index(
        tmp_path,
        {
            "m.js": "function run() { return 1 }\n"
            "function idle() { return 2 }\n"
            "module.exports = { run }\n"
        },
    )
    assert graph.exported("m.run") is True
    assert graph.exported("m.idle") is False


def test_member_named_like_an_exported_function_is_not_rooted(
    tmp_path: Path,
) -> None:
    # `export { helper }` names the module-level function, never a class
    # member that happens to share its name.
    graph = _index(
        tmp_path,
        {
            "u.ts": "function helper() { return 1 }\n"
            "class Unrelated { helper() { return 2 } }\n"
            "export { helper }\n"
        },
    )
    assert graph.exported("u.helper") is True
    assert graph.exported("u.Unrelated.helper") is False


# ---- negative: what must stay as it was --------------------------------------


def test_unexported_classes_keep_uncalled_methods_dead(tmp_path: Path) -> None:
    graph = _index(
        tmp_path,
        {
            "esm.ts": "class Internal { m() { return 1 } }\n"
            "class Public { m() { return 2 } }\n"
            "export { Public }\n",
            "cjs.js": "class Internal { m() { return 1 } }\n"
            "class Public { m() { return 2 } }\n"
            "module.exports = { Public }\n",
        },
    )
    assert {
        "esm.Internal",
        "esm.Internal.m",
        "cjs.Internal",
        "cjs.Internal.m",
    } <= _dead(graph)


def test_exported_function_does_not_root_an_unrelated_class(tmp_path: Path) -> None:
    graph = _index(
        tmp_path,
        {
            "u.ts": "function helper() { return 1 }\n"
            "class Unrelated { m() { return 2 } }\n"
            "export { helper }\n"
        },
    )
    assert _dead(graph) == {"u.Unrelated", "u.Unrelated.m"}


_PRIVATE_BODY = """\
class Store {
  read() { return this.#load() + this.secret() }
  #load() { return 1 }
  private secret() { return 2 }
}
"""


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("export " + _PRIVATE_BODY, id="export-class"),
        pytest.param(_PRIVATE_BODY + "export { Store }\n", id="clause"),
        pytest.param(_PRIVATE_BODY + "export default Store\n", id="default"),
        pytest.param(_PRIVATE_BODY + "module.exports = Store\n", id="cjs"),
    ],
)
def test_private_members_stay_unexported(tmp_path: Path, source: str) -> None:
    # `#name` and TS `private` are never public API, whichever way the class
    # is exported.
    graph = _index(tmp_path, {"s.ts": source})
    assert graph.exported("s.Store.#load") is False
    assert graph.exported("s.Store.secret") is False


@pytest.mark.parametrize(
    "export_line",
    [
        pytest.param("export type { Shape }\n", id="type-clause"),
        pytest.param("export { type Shape }\n", id="type-specifier"),
    ],
)
def test_type_only_export_keeps_the_type_only_rule(
    tmp_path: Path, export_line: str
) -> None:
    # A type-only export already counts as exporting the class; its members
    # follow that decision (a consumer holding a `Shape` can call `area()`).
    graph = _index(
        tmp_path,
        {"t.ts": "class Shape { area() { return 1 } }\n" + export_line},
    )
    assert graph.exported("t.Shape") is True
    assert graph.exported("t.Shape.area") is graph.exported("t.Shape")


def test_class_local_to_an_exported_function_stays_unexported(
    tmp_path: Path,
) -> None:
    # Only the module-level binding is exported; a class declared inside the
    # exported function's body is reached through it, as with
    # `export function outer() { class Inner {} }`.
    graph = _index(
        tmp_path,
        {
            "o.ts": "function outer() {\n"
            "  class Inner { m() { return 1 } }\n"
            "  return new Inner()\n"
            "}\n"
            "export { outer }\n"
        },
    )
    assert graph.exported("o.outer") is True
    assert graph.exported("o.outer.Inner.m") is False


@pytest.mark.parametrize(
    "source",
    [
        "const factory = () => class Local { method() { return 1 } }\n"
        "export { factory }\n",
        "export const factory = () => class Local { method() { return 1 } }\n",
    ],
    ids=["export-clause", "export-declaration"],
)
def test_class_built_by_a_concise_arrow_stays_unexported(
    tmp_path: Path, source: str
) -> None:
    # A concise arrow's expression body is a function body too: the class it
    # returns is built per call, like one declared inside `{ ... }`.
    graph = _index(tmp_path, {"f.ts": source})
    assert graph.exported("f.factory") is True
    assert graph.exported("f.Local") is False
    assert graph.exported("f.Local.method") is False


def test_commonjs_assignment_inside_a_function_exports_nothing(
    tmp_path: Path,
) -> None:
    # Assigned when the function runs, not at module load.
    graph = _index(
        tmp_path,
        {
            "w.js": "class Widget { render() { return 1 } }\n"
            "function install() { module.exports = Widget }\n"
            "install()\n"
        },
    )
    assert graph.exported("w.Widget") is False
    assert graph.exported("w.Widget.render") is False


def test_reexport_does_not_export_a_local_namesake(tmp_path: Path) -> None:
    # `export { Store } from './impl'` exports impl's binding and creates no
    # local one, so this module's own `Store` stays private.
    graph = _index(
        tmp_path,
        {
            "src/impl.ts": "export class Store { read() { return 1 } }\n",
            "src/index.ts": "class Store { read() { return 2 } }\n"
            "export { Store } from './impl'\n",
        },
    )
    assert graph.exported("src.index.Store") is False
    assert graph.exported("src.index.Store.read") is False


def test_export_name_scan_runs_once_per_file() -> None:
    # Every declaration of a file asks for the module's export names; the scan
    # of its top-level statements must run once per FILE, or a bundle with
    # thousands of declarations goes quadratic.
    parsers, _ = load_parsers()
    tree = parsers[cs.SupportedLanguage.JS].parse(
        b"function a() {}\nfunction b() {}\nfunction c() {}\nmodule.exports = { a }\n"
    )
    declarations = [
        (c, c.child_by_field_name(cs.FIELD_NAME))
        for c in tree.root_node.children
        if c.type == cs.TS_FUNCTION_DECLARATION
    ]
    with patch.object(
        export_detection,
        "_commonjs_export_local_names",
        wraps=export_detection._commonjs_export_local_names,
    ) as spy:
        exported = [
            export_detection._named_by_module_export(
                node, name.text.decode() if name is not None and name.text else ""
            )
            for node, name in declarations
        ]
    assert exported == [True, False, False]
    assert spy.call_count == 1


# Property-arrow members: the Method node is ingested from the arrow itself,
# while `private` / `#` sits on the enclosing field definition.
_ARROW_BODY = """\
class Panel {
  private hide = () => 1
  private static reset = () => 2
  #toggle = () => 3
  protected paint = () => 4
  protected layout() { return 5 }
  public show = () => 6
  open = () => 7
}
"""
_ARROW_FORMS = [
    pytest.param("export " + _ARROW_BODY, id="export-class"),
    pytest.param(_ARROW_BODY + "export { Panel }\n", id="clause"),
]


@pytest.mark.parametrize("source", _ARROW_FORMS)
def test_private_property_arrows_stay_unexported(tmp_path: Path, source: str) -> None:
    # The issue's Expected: only public (non-`#private`, non-`private`)
    # property-arrows are roots, exactly as for methods.
    graph = _index(tmp_path, {"p.ts": source})
    members = graph.members("p.Panel")
    assert members["hide"] is False
    assert members["reset"] is False
    # No node is ingested for a `#name` arrow today; none may be exported.
    assert members.get("#toggle", False) is False


@pytest.mark.parametrize("source", _ARROW_FORMS)
def test_protected_property_arrow_follows_protected_methods(
    tmp_path: Path, source: str
) -> None:
    # `protected` is an inheritance surface and stays exported for methods; a
    # protected property-arrow gets the same decision.
    members = _index(tmp_path, {"p.ts": source}).members("p.Panel")
    assert members["paint"] is members["layout"] is True


@pytest.mark.parametrize("source", _ARROW_FORMS)
def test_public_property_arrows_stay_exported(tmp_path: Path, source: str) -> None:
    members = _index(tmp_path, {"p.ts": source}).members("p.Panel")
    assert members["show"] is True
    assert members["open"] is True
