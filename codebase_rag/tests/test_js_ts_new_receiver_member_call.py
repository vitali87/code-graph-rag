# A JS/TS member call made directly on a construction, `new Box().bump()` or
# `(new Box()).bump()`, recorded INSTANTIATES Box but no CALLS to Box.bump
# (issue #2465), while `const b = new Box(); b.bump()` and Python's
# `Box().bump()` got both. The call name `new Box().bump` reaches the chained
# path, whose base `new Box()` is a construction rather than a typed variable
# or a factory call, so nothing typed it and the edge was dropped. The
# receiver is the constructed instance and must type exactly as the variable
# the same `new` initialises does.
#
# The negative tests pin what stays: a constructed class that lacks the method
# never rebinds it by name to another class, an unknown or external
# construction binds nothing, a receiver that merely contains a `new` is not
# typed as the constructed class, and the INSTANTIATES edges are unchanged.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts import utils as js_ts_utils
from codebase_rag.parsers.js_ts.type_inference import JsTypeInferenceEngine
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_Edges = dict[tuple[str, str], str | None]

_BOX_TS = (
    "export class Base { base(): number { return 0; } }\n"
    "export class Box extends Base {\n"
    "  bump(): number { return 1; }\n"
    "  self(): Box { return this; }\n"
    "}\n"
)
_BOX_JS = (
    "export class Base { base() { return 0; } }\n"
    "export class Box extends Base {\n"
    "  bump() { return 1; }\n"
    "  self() { return this; }\n"
    "}\n"
)


def _index(root: Path, files: dict[str, str], mock_ingestor: MagicMock) -> str:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    create_and_run_updater(root, mock_ingestor, skip_if_missing="typescript")
    return root.name


def _edges(mock_ingestor: MagicMock, rel: cs.RelationshipType) -> _Edges:
    edges: _Edges = {}
    for c in get_relationships(mock_ingestor, rel):
        props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
        edges[(c.args[0][2], c.args[2][2])] = (props or {}).get(cs.KEY_RESOLUTION)
    return edges


def _callees(mock_ingestor: MagicMock, caller: str) -> dict[str, str | None]:
    return {
        callee: resolution
        for (source, callee), resolution in _edges(
            mock_ingestor, cs.RelationshipType.CALLS
        ).items()
        if source == caller
    }


