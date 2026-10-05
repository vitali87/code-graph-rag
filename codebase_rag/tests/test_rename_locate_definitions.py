"""Issue #2768: `rename` locates a definition that has no `name` field.

The definition token was found by walking the span for a node whose `name`
field spells the old name. C and C++ name a function through the declarator
chain, a Lua `function M.f` names it with a `dot_index_expression` whose text
is `M.f`, and a JS/TS object-literal pair keys it with `key`: none of them was
found, so every C function, C++ free function, Lua module function and pair
value was refused with "Could not locate the name". PHP's token was found,
but the patcher rejected it because tree-sitter-php's identifier node is
`name`, which was not an identifier type.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.patcher import Patcher, PatcherError
from codebase_rag.editing.rename import QueryFn, RenameReport, rename
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

FILES = {
    # Bot review on PR #2895: a declaration and a same-named pair value on one
    # line share a line span; their columns tell them apart.
    "oneline.js": "function foo() { return 0; } const obj = { foo: () => 1 }; foo();\n",
    "util.c": (
        "static int helper(int x) {\n"
        "    return x + 1;\n"
        "}\n"
        "\n"
        "int *ptr_fn(void) {\n"
        "    return 0;\n"
        "}\n"
        "\n"
        "int count(int count) {\n"
        "    return count;\n"
        "}\n"
        "\n"
        "int caller(void) {\n"
        "    return helper(2) + *ptr_fn() + count(3);\n"
        "}\n"
    ),
    "math.cpp": (
        "int twice(int x) {\n"
        "    return x * 2;\n"
        "}\n"
        "\n"
        "namespace geo {\n"
        "int area(int w) {\n"
        "    return twice(w);\n"
        "}\n"
        "}\n"
        "\n"
        "int &ref_fn(int &x) {\n"
        "    return x;\n"
        "}\n"
        "\n"
        "class Widget {\n"
        "public:\n"
        "    int size() const;\n"
        "};\n"
        "\n"
        "int Widget::size() const {\n"
        "    return twice(3);\n"
        "}\n"
    ),
    "helpers.php": (
        "<?php\n"
        "function phelper($x) {\n"
        "    return $x + 1;\n"
        "}\n"
        "\n"
        "function pcaller() {\n"
        "    return phelper(2);\n"
        "}\n"
    ),
    "mod.lua": (
        "local M = {}\n"
        "\n"
        "function M.mhelper(x)\n"
        "  return x + 1\n"
        "end\n"
        "\n"
        "function M.mcaller()\n"
        "  return M.mhelper(2)\n"
        "end\n"
        "\n"
        "local function lf(x)\n"
        "  return x\n"
        "end\n"
        "\n"
        "function gf(x)\n"
        "  return lf(x)\n"
        "end\n"
        "\n"
        "return M\n"
    ),
    "obj.js": (
        "const obj = {\n"
        "  arrowProp: () => 1,\n"
        "  fnProp: function () {\n"
        "    return 2;\n"
        "  },\n"
        "  shortProp() {\n"
        "    return 3;\n"
        "  },\n"
        "};\n"
        "\n"
        "function make() {\n"
        "  return { make: () => 1 };\n"
        "}\n"
        "\n"
        "module.exports = obj;\n"
    ),
    "widget.cpp": (
        "class W {\n"
        "public:\n"
        "    int inl() { return 1; }\n"
        "};\n"
        "\n"
        "int use() {\n"
        "    W w;\n"
        "    return w.inl();\n"
        "}\n"
    ),
    "nest.lua": "local M = {sub = {}}\n\nfunction M.sub.deep(x)\n  return x\nend\n\nreturn M\n",
    "tobj.ts": "export const tobj = {\n  tarrow: (n: number): number => n,\n};\n",
    "pymod.py": "def pyhelper(x):\n    return x\n\n\ndef pycaller():\n    return pyhelper(1)\n",
    # The pair's key names the property even when the value starts on the
    # next line, or names itself too, or declares a same-named function
    # inside (bot review on PR #2895).
    "obj2.js": (
        "const obj2 = {\n"
        "  multi:\n"
        "    function () {\n"
        "      return 4;\n"
        "    },\n"
        "  named: function named() {\n"
        "    return 5;\n"
        "  },\n"
        "  outer: () => {\n"
        "    function outer() {\n"
        "      return 6;\n"
        "    }\n"
        "    return outer();\n"
        "  },\n"
        "};\n"
        "\n"
        "module.exports = obj2;\n"
    ),
}


def _fetch(graph: RecordedGraph) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return graph.fetch_all(query, dict(params) if params is not None else None)

    return fetch


@pytest.fixture
def poly_repo(tmp_path: Path) -> tuple[Path, RecordedGraph]:
    root = tmp_path / "polyrename"
    for rel, text in FILES.items():
        _write(root, rel, text)
    return root, _index(root, MagicMock())


def _plan(repo: tuple[Path, RecordedGraph], target: str) -> RenameReport:
    root, graph = repo
    return rename(
        root,
        _fetch(graph),
        graph.project,
        f"{graph.project}.{target}",
        "renamed_fn",
        dry_run=True,
    )


def _sites(report: RenameReport, kind: str) -> set[tuple[str, int, int]]:
    return {(s.path, s.line, s.col) for s in report.sites if s.kind == kind}


@pytest.mark.parametrize(
    ("target", "definition"),
    [
        ("util.helper", ("util.c", 1, 11)),
        ("util.ptr_fn", ("util.c", 5, 5)),
        ("math.twice", ("math.cpp", 1, 4)),
        ("math.geo.area", ("math.cpp", 6, 4)),
        ("math.ref_fn", ("math.cpp", 11, 5)),
        ("widget.W.inl", ("widget.cpp", 3, 8)),
        ("helpers.phelper", ("helpers.php", 2, 9)),
        ("mod.M.mhelper", ("mod.lua", 3, 11)),
        ("nest.M.sub.deep", ("nest.lua", 3, 15)),
        ("obj.arrowProp", ("obj.js", 2, 2)),
        ("obj.fnProp", ("obj.js", 3, 2)),
        ("tobj.tarrow", ("tobj.ts", 2, 2)),
        ("obj2.multi", ("obj2.js", 2, 2)),
        ("obj2.named", ("obj2.js", 6, 2)),
        # The property; `obj2.outer` alone is the function declared inside.
        ("obj2.outer@9", ("obj2.js", 9, 2)),
        ("oneline.foo", ("oneline.js", 1, 9)),
        ("oneline.foo@1", ("oneline.js", 1, 43)),
    ],
)
def test_the_definition_token_is_located(
    poly_repo: tuple[Path, RecordedGraph],
    target: str,
    definition: tuple[str, int, int],
) -> None:
    report = _plan(poly_repo, target)

    assert _sites(report, "definition") == {definition}


@pytest.mark.parametrize(
    ("target", "calls"),
    [
        ("util.helper", {("util.c", 14, 11)}),
        ("math.twice", {("math.cpp", 7, 11), ("math.cpp", 21, 11)}),
        ("widget.W.inl", {("widget.cpp", 8, 13)}),
        ("helpers.phelper", {("helpers.php", 7, 11)}),
        ("mod.M.mhelper", {("mod.lua", 8, 11)}),
    ],
)
def test_the_plan_lists_the_call_sites_too(
    poly_repo: tuple[Path, RecordedGraph],
    target: str,
    calls: set[tuple[str, int, int]],
) -> None:
    report = _plan(poly_repo, target)

    assert _sites(report, "call") == calls
    assert report.unlocatable == ()


def test_a_c_function_is_named_by_its_declarator_not_its_parameter(
    poly_repo: tuple[Path, RecordedGraph],
) -> None:
    report = _plan(poly_repo, "util.count")

    assert _sites(report, "definition") == {("util.c", 9, 4)}


@pytest.mark.parametrize(
    ("target", "path", "expected"),
    [
        ("util.helper", "util.c", "return renamed_fn(2) + *ptr_fn() + count(3);"),
        ("helpers.phelper", "helpers.php", "return renamed_fn(2);"),
        ("mod.M.mhelper", "mod.lua", "return M.renamed_fn(2)"),
    ],
)
def test_an_applied_rename_rewrites_definition_and_callers(
    poly_repo: tuple[Path, RecordedGraph], target: str, path: str, expected: str
) -> None:
    root, graph = poly_repo
    old = target.rsplit(".", 1)[-1]

    report = rename(
        root, _fetch(graph), graph.project, f"{graph.project}.{target}", "renamed_fn"
    )

    assert report.applied, report.message
    text = (root / path).read_text()
    assert old not in text
    assert expected in text


# Negative: what must not change.


@pytest.mark.parametrize(
    ("target", "definition"),
    [
        ("pymod.pyhelper", ("pymod.py", 1, 4)),
        ("math.Widget.size", ("math.cpp", 20, 12)),
        ("mod.lf", ("mod.lua", 11, 15)),
        ("mod.gf", ("mod.lua", 15, 9)),
        ("obj.shortProp", ("obj.js", 6, 2)),
        ("obj.make", ("obj.js", 11, 9)),
        ("obj2.outer", ("obj2.js", 10, 13)),
    ],
)
def test_a_definition_with_a_name_field_is_located_as_before(
    poly_repo: tuple[Path, RecordedGraph],
    target: str,
    definition: tuple[str, int, int],
) -> None:
    report = _plan(poly_repo, target)

    assert _sites(report, "definition") == {definition}


def test_a_python_rename_still_plans_its_call(
    poly_repo: tuple[Path, RecordedGraph],
) -> None:
    report = _plan(poly_repo, "pymod.pyhelper")

    assert _sites(report, "call") == {("pymod.py", 6, 11)}


def test_a_php_variable_is_still_not_an_identifier(
    poly_repo: tuple[Path, RecordedGraph],
) -> None:
    root, _graph = poly_repo
    patcher = Patcher(root)

    with pytest.raises(PatcherError, match="not a whole identifier"):
        patcher.replace_identifier_at("helpers.php", 2, 17, "$x", "$y")
