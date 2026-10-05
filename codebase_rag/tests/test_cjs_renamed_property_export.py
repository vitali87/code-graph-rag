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


# Negative: what must not change.


def test_a_shorthand_and_an_inline_export_still_bind(graph: RecordedGraph) -> None:
    callees = _callees(graph, "app.run")
    assert callees.get("lib.alpha") == "exact"
    assert "lib.gamma" not in callees
