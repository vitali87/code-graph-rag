# A JS/TS class expression with no name and no binding to take one from
# (`export default class {...}`, `module.exports = class extends Base {...}`,
# `module.exports = [class extends Base {...}]`, `register(class {...})`,
# `return class {...}`) got no Class node, no Method nodes, no INHERITS and no
# CALLS from its members. Functions nested in its methods were registered under
# a parent that does not exist (`rules.check.anonymous_3_56`), and the calls in
# its methods were attributed to the module. Issue #2567.
#
# Naming: the direct value of `export default` takes the module's default
# export name, `default`, which is the qn a default import already resolves to
# (`import X from './m'` maps X to `m.default`). Any other anonymous class takes
# the `anonymous_<row>_<col>` name anonymous functions get.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import get_language_for_extension
from codebase_rag.parser_loader import load_parsers
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
_CALLS = str(cs.RelationshipType.CALLS)
_INHERITS = str(cs.RelationshipType.INHERITS)
_OVERRIDES = str(cs.RelationshipType.OVERRIDES)
_DEFINES = str(cs.RelationshipType.DEFINES)
_DEFINES_METHOD = str(cs.RelationshipType.DEFINES_METHOD)


class _Graph:
    """What the parser writes, merged per qn the way Memgraph would, and served
    back to dead-code as its two scans."""

    def __init__(self) -> None:
        self.nodes: dict[str, ResultRow] = {}
        self.rels: list[ResultRow] = []

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
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

    def label(self, qn: str) -> str | None:
        row = self.nodes.get(_PREFIX + qn)
        return None if row is None else str(row[cs.KEY_LABEL])

    def exported(self, qn: str) -> bool:
        row = self.nodes.get(_PREFIX + qn)
        assert row is not None, f"no node {qn!r}; have {sorted(self.nodes)}"
        return row.get(cs.KEY_IS_EXPORTED) is True

    def has(self, frm: str, rel_type: str, to: str) -> bool:
        return any(
            r[cs.KEY_FROM_QN] == _PREFIX + frm
            and r[cs.KEY_REL_TYPE] == rel_type
            and r[cs.KEY_TO_QN] == _PREFIX + to
            for r in self.rels
        )

    def out(self, frm: str, rel_type: str) -> set[str]:
        return {
            str(r[cs.KEY_TO_QN]).removeprefix(_PREFIX)
            for r in self.rels
            if r[cs.KEY_FROM_QN] == _PREFIX + frm and r[cs.KEY_REL_TYPE] == rel_type
        }

    def qns(self, label: cs.NodeLabel) -> set[str]:
        return {
            qn.removeprefix(_PREFIX)
            for qn, row in self.nodes.items()
            if row[cs.KEY_LABEL] == label.value
        }


def _index(tmp_path: Path, files: dict[str, str]) -> _Graph:
    for name, src in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    parsers, queries = load_parsers()
    # The module-scoped `issue_graph` runs before the per-test grammar skip
    # hook is installed, so a base install without these grammars must skip
    # here rather than assert on an empty graph.
    missing = sorted(
        {
            language.value
            for name in files
            if (language := get_language_for_extension(Path(name).suffix)) is not None
            and language not in parsers
        }
    )
    if missing:
        pytest.skip(f"{', '.join(missing)} parser not available")
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


# The issue's repro, verbatim apart from the helper module.
BASE_JS = """\
class Base { require(cond) { if (!cond) throw new Error('x'); } }
module.exports = { Base };
"""

RULES_JS = """\
const { Base } = require('./base');
module.exports = [
  class extends Base {
    check(node) { this.require(node.ok); return [1].map(x => x + 1); }
  },
  class NamedRule extends Base {
    check(node) { this.require(node.fine); }
  },
];
"""

SINGLE_JS = """\
const { Base } = require('./base');
module.exports = class extends Base {
  run(node) { this.require(node.a); }
};
"""

HELPER_MJS = """\
export function helper() { return 1; }
"""

ESM_MJS = """\
import { helper } from './helper.mjs';
export default class {
  go() { return helper(); }
}
export const Anon = class {
  stop() { return helper(); }
};
"""