# ---------------------------------------------------------------------------
# The issue's reproduction: a member call on a construction binds exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ext", "box"),
    [(".ts", _BOX_TS), (".js", _BOX_JS)],
    ids=["ts", "js"],
)
@pytest.mark.parametrize(
    "receiver",
    ["new Box()", "(new Box())", "((new Box()))", "(new Box)", "new Box()\n    "],
    ids=["chained", "parenthesized", "double-parens", "no-args", "dot-on-next-line"],
)
def test_member_call_on_construction_binds_the_constructed_class(
    temp_repo: Path, mock_ingestor: MagicMock, ext: str, box: str, receiver: str
) -> None:
    project = _index(
        temp_repo / "newchain",
        {
            f"m{ext}": box
            + "export function viaVar() { const b = new Box(); return b.bump(); }\n"
            + f"export function chained() {{ return {receiver}.bump(); }}\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    # The variable form is the baseline the construction must match.
    assert _callees(mock_ingestor, f"{module}.viaVar") == {
        f"{module}.Box.bump": cs.EdgeResolution.EXACT
    }
    assert _callees(mock_ingestor, f"{module}.chained") == {
        f"{module}.Box.bump": cs.EdgeResolution.EXACT
    }


def test_typescript_type_arguments_do_not_hide_the_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "generic",
        {
            "m.ts": "export class Box<T> { bump(): number { return 1; } }\n"
            "export function chained() { return new Box<number>().bump(); }\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    assert _callees(mock_ingestor, f"{module}.chained") == {
        f"{module}.Box.bump": cs.EdgeResolution.EXACT
    }


def test_multiline_construction_arguments(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "multiline",
        {
            "m.ts": _BOX_TS + "export function chained(a: number) {\n"
            "  return new Box(\n    a.valueOf(),\n    { k: a },\n  ).bump();\n}\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    assert _callees(mock_ingestor, f"{module}.chained") == {
        f"{module}.Box.bump": cs.EdgeResolution.EXACT
    }


@pytest.mark.parametrize(
    ("files", "caller", "callee"),
    [
        (
            {
                "box.ts": _BOX_TS,
                "use.ts": "import { Box } from './box';\n"
                "export function chained() { return new Box().bump(); }\n",
            },
            "use.chained",
            "box.Box.bump",
        ),
        (
            {
                "box.ts": _BOX_TS,
                "index.ts": "export { Box } from './box';\n",
                "use.ts": "import { Box } from './index';\n"
                "export function chained() { return (new Box()).bump(); }\n",
            },
            "use.chained",
            "box.Box.bump",
        ),
        (
            {
                "lib/c.js": "class Zeta { run() { return 1; } }\n"
                "module.exports = Zeta;\n",
                "cjs.js": "const Zeta = require('./lib/c');\n"
                "function chained() { return new Zeta().run(); }\n"
                "module.exports = { chained };\n",
            },
            "cjs.chained",
            "lib.c.Zeta.run",
        ),
    ],
    ids=["esm-import", "re-export", "commonjs-require"],
)
def test_imported_class_construction_binds_across_files(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    files: dict[str, str],
    caller: str,
    callee: str,
) -> None:
    project = _index(temp_repo / "imported", files, mock_ingestor)
    assert _callees(mock_ingestor, f"{project}.{caller}") == {
        f"{project}.{callee}": cs.EdgeResolution.EXACT
    }


def test_inherited_method_binds_on_the_base_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "inherit",
        {"m.ts": _BOX_TS + "export function f() { return new Box().base(); }\n"},
        mock_ingestor,
    )
    module = f"{project}.m"
    assert _callees(mock_ingestor, f"{module}.f") == {
        f"{module}.Base.base": cs.EdgeResolution.EXACT
    }


def test_longer_chain_matches_the_variable_form(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `new Box().self()` is itself a member call on the construction; the hops
    # after it follow the same rules as `b.self().bump()` on a variable.
    project = _index(
        temp_repo / "hops",
        {
            "m.ts": _BOX_TS
            + "export function viaVar() { const b = new Box(); return b.self().bump(); }\n"
            "export function chained() { return new Box().self().bump(); }\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    chained = _callees(mock_ingestor, f"{module}.chained")
    assert chained.get(f"{module}.Box.self") == cs.EdgeResolution.EXACT
    assert chained == _callees(mock_ingestor, f"{module}.viaVar")


# ---------------------------------------------------------------------------
# Negative: what must not bind, and what stays as it was
# ---------------------------------------------------------------------------


def test_constructed_class_without_the_method_binds_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The receiver's type is KNOWN, so a method it lacks is a non-edge, never
    # a licence to bind another class's same-named method.
    project = _index(
        temp_repo / "lacks",
        {
            "m.ts": _BOX_TS + "export class Other { missing(): number { return 2; } }\n"
            "export function f() { return new Box().missing(); }\n",
        },
        mock_ingestor,
    )
    assert _callees(mock_ingestor, f"{project}.m.f") == {}


@pytest.mark.parametrize(
    "receiver",
    ["new URL('x')", "(new Unknown())", "new ns.Box()"],
    ids=["builtin", "unknown", "namespaced"],
)
def test_unknown_construction_does_not_bind_by_method_name(
    temp_repo: Path, mock_ingestor: MagicMock, receiver: str
) -> None:
    # A class the project does not define says nothing about `bump`; the
    # first-party Box.bump must not be reached by its bare name.
    project = _index(
        temp_repo / "unknown",
        {
            "m.ts": _BOX_TS + "declare const ns: { Box: new () => Box };\n"
            f"export function f() {{ return {receiver}.bump(); }}\n",
        },
        mock_ingestor,
    )
    assert f"{project}.m.Box.bump" not in _callees(mock_ingestor, f"{project}.m.f")


@pytest.mark.parametrize(
    "receiver",
    [
        "(new Box() || fallback)",
        "(flag ? new Box() : fallback)",
        "(newBox())",
    ],
    ids=["logical", "ternary", "new-prefixed-identifier"],
)
def test_receiver_that_only_contains_a_construction_is_not_typed_by_it(
    temp_repo: Path, mock_ingestor: MagicMock, receiver: str
) -> None:
    # Only a receiver that IS the construction is the constructed instance;
    # an expression around it can evaluate to something else.
    project = _index(
        temp_repo / "around",
        {
            "m.ts": _BOX_TS + "export class Decoy { bump(): number { return 3; } }\n"
            "declare const fallback: Decoy;\n"
            "declare const flag: boolean;\n"
            "declare function newBox(): Decoy;\n"
            f"export function f() {{ return {receiver}.bump(); }}\n",
        },
        mock_ingestor,
    )
    assert f"{project}.m.Box.bump" not in _callees(mock_ingestor, f"{project}.m.f")


def test_instantiates_edges_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "inst",
        {
            "m.ts": _BOX_TS
            + "export function viaVar() { const b = new Box(); return b.bump(); }\n"
            "export function chained() { return new Box().bump(); }\n"
            "export function parenthesized() { return (new Box()).bump(); }\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    instantiates = _edges(mock_ingestor, cs.RelationshipType.INSTANTIATES)
    assert instantiates == {
        (f"{module}.{caller}", f"{module}.Box"): cs.EdgeResolution.EXACT
        for caller in ("viaVar", "chained", "parenthesized")
    }


def _construction_in(
    language: cs.SupportedLanguage, source: str, receiver: str
) -> str | None:
    # The receiver text as a call name spells it, located at its own offset.
    parsers, _ = load_parsers()
    root = parsers[language].parse(source.encode()).root_node
    start = len(source[: source.index(receiver)].encode())
    node = js_ts_utils.construction_at(root, start, receiver)
    return js_ts_utils.extract_constructor_name(node) if node is not None else None


@pytest.mark.parametrize(
    ("language", "source", "receiver"),
    [
        (cs.SupportedLanguage.TS, "new Box().bump();", "new Box()"),
        (
            cs.SupportedLanguage.JS,
            "( (new Box(a.b, c())) ).bump();",
            "( (new Box(a.b, c())) )",
        ),
        (
            cs.SupportedLanguage.TS,
            "new Box<Map<string, number>>().bump();",
            "new Box<Map<string, number>>()",
        ),
        (cs.SupportedLanguage.TSX, "(new Box).bump();", "(new Box)"),
        (
            cs.SupportedLanguage.JS,
            "new Box(\n  1,\n)\n  .bump();",
            "new Box(\n  1,\n)\n  ",
        ),
        (cs.SupportedLanguage.TS, "const s = 'é'; new Box().bump();", "new Box()"),
    ],
    ids=["chained", "parens", "type-args", "no-args", "multiline", "non-ascii-before"],
)
def test_construction_at_reads_the_construction(
    language: cs.SupportedLanguage, source: str, receiver: str
) -> None:
    assert _construction_in(language, source, receiver) == "Box"


@pytest.mark.parametrize(
    ("source", "receiver"),
    [
        ("(new Box() || other).bump();", "(new Box() || other)"),
        ("(flag ? new Box() : other).bump();", "(flag ? new Box() : other)"),
        ("newBox().bump();", "newBox()"),
        ("renew(new Box()).bump();", "renew(new Box())"),
        ("new Box()().bump();", "new Box()()"),
        ("(a)(new Box()).bump();", "(a)(new Box())"),
        ("(/* c */ new Box()).bump();", "(/* c */ new Box())"),
        ("new Box().bump();", "new Box"),
    ],
    ids=[
        "logical",
        "ternary",
        "new-prefixed-call",
        "argument",
        "called-construction",
        "call-of-parens",
        "comment",
        "partial-span",
    ],
)
def test_construction_at_rejects_any_other_expression(
    source: str, receiver: str
) -> None:
    assert _construction_in(cs.SupportedLanguage.TS, source, receiver) is None


def test_property_read_on_construction_is_not_a_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `new Box().bump` without parentheses reads the method, it does not call it.
    project = _index(
        temp_repo / "read",
        {"m.ts": _BOX_TS + "export function f() { return new Box().bump; }\n"},
        mock_ingestor,
    )
    assert f"{project}.m.Box.bump" not in _callees(mock_ingestor, f"{project}.m.f")


# ---------------------------------------------------------------------------
# A constructor name bound in the caller's scope is not the module class
# ---------------------------------------------------------------------------
#
# `new Box()` reads whatever `Box` is in scope at the construction. A
# parameter, a local, a loop or catch binding, or a function or class declared
# in an enclosing callable is a different value from the module's class, often
# a constructor the caller is handed, so typing the instance as the module
# class gave its methods callers that never reach them. The variable form
# `const b = new Box(); b.bump()` read the module class there as well. `{use}`
# is filled with both forms. A second class defining `bump` keeps the
# untyped-receiver fallback, which binds a method name unique in the module,
# out of the variable form's answer: what is pinned is that the construction
# no longer TYPES the receiver as the module class.

_DECOY = "export class Decoy { bump() { return 3; } }\n"

_FORMS = {
    "chained": "return new Box().bump();",
    "variable": "const b = new Box(); return b.bump();",
}

_SHADOWS = {
    "parameter": "export function f(Box) { {use} }\n",
    "destructured-parameter": "export function f({ Box }) { {use} }\n",
    "default-parameter": "export function f(Box = Base) { {use} }\n",
    "rest-parameter": "export function f(...Box) { {use} }\n",
    "arrow-parameter": "export const f = (Box) => { {use} };\n",
    "outer-parameter": "export function f(Box) { return () => { {use} }; }\n",
    "const": "export function f(pick) { const Box = pick(); {use} }\n",
    "let": "export function f(pick) { let Box = pick(); {use} }\n",
    "var": "export function f(pick) { var Box = pick(); {use} }\n",
    "var-hoisted-from-block": (
        "export function f(pick, x) { if (x) { var Box = pick(); } {use} }\n"
    ),
    "nested-function": "export function f() { function Box() {} {use} }\n",
    "nested-class": (
        "export function f() { class Box { bump() { return 2; } } {use} }\n"
    ),
    "class-expression": (
        "export function f() { const Box = class { bump() { return 2; } }; {use} }\n"
    ),
    "named-class-expression": (
        "export const K = class Box { bump() { return 2; } make() { {use} } };\n"
    ),
    "named-function-expression": "export const f = function Box() { {use} };\n",
    "catch": "export function f() { try { return 0; } catch (Box) { {use} } }\n",
    "for-of": "export function f(xs) { for (const Box of xs) { {use} } }\n",
    "for-in": "export function f(o) { for (let Box in o) { {use} } }\n",
    "for-var-after-loop": ("export function f(xs) { for (var Box of xs) {} {use} }\n"),
    "for-initializer": (
        "export function f(pick) { for (let Box = pick(); ; ) { {use} } }\n"
    ),
}


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
@pytest.mark.parametrize("ext", [".ts", ".js"], ids=["ts", "js"])
@pytest.mark.parametrize("shadow", list(_SHADOWS), ids=list(_SHADOWS))
def test_locally_bound_constructor_is_not_the_module_class(
    temp_repo: Path, mock_ingestor: MagicMock, shadow: str, ext: str, form: str
) -> None:
    box = _BOX_TS if ext == ".ts" else _BOX_JS
    source = _SHADOWS[shadow].replace("{use}", _FORMS[form])
    project = _index(
        temp_repo / "shadow", {f"m{ext}": box + _DECOY + source}, mock_ingestor
    )
    target = f"{project}.m.Box.bump"
    callers = {
        source_qn
        for (source_qn, callee) in _edges(mock_ingestor, cs.RelationshipType.CALLS)
        if callee == target
    }
    assert callers == set(), source


@pytest.mark.parametrize("ext", [".ts", ".js"], ids=["ts", "js"])
def test_parameter_constructor_on_a_construction_binds_nothing(
    temp_repo: Path, mock_ingestor: MagicMock, ext: str
) -> None:
    # The review's reproduction: the module's only `bump` and a parameter
    # named after its class. The chained form has no by-name fallback, so it
    # binds nothing at all, while `ok` still reaches the module class.
    project = _index(
        temp_repo / "param_ctor",
        {
            f"m{ext}": "class Box { bump() { return 1; } }\n"
            "function f(Box) { return new Box().bump(); }\n"
            "function ok() { return new Box().bump(); }\n"
            "module.exports = { f, ok };\n",
        },
        mock_ingestor,
    )
    module = f"{project}.m"
    assert _callees(mock_ingestor, f"{module}.f") == {}
    assert _callees(mock_ingestor, f"{module}.ok") == {
        f"{module}.Box.bump": cs.EdgeResolution.EXACT
    }


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
def test_imported_class_shadowed_by_a_parameter_is_refused(
    temp_repo: Path, mock_ingestor: MagicMock, form: str
) -> None:
    project = _index(
        temp_repo / "imported_shadow",
        {
            "box.ts": _BOX_TS,
            "use.ts": "import { Box } from './box';\n"
            + _DECOY
            + "export function ok() { return new Box().bump(); }\n"
            + "export function f(Box: any) { {use} }\n".replace("{use}", _FORMS[form]),
        },
        mock_ingestor,
    )
    target = f"{project}.box.Box.bump"
    assert _callees(mock_ingestor, f"{project}.use.ok") == {
        target: cs.EdgeResolution.EXACT
    }
    assert target not in _callees(mock_ingestor, f"{project}.use.f")


_UNSHADOWED = {
    "plain": "export function f() { {use} }\n",
    "other-names": (
        "export function f(Other, { x }, ...rest) { const y = 1; let z = 2; {use} }\n"
    ),
    "default-value-reads-box": "export function f(x = Box) { {use} }\n",
    "sibling-function": (
        "export function g(Box) { return Box; }\nexport function f() { {use} }\n"
    ),
    "nested-function-parameter": (
        "export function f() { const g = (Box) => Box; {use} }\n"
    ),
    "block-scoped-elsewhere": (
        "export function f(x) { if (x) { const Box = 1; } {use} }\n"
    ),
    "other-catch": "export function f() { try {} catch (Box) {} {use} }\n",
    "loop-scoped-elsewhere": (
        "export function f(xs) { for (const Box of xs) {} {use} }\n"
    ),
    "own-method": "export class C { m() { {use} } }\n",
}


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
@pytest.mark.parametrize("ext", [".ts", ".js"], ids=["ts", "js"])
@pytest.mark.parametrize("case", list(_UNSHADOWED), ids=list(_UNSHADOWED))
def test_module_class_still_binds_when_nothing_shadows_it(
    temp_repo: Path, mock_ingestor: MagicMock, case: str, ext: str, form: str
) -> None:
    box = _BOX_TS if ext == ".ts" else _BOX_JS
    source = _UNSHADOWED[case].replace("{use}", _FORMS[form])
    project = _index(temp_repo / "unshadowed", {f"m{ext}": box + source}, mock_ingestor)
    caller = "C.m" if case == "own-method" else "f"
    assert _callees(mock_ingestor, f"{project}.m.{caller}") == {
        f"{project}.m.Box.bump": cs.EdgeResolution.EXACT
    }, source


# ---------------------------------------------------------------------------
# A function declared in a nested block is block-scoped in strict code
# ---------------------------------------------------------------------------
#
# ES modules, TypeScript, class bodies and "use strict" code scope a function
# declared inside a block to that block, like `let`. Treating it as bound
# through the whole function hid the module class after the block, and the
# real `f -> Box.bump` edge was lost. Only a sloppy-mode script hoists it
# (Annex B): there it really is visible after the block, so it still shadows.

_BLOCK_FUNCTIONS = {
    "bare-block": "export function f() { { function Box() {} } {use} }\n",
    "if-block": "export function f(x) { if (x) { function Box() {} } {use} }\n",
    "class-method": ("export class C { m() { { function Box() {} } {use} } }\n"),
}


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
@pytest.mark.parametrize("ext", [".ts", ".js"], ids=["ts", "js"])
@pytest.mark.parametrize("case", list(_BLOCK_FUNCTIONS), ids=list(_BLOCK_FUNCTIONS))
def test_block_function_does_not_hide_the_class_after_its_block(
    temp_repo: Path, mock_ingestor: MagicMock, case: str, ext: str, form: str
) -> None:
    box = _BOX_TS if ext == ".ts" else _BOX_JS
    source = _BLOCK_FUNCTIONS[case].replace("{use}", _FORMS[form])
    project = _index(
        temp_repo / "block_fn", {f"m{ext}": box + _DECOY + source}, mock_ingestor
    )
    caller = "C.m" if case == "class-method" else "f"
    callees = _callees(mock_ingestor, f"{project}.m.{caller}")
    assert callees.get(f"{project}.m.Box.bump") == cs.EdgeResolution.EXACT, source


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
def test_use_strict_script_scopes_a_block_function_to_its_block(
    temp_repo: Path, mock_ingestor: MagicMock, form: str
) -> None:
    project = _index(
        temp_repo / "strict_script",
        {
            "m.js": "'use strict';\n"
            "class Box { bump() { return 1; } }\n"
            "class Decoy { bump() { return 3; } }\n"
            "function f() { { function Box() {} } {use} }\n".replace(
                "{use}", _FORMS[form]
            )
            + "module.exports = { f, Box, Decoy };\n",
        },
        mock_ingestor,
    )
    callees = _callees(mock_ingestor, f"{project}.m.f")
    assert callees.get(f"{project}.m.Box.bump") == cs.EdgeResolution.EXACT


_STILL_SHADOWED = {
    "used-inside-its-block": ("export function f() { { function Box() {} {use} } }\n"),
    "top-level-declared-after-use": (
        "export function f() { {use} function Box() {} }\n"
    ),
}


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
@pytest.mark.parametrize("ext", [".ts", ".js"], ids=["ts", "js"])
@pytest.mark.parametrize("case", list(_STILL_SHADOWED), ids=list(_STILL_SHADOWED))
def test_function_declaration_still_shadows_where_it_is_in_scope(
    temp_repo: Path, mock_ingestor: MagicMock, case: str, ext: str, form: str
) -> None:
    box = _BOX_TS if ext == ".ts" else _BOX_JS
    source = _STILL_SHADOWED[case].replace("{use}", _FORMS[form])
    project = _index(
        temp_repo / "still_shadowed", {f"m{ext}": box + _DECOY + source}, mock_ingestor
    )
    assert f"{project}.m.Box.bump" not in _callees(mock_ingestor, f"{project}.m.f")


@pytest.mark.parametrize("form", list(_FORMS), ids=list(_FORMS))
def test_sloppy_script_block_function_still_shadows_after_its_block(
    temp_repo: Path, mock_ingestor: MagicMock, form: str
) -> None:
    # No module syntax and no "use strict": Annex B makes `Box` the block's
    # function (or undefined) after the block, never the class.
    project = _index(
        temp_repo / "sloppy_script",
        {
            "m.js": "class Box { bump() { return 1; } }\n"
            "class Decoy { bump() { return 3; } }\n"
            "function f() { { function Box() {} } {use} }\n".replace(
                "{use}", _FORMS[form]
            )
            + "module.exports = { f, Box, Decoy };\n",
        },
        mock_ingestor,
    )
    assert f"{project}.m.Box.bump" not in _callees(mock_ingestor, f"{project}.m.f")


# ---------------------------------------------------------------------------
# The binding scan is linear in the enclosing function, not per construction
# ---------------------------------------------------------------------------


def test_binding_scan_is_not_repeated_per_construction(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Each construction asked the whole enclosing function which names it
    # binds, so a function with n constructions (and n declarators) did n
    # full scans: O(n^2). The answer is indexed once per function.
    n = 200
    body = "".join(
        f"  const b{i} = new Box(); b{i}.bump();\n  new Box().bump();\n"
        for i in range(n)
    )
    original = JsTypeInferenceEngine._js_declarator_span
    with patch.object(
        JsTypeInferenceEngine,
        "_js_declarator_span",
        autospec=True,
        side_effect=original,
    ) as scanned:
        project = _index(
            temp_repo / "many",
            {"m.ts": _BOX_TS + "export function f() {\n" + body + "}\n"},
            mock_ingestor,
        )
    assert _callees(mock_ingestor, f"{project}.m.f") == {
        f"{project}.m.Box.bump": cs.EdgeResolution.EXACT
    }
    assert scanned.call_count <= 10 * n, scanned.call_count
