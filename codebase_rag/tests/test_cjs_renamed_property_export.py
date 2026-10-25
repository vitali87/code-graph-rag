"""Issue #2984: a CommonJS renamed property export binds its function.

`module.exports = { alpha, renamed: beta }` exports `beta` under the name
`renamed`, but the export was recorded under the key alone and never tied to
`beta`, so `const { renamed } = require("./lib"); renamed()` bound nothing.
The shorthand `{ alpha }` and the ESM `export { beta as renamed }` bound.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

FILES = {
    "lib.js": (
        "function alpha() { return 1; }\n"
        "function beta() { return 2; }\n"
        "function gamma() { return 3; }\n"
        "module.exports = { alpha, renamed: beta, again: alpha, inline: () => 4 };\n"
    ),
    "app.js": (
        'const { alpha, renamed, again, inline } = require("./lib");\n'
        "function run() {\n"
        "  return alpha() + renamed() + again() + inline();\n"
        "}\n"
    ),
    # Bot review on PR #2994: a same-named local does not hide the published
    # binding, and a later whole-object assignment replaces the earlier one.
    "shadow.js": (
        "function renamed() { return 1; }\n"
        "function beta() { return 2; }\n"
        "module.exports = { renamed: beta };\n"
    ),
    "esm_shadow.mjs": (
        "function renamed() { return 1; }\n"
        "function beta() { return 2; }\n"
        "export { beta as renamed };\n"
    ),
    "replaced.js": (
        "function beta() { return 2; }\n"
        "module.exports = { renamed: beta };\n"
        "module.exports = {};\n"
    ),
    "user.js": (
        'const { renamed } = require("./shadow");\n'
        "function viaShadow() {\n  return renamed();\n}\n"
    ),
    "replaced_user.js": (
        'const { renamed } = require("./replaced");\n'
        "function viaReplaced() {\n  return renamed();\n}\n"
    ),
    "esm_user.mjs": (
        'import { renamed } from "./esm_shadow.mjs";\n'
        "export function viaEsmShadow() {\n  return renamed();\n}\n"
    ),
    # A published binding the graph cannot resolve (an external package's)
    # still is not the module's own same-named local.
    "ext_shadow.js": (
        'const { beta } = require("left-pad");\n'
        "function renamed() { return 1; }\n"
        "module.exports = { renamed: beta };\n"
    ),
    "ext_esm_shadow.mjs": (
        'import { beta } from "left-pad";\n'
        "function renamed() { return 1; }\n"
        "export { beta as renamed };\n"
    ),
    "ext_user.js": (
        'const { renamed } = require("./ext_shadow");\n'
        "function viaExtShadow() {\n  return renamed();\n}\n"
    ),
    "ext_esm_user.mjs": (
        'import { renamed } from "./ext_esm_shadow.mjs";\n'
        "export function viaExtEsmShadow() {\n  return renamed();\n}\n"
    ),
}


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("cjsren") / "cjsren"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}{caller}"
    }


def test_a_renamed_property_export_binds_its_function(graph: RecordedGraph) -> None:
    assert _callees(graph, "app.run").get("lib.beta") == "exact"


@pytest.mark.parametrize(
    ("caller", "module"),
    [("user.viaShadow", "shadow"), ("esm_user.viaEsmShadow", "esm_shadow")],
    ids=["commonjs", "esm"],
)
def test_a_same_named_local_does_not_hide_the_published_binding(
    graph: RecordedGraph, caller: str, module: str
) -> None:
    callees = _callees(graph, caller)
    assert callees == {f"{module}.beta": "exact"}, callees


@pytest.mark.parametrize(
    "caller",
    ["ext_user.viaExtShadow", "ext_esm_user.viaExtEsmShadow"],
    ids=["commonjs", "esm"],
)
def test_an_unresolvable_published_binding_is_not_the_same_named_local(
    graph: RecordedGraph, caller: str
) -> None:
    callees = _callees(graph, caller)
    assert "exact" not in callees.values(), callees


# Negative: what must not change.


def test_a_shorthand_and_an_inline_export_still_bind(graph: RecordedGraph) -> None:
    callees = _callees(graph, "app.run")
    assert callees.get("lib.alpha") == "exact"
    assert "lib.gamma" not in callees


def test_a_replaced_export_object_publishes_nothing_it_dropped(
    graph: RecordedGraph,
) -> None:
    assert "replaced.beta" not in _callees(graph, "replaced_user.viaReplaced")