PAGE_TS = """\
import { helper } from './helper.mjs'
class Component { render(): number { return 0 } }
export default class extends Component {
  render(): number { return helper() }
}
"""

ISSUE_FILES = {
    "base.js": BASE_JS,
    "rules.js": RULES_JS,
    "single.js": SINGLE_JS,
    "helper.mjs": HELPER_MJS,
    "esm.mjs": ESM_MJS,
    "page.ts": PAGE_TS,
}


@pytest.fixture(scope="module")
def issue_graph(tmp_path_factory: pytest.TempPathFactory) -> _Graph:
    return _index(tmp_path_factory.mktemp("issue2567"), ISSUE_FILES)


class TestIssueRepro:
    def test_class_in_module_exports_array(self, issue_graph: _Graph) -> None:
        cls = "rules.anonymous_2_2"
        assert issue_graph.label(cls) == cs.NodeLabel.CLASS.value
        assert issue_graph.label(f"{cls}.check") == cs.NodeLabel.METHOD.value
        assert issue_graph.has("rules", _DEFINES, cls)
        assert issue_graph.has(cls, _DEFINES_METHOD, f"{cls}.check")
        assert issue_graph.has(cls, _INHERITS, "base.Base")
        assert issue_graph.has(f"{cls}.check", _CALLS, "base.Base.require")

    def test_nested_arrow_hangs_off_the_method(self, issue_graph: _Graph) -> None:
        functions = issue_graph.qns(cs.NodeLabel.FUNCTION)
        assert "rules.anonymous_2_2.check.anonymous_3_56" in functions
        assert "rules.check.anonymous_3_56" not in functions

    def test_module_exports_class(self, issue_graph: _Graph) -> None:
        cls = "single.anonymous_1_17"
        assert issue_graph.label(cls) == cs.NodeLabel.CLASS.value
        assert issue_graph.label(f"{cls}.run") == cs.NodeLabel.METHOD.value
        assert issue_graph.has(cls, _INHERITS, "base.Base")
        assert issue_graph.has(f"{cls}.run", _CALLS, "base.Base.require")

    def test_export_default_class_takes_the_default_export_name(
        self, issue_graph: _Graph
    ) -> None:
        assert issue_graph.label("esm.default") == cs.NodeLabel.CLASS.value
        assert issue_graph.label("esm.default.go") == cs.NodeLabel.METHOD.value
        assert issue_graph.has("esm.default.go", _CALLS, "helper.helper")

    def test_ts_export_default_class_extends(self, issue_graph: _Graph) -> None:
        assert issue_graph.label("page.default") == cs.NodeLabel.CLASS.value
        assert issue_graph.has("page.default", _INHERITS, "page.Component")
        assert issue_graph.has(
            "page.default.render", _OVERRIDES, "page.Component.render"
        )
        assert issue_graph.has("page.default.render", _CALLS, "helper.helper")

    def test_member_calls_are_not_attributed_to_the_module(
        self, issue_graph: _Graph
    ) -> None:
        # The ESM files' only calls are in the class bodies. (The CommonJS
        # files' top-level `require('./base')` is a module call of its own.)
        assert "helper.helper" not in issue_graph.out("esm", _CALLS)
        assert "helper.helper" not in issue_graph.out("page", _CALLS)

    def test_exported_anonymous_classes_are_exported(self, issue_graph: _Graph) -> None:
        # Exported the way `export class X` and its members are, so dead-code
        # roots them instead of reporting the framework-called methods.
        for qn in (
            "esm.default",
            "esm.default.go",
            "page.default",
            "page.default.render",
            "single.anonymous_1_17",
            "single.anonymous_1_17.run",
            "rules.anonymous_2_2",
            "rules.anonymous_2_2.check",
        ):
            assert issue_graph.exported(qn), qn

    def test_exported_anonymous_classes_are_not_dead(self, issue_graph: _Graph) -> None:
        dead = _dead(issue_graph)
        assert not {qn for qn in dead if "anonymous_2_2" in qn}
        assert not {qn for qn in dead if "anonymous_1_17" in qn}
        assert not {qn for qn in dead if qn.startswith(("esm.default", "page.default"))}


