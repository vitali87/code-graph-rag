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
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.js_ts import utils as js_ts_utils
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
    ["new Box()", "(new Box())", "((new Box()))", "(new Box)"],
    ids=["chained", "parenthesized", "double-parens", "no-args"],
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


@pytest.mark.parametrize(
    ("language", "text", "constructor"),
    [
        (cs.SupportedLanguage.TS, "new Box()", "Box"),
        (cs.SupportedLanguage.JS, "( (new Box(a.b, c())) )", "Box"),
        (cs.SupportedLanguage.TS, "new Box<Map<string, number>>()", "Box"),
        (cs.SupportedLanguage.TSX, "(new Box)", "Box"),
        (cs.SupportedLanguage.JS, "\n  new Box(\n  1,\n)", "Box"),
    ],
)
def test_construction_receiver_reads_the_construction(
    language: cs.SupportedLanguage, text: str, constructor: str
) -> None:
    node = js_ts_utils.construction_receiver(text, language)
    assert node is not None
    assert js_ts_utils.extract_constructor_name(node) == constructor


@pytest.mark.parametrize(
    "text",
    [
        "(new Box() || other)",
        "(flag ? new Box() : other)",
        "newBox()",
        "renew(new Box())",
        "new Box(); other",
        "new Box(",
        "(a)(new Box())",
        "/* c */ new Box()",
    ],
)
def test_construction_receiver_rejects_any_other_expression(text: str) -> None:
    assert js_ts_utils.construction_receiver(text, cs.SupportedLanguage.TS) is None


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
