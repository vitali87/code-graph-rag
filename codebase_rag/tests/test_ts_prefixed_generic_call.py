"""Issue #2930: `await f<T>(x)` and `!f<T>(x)` record the call of `f`.

tree-sitter-typescript binds a prefix operator tighter than a call with
type arguments: `await client.call<number>(x)` parses as a call whose
callee is `await client.call`, and `!isKind<string>(v)` as one whose callee
is `!isKind`. No case named such a callee, so the call was dropped: vscode
has 1,107 such sites, and on its agentHost 0 of 485 were linked.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

LIB = """\
export function isKind<T>(value: unknown): value is T {
\treturn value !== undefined;
}

export function make<T>(): T {
\treturn undefined as T;
}

export class Client {
\tasync call<T>(command: string): Promise<T> {
\t\treturn undefined as T;
\t}
}
"""

APP = """\
import {isKind, make, Client} from './lib';

export function negated(value: unknown) {
\tif (!isKind<string>(value)) {
\t\treturn false;
\t}
\treturn true;
}

export async function awaitedMethod(client: Client) {
\tconst result = await client.call<number>('version');
\treturn result;
}

export function typeOf() {
\treturn typeof make<number>();
}

export function voided() {
\tvoid make<string>();
}

export async function notAwaited(client: Client) {
\treturn !await client.call<boolean>('ok');
}

export async function awaitedPlain(client: Client) {
\treturn await client.call('version');
}

export function negatedPlain(value: unknown) {
\treturn !isKind(value);
}

export async function parenthesized(client: Client) {
\treturn await (client.call<number>('version'));
}
"""


@pytest.fixture(scope="module", params=["app.ts", "app.tsx"])
def graph(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> RecordedGraph:
    root = tmp_path_factory.mktemp("tsawait") / "tsawait"
    _write(root, "src/lib.ts", LIB)
    _write(root, f"src/{request.param}", APP)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}src.app.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("negated", "src.lib.isKind"),
        ("awaitedMethod", "src.lib.Client.call"),
        ("typeOf", "src.lib.make"),
        ("voided", "src.lib.make"),
        ("notAwaited", "src.lib.Client.call"),
    ],
)
def test_a_prefixed_call_with_type_arguments_is_recorded(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller) == {callee: "exact"}


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("awaitedPlain", "src.lib.Client.call"),
        ("negatedPlain", "src.lib.isKind"),
        ("parenthesized", "src.lib.Client.call"),
    ],
)
def test_a_call_that_already_bound_still_binds(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller) == {callee: "exact"}