SHAPES_JS = """\
const { Base } = require('./base');
register(class extends Base {
  run() { this.require(1); }
});
function makeRule() {
  return class extends Base {
    check() { this.require(2); }
  };
}
module.exports = { makeRule };
"""


class TestOtherUnboundShapes:
    def test_class_passed_as_argument(self, tmp_path: Path) -> None:
        graph = _index(tmp_path, {"base.js": BASE_JS, "shapes.js": SHAPES_JS})
        cls = "shapes.anonymous_1_9"
        assert graph.label(cls) == cs.NodeLabel.CLASS.value
        assert graph.has(cls, _INHERITS, "base.Base")
        assert graph.has(f"{cls}.run", _CALLS, "base.Base.require")

    def test_class_returned_from_factory_is_scoped_to_it(self, tmp_path: Path) -> None:
        graph = _index(tmp_path, {"base.js": BASE_JS, "shapes.js": SHAPES_JS})
        cls = "shapes.makeRule.anonymous_5_9"
        assert graph.label(cls) == cs.NodeLabel.CLASS.value
        assert graph.has("shapes.makeRule", _DEFINES, cls)
        assert graph.has(cls, _INHERITS, "base.Base")
        assert graph.has(f"{cls}.check", _CALLS, "base.Base.require")


class TestDefaultImportTarget:
    def test_default_import_resolves_to_the_default_class(self, tmp_path: Path) -> None:
        # A default import is mapped to `<module>.default`; naming the class
        # that makes the subclass edge land on it.
        graph = _index(
            tmp_path,
            {
                "page.mjs": "export default class { render() { return 0; } }\n",
                "home.mjs": (
                    "import Page from './page.mjs';\n"
                    "export class Home extends Page { render() { return 1; } }\n"
                ),
            },
        )
        assert graph.has("home.Home", _INHERITS, "page.default")
        assert graph.has("home.Home.render", _OVERRIDES, "page.default.render")


DECORATOR_FACTORY_TS = """\
function Stamp<T extends { new (...args: any[]): {} }>(constructor: T) {
  return class Stamped extends constructor { ts = 1; };
}
function Tag<T extends { new (...args: any[]): {} }>(constructor: T) {
  return class extends constructor { tag = 2; };
}
export class User { constructor(public name: string) {} }
"""


class TestHeritageNeverResolvesToAMethod:
    def test_parameter_base_does_not_bind_a_same_named_method(
        self, tmp_path: Path
    ) -> None:
        # `constructor` is the factory's parameter. The simple-name sweep used
        # to bind it to `User.constructor`, an INHERITS onto a Method; the
        # newly indexed anonymous classes of every decorator factory made that
        # common. It is now the written, unresolved name, like any other
        # unknown base.
        graph = _index(tmp_path, {"deco.ts": DECORATOR_FACTORY_TS})
        onto_methods = [
            r
            for r in graph.rels
            if r[cs.KEY_REL_TYPE] == _INHERITS
            and r[cs.KEY_TO_LABEL] == cs.NodeLabel.METHOD.value
        ]
        assert not onto_methods
        for child in ("deco.Stamp.Stamped", "deco.Tag.anonymous_4_9"):
            assert graph.label(child) == cs.NodeLabel.CLASS.value
            assert "deco.User.constructor" not in graph.out(child, _INHERITS)


# --- Negative tests: what must stay as it was. ---

NAMED_MJS = """\
import { helper } from './helper.mjs';
export default class Widget { draw() { return helper(); } }
export const Aliased = class Inner { go() { return helper(); } };
const Bound = class { run() { return helper(); } };
export { Bound };
"""


