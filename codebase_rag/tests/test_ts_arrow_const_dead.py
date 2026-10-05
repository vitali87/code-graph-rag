"""An unused module-level arrow const is dead code, like an unused function.

`const foo = () => {}` declares `foo` with the arrow as its definition, but
the assignment pass also emitted `REFERENCES module -> foo` for it, as if
the module used the function it was defining. The module is a reachability
root, so every arrow const was live and `cgr dead-code` never reported one,
while the same function written as `function foo() {}` was (issue #2857).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.tests.test_js_ts_separate_export_class_members import (
    _dead,
    _index,
)

_ISSUE_TS = """\
const unusedArrow = () => 1;

const unusedAsyncArrow = async () => 2;

function unusedFunc() {
    return 3;
}

export function entry() {
    return 4;
}
"""


def test_the_issue_reports_the_arrow_consts(tmp_path: Path) -> None:
    dead = _dead(_index(tmp_path, {"m.ts": _ISSUE_TS}))
    assert {"m.unusedArrow", "m.unusedAsyncArrow", "m.unusedFunc"} <= dead, dead
    assert "m.entry" not in dead, dead


@pytest.mark.parametrize(
    ("file_name", "declaration", "name"),
    [
        ("m.js", "const unusedArrow = () => 1;", "unusedArrow"),
        ("m.js", "let unusedExpr = function () { return 1; };", "unusedExpr"),
        ("m.js", "var unusedGen = function* () { yield 1; };", "unusedGen"),
        ("m.ts", "const typed: () => number = () => 1;", "typed"),
        ("m.tsx", "const Unused = () => <div />;", "Unused"),
    ],
)
def test_every_inline_function_declarator_can_be_dead(
    tmp_path: Path, file_name: str, declaration: str, name: str
) -> None:
    src = f"{declaration}\nexport function entry() {{ return 4; }}\n"
    dead = _dead(_index(tmp_path, {file_name: src}))
    assert f"m.{name}" in dead, dead


_LIVE_TSX = """\
import { register } from "./bus";

const called = () => 1;
const callback = (x: number) => x;
const Card = () => <div />;
function named() { return 2; }
const alias = named;
const handlers: { [k: string]: () => number } = {};
handlers.onLoad = () => 3;

export const exported = () => 4;

export function App() {
    register(alias);
    return [called(), [1].map(callback), <Card />];
}
"""


@pytest.mark.parametrize(
    "name", ["called", "callback", "Card", "named", "exported", "App"]
)
def test_an_arrow_const_that_is_used_stays_live(tmp_path: Path, name: str) -> None:
    # Negatives: a call, a callback, a JSX render, an alias to a named
    # function and an export each keep their function reachable.
    graph = _index(
        tmp_path,
        {"app.tsx": _LIVE_TSX, "bus.ts": "export function register(f: unknown) {}\n"},
    )
    assert f"app.{name}" not in _dead(graph)


@pytest.mark.parametrize(
    ("file_name", "export"),
    [
        ("m.ts", "export { later };"),
        ("m.ts", "export default later;"),
        ("m.js", "module.exports = { later };"),
        ("m.js", "exports.later = later;"),
    ],
)
def test_an_arrow_const_exported_after_its_declaration_stays_live(
    tmp_path: Path, file_name: str, export: str
) -> None:
    # Negative: a separate export statement still makes it a public root.
    src = f"const later = () => 1;\n{export}\n"
    assert "m.later" not in _dead(_index(tmp_path, {file_name: src}))
