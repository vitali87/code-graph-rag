"""A call through the name an object literal is bound to reaches its member.

An object literal's function registers under its scope without the object's
name (`const api = { fetchUser() {} }` gives `<module>.fetchUser`, issue
#2435), and nothing linked `api` to its members. `api.fetchUser()` therefore
got no CALLS edge in the same module or through any import, `callers` was
empty, and `cgr rename` rewrote only the definition, leaving `view.ts`
calling a name that no longer existed (issue #2763).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_rename_op import _index
from evals.cgr_graph import _StatefulIngestor

_API = (
    "export const api = {\n"
    "  fetchUser(id: number): string {\n"
    "    return String(id);\n"
    "  },\n"
    "};\n"
)
_VIEW = (
    'import { api } from "./api";\n\n'
    "export function show(): string {\n"
    "  return api.fetchUser(1);\n"
    "}\n"
)

_FILES = {
    "api.ts": _API,
    "view.ts": _VIEW,
    "same.mjs": (
        "export const shared = { go() { return 1; } };\n"
        "export function useShared() { return shared.go(); }\n"
        "export function local() {\n"
        "  const tools = { build: () => 2, make() { return 3; } };\n"
        "  return tools.build() + tools.make();\n"
        "}\n"
    ),
    "other.mjs": (
        'import { shared } from "./same.mjs";\n'
        'import * as ns from "./same.mjs";\n'
        "export function viaNamed() { return shared.go(); }\n"
        "export function viaNs() { return ns.shared.go(); }\n"
    ),
    "cjs_obj.js": (
        "const shared2 = { go2() { return 2; } };\nmodule.exports = { shared2 };\n"
    ),
    "cjs_use.js": (
        'const { shared2 } = require("./cjs_obj");\n'
        "function viaCjs() { return shared2.go2(); }\n"
        "module.exports = { viaCjs };\n"
    ),
    "defapi.mjs": "export default { run() { return 1; } };\n",
    "defuse.mjs": (
        'import api from "./defapi.mjs";\n'
        "export function viaDefault() { return api.run(); }\n"
    ),
    "neg.mjs": (
        'import { shared } from "./same.mjs";\n'
        "const cfg = { retry: { delay() { return 0; } } };\n"
        "export function nested() { return cfg.delay(); }\n"
        "export function missing() { return shared.stop(); }\n"
        "export function passed(f) {\n"
        "  const opts = f({ go() { return 9; } });\n"
        "  return opts.go();\n"
        "}\n"
    ),
}


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], str]:
    root = tmp_path_factory.mktemp("js2763") / "objts"
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="typescript")
    edges: dict[tuple[str, str], str] = {}
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        caller = str(c.args[0][2]).removeprefix("objts.")
        callee = str(c.args[2][2]).removeprefix("objts.")
        edges[(caller, callee)] = str(props.get(cs.KEY_RESOLUTION))
    return edges


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("view.show", "api.fetchUser"),
        ("same.useShared", "same.go"),
        ("same.local", "same.local.build"),
        ("same.local", "same.local.make"),
        ("other.viaNamed", "same.go"),
        ("other.viaNs", "same.go"),
        ("cjs_use.viaCjs", "cjs_obj.go2"),
        ("defuse.viaDefault", "defapi.run"),
    ],
    ids=[
        "ts-named-import",
        "same-module",
        "function-local-arrow",
        "function-local-method",
        "esm-named-import",
        "namespace-import",
        "commonjs-destructure",
        "default-export",
    ],
)
def test_a_bound_object_member_call_resolves_exact(
    calls: dict[tuple[str, str], str], caller: str, callee: str
) -> None:
    assert calls.get((caller, callee)) == cs.EdgeResolution.EXACT, calls


def test_other_objects_and_keys_are_not_bound(
    calls: dict[tuple[str, str], str],
) -> None:
    # Negatives: a nested object's member is not its parent's
    # (`cfg.delay()` names no member of `cfg`), a key the object does not
    # have is no edge, and a literal passed to a call binds no name.
    assert not [c for (src, c) in calls if src == "neg.nested"], calls
    assert not [c for (src, c) in calls if src == "neg.missing"], calls
    assert ("neg.passed", "neg.go") not in calls, calls


def test_rename_rewrites_the_importing_call_site(temp_repo: Path) -> None:
    (temp_repo / "api.ts").write_text(_API, encoding="utf-8")
    (temp_repo / "view.ts").write_text(_VIEW, encoding="utf-8")
    graph = _index(temp_repo, MagicMock())
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.api.fetchUser",
        "loadUser",
        allow_heuristic=True,
    )
    assert report.applied, report.message
    assert "return api.loadUser(1);" in (temp_repo / "view.ts").read_text()
    assert "loadUser(id: number)" in (temp_repo / "api.ts").read_text()


def test_an_incremental_sync_of_the_caller_keeps_the_edge(temp_repo: Path) -> None:
    # Re-parsing only view.ts leaves api.ts to rehydration from the graph,
    # so the member's binding has to survive the round trip.
    parsers, queries = load_parsers()
    if "typescript" not in parsers:
        pytest.skip("typescript parser not available")
    (temp_repo / "api.ts").write_text(_API, encoding="utf-8")
    (temp_repo / "view.ts").write_text(_VIEW, encoding="utf-8")
    store = _StatefulIngestor()

    def sync(force: bool) -> set[str]:
        GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        ).run(force=force)
        return {
            str(target)
            for _, source, rel, _, target in store.edges
            if rel == cs.RelationshipType.CALLS.value
            and str(source).endswith(".view.show")
        }

    assert any(t.endswith(".api.fetchUser") for t in sync(force=True))
    (temp_repo / "view.ts").write_text(_VIEW + "// touched\n", encoding="utf-8")
    assert any(t.endswith(".api.fetchUser") for t in sync(force=False))