class TestNamedClassesUnchanged:
    def test_named_declarations_and_expressions_keep_their_names(
        self, tmp_path: Path
    ) -> None:
        graph = _index(
            tmp_path,
            {"helper.mjs": HELPER_MJS, "named.mjs": NAMED_MJS},
        )
        assert graph.qns(cs.NodeLabel.CLASS) == {
            "named.Widget",
            "named.Inner",
            "named.Bound",
        }
        assert graph.has("named.Widget.draw", _CALLS, "helper.helper")
        assert graph.has("named.Inner.go", _CALLS, "helper.helper")
        assert graph.has("named.Bound.run", _CALLS, "helper.helper")

    def test_named_class_in_the_issue_array_keeps_its_name(
        self, issue_graph: _Graph
    ) -> None:
        rules = {
            q for q in issue_graph.qns(cs.NodeLabel.CLASS) if q.startswith("rules.")
        }
        assert rules == {"rules.NamedRule", "rules.anonymous_2_2"}
        assert issue_graph.has("rules.NamedRule", _INHERITS, "base.Base")


OBJECT_EXPORT_JS = """\
const { helper } = require('./h');
module.exports = {
  run() { return helper(); },
  named: function () { return helper(); },
  arrow: () => helper(),
};
"""


class TestObjectLiteralExport:
    def test_object_literal_is_not_a_class(self, tmp_path: Path) -> None:
        graph = _index(
            tmp_path,
            {
                "h.js": "exports.helper = function () { return 1; };\n",
                "obj.js": OBJECT_EXPORT_JS,
            },
        )
        assert not graph.qns(cs.NodeLabel.CLASS)
        assert {"obj.run", "obj.named", "obj.arrow"} <= graph.qns(cs.NodeLabel.FUNCTION)
        # The CommonJS class rule covers classes only: the object's own
        # functions keep the export decision they had before.
        for qn in ("obj.run", "obj.named", "obj.arrow"):
            assert not graph.exported(qn), qn


FUNCTION_SCOPE_MJS = """\
import { helper } from './helper.mjs';
export function outer() {
  const Local = class { m() { return helper(); } };
  return class extends Local { n() { return helper(); } };
}
export default function make() { return class { k() { return 1; } }; }
"""

FUNCTION_SCOPE_CJS = """\
const x = require('./dep');
function setup() {
  module.exports = class { m() { return 1; } };
}
module.exports.factory = () => class { k() { return 2; } };
setup();
"""


class TestClassInsideAFunction:
    def test_keeps_function_scope_naming(self, tmp_path: Path) -> None:
        graph = _index(
            tmp_path,
            {"helper.mjs": HELPER_MJS, "fn.mjs": FUNCTION_SCOPE_MJS},
        )
        assert graph.qns(cs.NodeLabel.CLASS) == {
            "fn.outer.Local",
            "fn.outer.anonymous_3_9",
            # Inside the default-exported function, not the default export.
            "fn.make.anonymous_5_40",
        }
        assert graph.has("fn.outer", _DEFINES, "fn.outer.anonymous_3_9")
        assert graph.has("fn.outer.anonymous_3_9", _INHERITS, "fn.outer.Local")
        assert graph.has("fn.make", _DEFINES, "fn.make.anonymous_5_40")
        assert graph.label("fn.default") is None
        assert graph.exported("fn.make")

    def test_is_not_exported(self, tmp_path: Path) -> None:
        graph = _index(
            tmp_path,
            {
                "helper.mjs": HELPER_MJS,
                "fn.mjs": FUNCTION_SCOPE_MJS,
                "dep.js": "module.exports = {};\n",
                "cjs.js": FUNCTION_SCOPE_CJS,
            },
        )
        # A class built inside a function is that function's local, even when
        # the function is exported or the class is assigned to module.exports
        # when the function runs.
        for qn in (
            "fn.outer.anonymous_3_9",
            "fn.outer.anonymous_3_9.n",
            "fn.make.anonymous_5_40",
            "fn.make.anonymous_5_40.k",
            "cjs.setup.anonymous_2_19",
            "cjs.setup.anonymous_2_19.m",
            "cjs.anonymous_4_31",
            "cjs.anonymous_4_31.k",
        ):
            assert not graph.exported(qn), qn


CHAINED_AND_COMPUTED_CJS = """\
const x = require('./dep');
module.exports = exports.Rule = class { run() { return 1; } };
exports["Named"] = class { go() { return 2; } };
module.exports['Other'] = class { put() { return 3; } };
exports[pick()] = class { dyn() { return 4; } };
a = b = class { w() { return 5; } };
function pick() { return 'k'; }
"""


