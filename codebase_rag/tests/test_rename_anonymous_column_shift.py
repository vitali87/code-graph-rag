"""Issue #2924: an inline function after a renamed site does not roll it back.

An inline arrow or closure is named by its position, `anonymous_<row>_<col>`.
Renaming a call site or JSX tag earlier on the same line shifts its column,
so the structural delta pairs the old and new names as a rename, and the
contract called that an unexpected rename and rolled the whole rename back
(`<TodoItem onToggle={() => ...} />`, `withRetry(() => 42)`, `apply(|v| v + 1)`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.editing import rename_expectation, verify
from codebase_rag.editing.rename import rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import RenameFinding, StructuralDelta
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "anonren"

JS_UTIL = "export function withRetry(fn) {\n  return fn();\n}\n"
JS_MAIN = (
    'import { withRetry } from "./util.js";\n\n'
    "export function load() {\n  return withRetry(() => 42);\n}\n"
)
# The arrow goes to an external callee: a first-party one that invokes it
# adds a flow edge from another file, a separate rollback cause.
JS_ONE_LINE = "export function load() { return setTimeout(() => 42, 0); }\n"
TSX_ITEM = (
    "export function TodoItem(props: { onToggle: () => void }) {\n"
    "  return <li onClick={props.onToggle} />;\n}\n"
)
TSX_LIST = (
    'import { TodoItem } from "./TodoItem";\n\n'
    "export function TodoList(props: { toggle: (id: number) => void }) {\n"
    "  return <TodoItem onToggle={() => props.toggle(1)} />;\n}\n"
)
RS_MAIN = (
    "fn apply_twice<F: Fn(i32) -> i32>(f: F, v: i32) -> i32 {\n    f(f(v))\n}\n\n"
    "fn main() {\n    let r = apply_twice(|v| v + 1, 3);\n"
    '    println!("{}", r);\n}\n'
)
JS_HOOK = "export function useTodos() {\n  return [];\n}\n"
JS_USES_HOOK = (
    'import { useTodos } from "./hooks.js";\n\n'
    "export function view() {\n  const todos = useTodos();\n"
    "  return todos.map((t) => t);\n}\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _rename(
    temp_repo: Path, files: dict[str, str], target: str, new_name: str
) -> tuple[bool, str]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        _write(root, rel, text)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)

    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, dict(params) if params is not None else None)

    report = rename(
        root,
        fetch_all,
        PROJECT,
        f"{PROJECT}.{target}",
        new_name,
        allow_heuristic=True,
        reingest=updater.reingest,
    )
    return report.applied, report.message


@pytest.mark.parametrize(
    ("files", "target", "new_name", "rewritten", "expected"),
    [
        (
            {"js/util.js": JS_UTIL, "js/main.js": JS_MAIN},
            "js.util.withRetry",
            "retryIt",
            "js/main.js",
            "return retryIt(() => 42);",
        ),
        (
            {"src/TodoItem.tsx": TSX_ITEM, "src/TodoList.tsx": TSX_LIST},
            "src.TodoItem.TodoItem",
            "TodoRow",
            "src/TodoList.tsx",
            "<TodoRow onToggle={() => props.toggle(1)} />",
        ),
        (
            {"Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n'}
            | {"src/main.rs": RS_MAIN},
            "src.main.apply_twice",
            "twice",
            "src/main.rs",
            "let r = twice(|v| v + 1, 3);",
        ),
        (
            {"js/main.js": JS_ONE_LINE},
            "js.main.load",
            "loadAll",
            "js/main.js",
            "export function loadAll() { return setTimeout(() => 42, 0); }",
        ),
    ],
    ids=["js-call", "tsx-element", "rust-closure", "enclosing-function"],
)
def test_a_rename_before_an_inline_function_on_its_line_applies(
    temp_repo: Path,
    files: dict[str, str],
    target: str,
    new_name: str,
    rewritten: str,
    expected: str,
) -> None:
    applied, message = _rename(temp_repo, files, target, new_name)
    assert applied, message
    assert expected in (temp_repo / PROJECT / rewritten).read_text()


def _delta(renamed: list[tuple[str, str]]) -> StructuralDelta:
    return {
        "paths": ["js/main.js"],
        "reparsed": ["js/main.js"],
        "affected": [],
        "removed_files": [],
        "symbols": {
            "added": [],
            "removed": [],
            "renamed": [
                RenameFinding(old=old, new=new, path="js/main.js")
                for old, new in renamed
            ],
            "changed": [],
        },
        "dangling_callers": [],
        "dangling_importers": [],
        "signature_changes": [],
        "arity_findings": [],
        "new_duplicates": [],
        "new_import_cycles": [],
        "stale_importers": [],
        "tests_reaching": [],
        "call_sites": {"before": 1, "after": 1},
        "reingest_ms": 1.0,
        "delta_ms": 1.0,
    }


REQUESTED = ("p.js.util.withRetry", "p.js.util.retryIt")


def test_a_renumbered_function_nested_in_a_renumbered_one_is_no_rename() -> None:
    verdict = verify(
        rename_expectation([REQUESTED], True),
        _delta(
            [
                REQUESTED,
                ("p.js.main.load.anonymous_3_19", "p.js.main.load.anonymous_3_17"),
                (
                    "p.js.main.load.anonymous_3_19.anonymous_3_25",
                    "p.js.main.load.anonymous_3_17.anonymous_3_23",
                ),
                (
                    "p.js.main.load.anonymous_3_19.inner",
                    "p.js.main.load.anonymous_3_17.inner",
                ),
            ]
        ),
    )
    assert verdict.ok, verdict.failures


# Negative: what must not change.


def test_a_rename_with_no_inline_function_after_the_site_still_applies(
    temp_repo: Path,
) -> None:
    applied, message = _rename(
        temp_repo,
        {"js/hooks.js": JS_HOOK, "js/view.js": JS_USES_HOOK},
        "js.hooks.useTodos",
        "useTodoStore",
    )
    assert applied, message


@pytest.mark.parametrize(
    "pair",
    [
        ("p.js.main.load.anonymous_3_19", "p.js.main.load.anonymous_4_19"),
        ("p.js.main.load.anonymous_3_19", "p.js.main.save.anonymous_3_17"),
        ("p.js.main.load.callback", "p.js.main.load.anonymous_3_17"),
        ("p.js.main.load.anonymous_3_19", "p.js.main.load.callback"),
        ("p.js.main.load.anonymous_3_19.inner", "p.js.main.load.anonymous_3_17.outer"),
    ],
    ids=[
        "other-row",
        "other-parent",
        "named-to-anonymous",
        "anonymous-to-named",
        "renamed-child",
    ],
)
def test_any_other_rename_is_still_unexpected(pair: tuple[str, str]) -> None:
    verdict = verify(rename_expectation([REQUESTED], True), _delta([REQUESTED, pair]))
    assert not verdict.ok
    assert any(pair[0] in failure for failure in verdict.failures)