class TestChainedAndComputedCommonJsExports:
    @pytest.fixture(scope="class")
    def graph(self, tmp_path_factory: pytest.TempPathFactory) -> _Graph:
        return _index(
            tmp_path_factory.mktemp("cjs"),
            {"dep.js": "module.exports = {};\n", "rules.js": CHAINED_AND_COMPUTED_CJS},
        )

    @pytest.mark.parametrize(
        "cls",
        [
            # `module.exports = exports.Rule = class`: the inner assignment is
            # the value of the top-level export.
            "rules.anonymous_1_32",
            # A static computed property names the export as `.Named` does.
            "rules.anonymous_2_19",
            "rules.anonymous_3_26",
        ],
    )
    def test_is_exported_with_its_members(self, graph: _Graph, cls: str) -> None:
        assert graph.label(cls) == cs.NodeLabel.CLASS.value
        assert graph.exported(cls)
        members = graph.out(cls, _DEFINES_METHOD)
        assert members
        for member in members:
            assert graph.exported(member), member

    @pytest.mark.parametrize(
        "cls",
        [
            # A computed key names no export a caller can be found through.
            "rules.anonymous_4_18",
            # A chain of plain assignments exports nothing.
            "rules.anonymous_5_8",
        ],
    )
    def test_is_not_exported(self, graph: _Graph, cls: str) -> None:
        assert graph.label(cls) == cs.NodeLabel.CLASS.value
        assert not graph.exported(cls)


PRIVATE_MEMBER_JS = """\
const x = require('./dep');
module.exports = class {
  #secret() { return 1; }
  open() { return this.#secret(); }
};
"""


class TestUnexportedMembers:
    def test_private_member_of_a_commonjs_class_stays_unexported(
        self, tmp_path: Path
    ) -> None:
        graph = _index(
            tmp_path,
            {"dep.js": "module.exports = {};\n", "priv.js": PRIVATE_MEMBER_JS},
        )
        assert graph.exported("priv.anonymous_1_17.open")
        assert not graph.exported("priv.anonymous_1_17.#secret")

    def test_class_passed_as_argument_is_not_exported(self, tmp_path: Path) -> None:
        graph = _index(tmp_path, {"base.js": BASE_JS, "shapes.js": SHAPES_JS})
        assert not graph.exported("shapes.anonymous_1_9")
        assert not graph.exported("shapes.anonymous_1_9.run")


# --- CommonJS export shapes the assignment-chain walk missed (PR #2628
# review): the chain behind parentheses, the chain as a declarator's value,
# the assignment inside a module-level block, and a string-keyed export as the
# file's only module construct. Each left a published class reported dead, or
# left the file a classic script whose unrelated declarations were rooted.

DEP_JS = "module.exports = {};\n"


class TestCommonJsExportShapes:
    @pytest.mark.parametrize(
        ("src", "cls"),
        [
            pytest.param(
                "module.exports = (exports.Rule = class { run() {} });\n",
                "m.anonymous_0_33",
                id="parenthesized-chain",
            ),
            pytest.param(
                "const Rule = module.exports = class { run() {} };\n",
                "m.anonymous_0_30",
                id="declarator-chain",
            ),
            pytest.param(
                "if (typeof module === 'object') {\n"
                "  module.exports = class { run() {} };\n"
                "}\n",
                "m.anonymous_1_19",
                id="module-level-block",
            ),
        ],
    )
    def test_published_class_and_method_are_exported_and_live(
        self, tmp_path: Path, src: str, cls: str
    ) -> None:
        graph = _index(tmp_path, {"m.js": src})
        assert graph.label(cls) == cs.NodeLabel.CLASS.value
        assert graph.exported(cls)
        assert graph.exported(f"{cls}.run")
        dead = _dead(graph)
        assert cls not in dead
        assert f"{cls}.run" not in dead

    @pytest.mark.parametrize(
        "export",
        [
            pytest.param('exports["Rule"] = class { run() {} };\n', id="string-key"),
            pytest.param(
                "const Rule = exports.Rule = class { run() {} };\n",
                id="declarator-chain",
            ),
            pytest.param(
                "if (typeof exports === 'object') {\n"
                "  exports['Rule'] = class { run() {} };\n"
                "}\n",
                id="module-level-block",
            ),
        ],
    )
    def test_commonjs_export_makes_the_file_a_module(
        self, tmp_path: Path, export: str
    ) -> None:
        # With no `require`, the export assignment is the file's only module
        # construct. Missing it left the file a classic script, so the
        # unrelated `Unused` was rooted as a page global.
        graph = _index(tmp_path, {"m.js": export + "class Unused { idle() {} }\n"})
        assert not graph.exported("m.Unused")
        assert not graph.exported("m.Unused.idle")
        dead = _dead(graph)
        assert "m.Unused" in dead
        assert "m.Unused.idle" in dead


class TestCommonJsExportShapeBoundaries:
    def test_non_export_assignment_chains_stay_unexported(self, tmp_path: Path) -> None:
        src = (
            "const dep = require('./dep');\n"
            "let a, b;\n"
            "a = (b = class { w() {} });\n"
            "const R = b = class { v() {} };\n"
        )
        graph = _index(tmp_path, {"dep.js": DEP_JS, "m.js": src})
        dead = _dead(graph)
        for qn in (
            "m.anonymous_2_9",
            "m.anonymous_2_9.w",
            "m.anonymous_3_14",
            "m.anonymous_3_14.v",
        ):
            assert not graph.exported(qn), qn
            assert qn in dead, qn

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(
                "  if (flag) {\n    module.exports = class { m() {} };\n  }\n",
                id="block-in-function",
            ),
            pytest.param(
                "  if (flag) {\n    exports['Rule'] = class { m() {} };\n  }\n",
                id="string-key-in-function",
            ),
            pytest.param(
                "  const Rule = exports.Rule = class { m() {} };\n",
                id="declarator-in-function",
            ),
        ],
    )
    def test_assignment_inside_a_function_exports_nothing(
        self, tmp_path: Path, body: str
    ) -> None:
        src = (
            "const dep = require('./dep');\n"
            "function setup(flag) {\n" + body + "}\n"
            "setup(true);\n"
        )
        graph = _index(tmp_path, {"dep.js": DEP_JS, "m.js": src})
        (cls,) = {q for q in graph.qns(cs.NodeLabel.CLASS) if q.startswith("m.setup.")}
        assert not graph.exported(cls)
        assert not graph.exported(f"{cls}.m")

    @pytest.mark.parametrize("target", ["exports.Rule", "exports['Rule']"])
    def test_export_inside_a_function_keeps_a_script_a_script(
        self, tmp_path: Path, target: str
    ) -> None:
        # Only a module-level export makes the file a CommonJS module, as
        # before for `exports.Rule`; one inside a function runs only when it
        # is called, so the file's top-level declarations stay page globals.
        src = (
            "function setup() {\n"
            f"  {target} = class {{ m() {{}} }};\n"
            "}\n"
            "class Page { draw() {} }\n"
        )
        graph = _index(tmp_path, {"m.js": src})
        assert graph.exported("m.Page")
        assert graph.exported("m.Page.draw")
        (cls,) = {q for q in graph.qns(cs.NodeLabel.CLASS) if q.startswith("m.setup.")}
        assert not graph.exported(cls)

    def test_script_without_a_module_construct_keeps_its_globals(
        self, tmp_path: Path
    ) -> None:
        # Mentions of `exports` that are not a CommonJS export (a string, a
        # property of another object) do not make a classic script a module.
        src = (
            "var label = 'exports';\n"
            "window.exports = { label: label };\n"
            "if (window.ready) {\n"
            "  window.exports.page = label;\n"
            "}\n"
            "class Page { draw() { return label; } }\n"
            "function helper() { return 1; }\n"
        )
        graph = _index(tmp_path, {"m.js": src})
        dead = _dead(graph)
        for qn in ("m.Page", "m.Page.draw", "m.helper"):
            assert graph.exported(qn), qn
            assert qn not in dead, qn
