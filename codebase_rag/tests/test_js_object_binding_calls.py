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
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_rename_op import _index
from codebase_rag.types_defs import NodeType
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
    "shadow.mjs": (
        "const api = { run() { return 1; } };\n"
        "export function viaParam(api) { return api.run(); }\n"
        "export function viaLocal(s) { const api = JSON.parse(s); return api.run(); }\n"
        "export function viaModule() { return api.run(); }\n"
        "function make() { return { run() { return 2; } }; }\n"
        "export function viaParamLater(api) { return api.run(); }\n"
    ),
    "dup.mjs": (
        "const a = { go() { return 1; } };\n"
        "const b = { go() { return 2; } };\n"
        "export function useA() { return a.go(); }\n"
        "export function useB() { return b.go(); }\n"
    ),
    # Bot review on PR #3118: sibling blocks binding one name, and a module
    # exporting another binding under a private local's name.
    "blocks.mjs": (
        "export function both(flag) {\n"
        "  if (flag) {\n"
        "    const api = { run() { return 1; } };\n"
        "    return api.run();\n"
        "  }\n"
        "  {\n"
        "    const api = { run() { return 2; } };\n"
        "    return api.run();\n"
        "  }\n"
        "}\n"
    ),
    # CodeRabbit, PR #3118: sibling blocks with different keys share the
    # binding too; a local `function api` and a callback's `api` parameter
    # shadow the module's `api`.
    "keys.mjs": (
        "export function differentKeys(flag) {\n"
        "  if (flag) {\n"
        "    const api = { run() { return 1; } };\n"
        "    return api.run();\n"
        "  }\n"
        "  {\n"
        "    const api = { other() { return 2; } };\n"
        "    return api.run();\n"
        "  }\n"
        "}\n"
    ),
    "fnshadow.mjs": (
        "const api = { run() { return 1; } };\n"
        "export function viaDeclaration() {\n"
        "  function api() {}\n"
        "  api.run = () => 2;\n"
        "  return api.run();\n"
        "}\n"
        "export function viaCallback(items) {\n"
        "  return items.map((api) => api.run());\n"
        "}\n"
        "export function stillModule() { return api.run(); }\n"
    ),
    # Bot review on PR #3118: a later spread may replace a member, and a
    # callback's own `const api` shadows the module's `api` inside it.
    "spread.mjs": (
        "const base = { run() { return 2; } };\n"
        "const api = { run() { return 1; }, ...base };\n"
        "export function use() { return api.run(); }\n"
    ),
    "cbdecl.mjs": (
        "const api = { run() { return 1; } };\n"
        "export function use(items, text) {\n"
        "  items.forEach(() => {\n"
        "    const api = JSON.parse(text);\n"
        "    api.run();\n"
        "  });\n"
        "}\n"
    ),
    "exported.mjs": (
        "const api = { run() { return 1; } };\n"
        "const publicApi = { run() { return 2; } };\n"
        "export { publicApi as api };\n"
    ),
    "exporteduse.mjs": (
        'import { api } from "./exported.mjs";\n'
        "export function viaExport() { return api.run(); }\n"
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


def _targets(calls: dict[tuple[str, str], str], caller: str) -> dict[str, str]:
    return {callee: res for (src, callee), res in calls.items() if src == caller}


@pytest.mark.parametrize(
    "caller", ["shadow.viaParam", "shadow.viaLocal", "shadow.viaParamLater"]
)
def test_a_local_of_the_bindings_name_shadows_it(
    calls: dict[tuple[str, str], str], caller: str
) -> None:
    # `api` there is the parameter or the local, not the module's object, so
    # its `run` is no exact target (a shared calls cache must not make it one).
    targets = _targets(calls, caller)
    assert targets.get("shadow.run") != cs.EdgeResolution.EXACT, targets


def test_a_bound_call_reaches_only_its_own_objects_member(
    calls: dict[tuple[str, str], str],
) -> None:
    # Same-key members of other objects share the `@line` group (`dup.go`,
    # `dup.go@2`) or look like a prototype twin (`make`'s literal `run`):
    # neither is what the binding holds.
    assert _targets(calls, "shadow.viaModule") == {
        "shadow.run": cs.EdgeResolution.EXACT
    }
    assert _targets(calls, "dup.useA") == {"dup.go": cs.EdgeResolution.EXACT}
    use_b = _targets(calls, "dup.useB")
    assert len(use_b) == 1, use_b
    ((callee, resolution),) = use_b.items()
    assert callee.startswith("dup.go@"), use_b
    assert resolution == cs.EdgeResolution.EXACT, use_b


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


def test_sibling_blocks_binding_one_name_pick_no_member(
    calls: dict[tuple[str, str], str],
) -> None:
    # Both literals bind `both.api`; which one a call means is not known, so
    # neither block's `run` is an exact target.
    targets = _targets(calls, "blocks.both")
    assert cs.EdgeResolution.EXACT not in targets.values(), targets


def test_an_import_reaches_the_binding_the_module_exports(
    calls: dict[tuple[str, str], str],
) -> None:
    # `export { publicApi as api }`: the importer's `api` is `publicApi`,
    # never the private `const api` (the first `run`, `exported.run`).
    targets = _targets(calls, "exporteduse.viaExport")
    exact = [
        callee for callee, res in targets.items() if res == cs.EdgeResolution.EXACT
    ]
    assert len(exact) == 1 and exact[0].startswith("exported.run@"), targets


def test_sibling_blocks_with_different_keys_pick_no_member(
    calls: dict[tuple[str, str], str],
) -> None:
    # The second block's `api` has no `run`; the first block's must not be
    # what its `api.run()` calls.
    targets = _targets(calls, "keys.differentKeys")
    assert cs.EdgeResolution.EXACT not in targets.values(), targets


@pytest.mark.parametrize("caller", ["fnshadow.viaDeclaration", "fnshadow.viaCallback"])
def test_a_local_function_or_callback_parameter_shadows_the_module_object(
    calls: dict[tuple[str, str], str], caller: str
) -> None:
    exact = {
        callee
        for (src, callee), res in calls.items()
        if src.startswith(caller)
        and callee == "fnshadow.run"
        and res == cs.EdgeResolution.EXACT
    }
    assert not exact, calls
    assert _targets(calls, "fnshadow.stillModule") == {
        "fnshadow.run": cs.EdgeResolution.EXACT
    }


def test_a_member_marked_under_a_new_binding_leaves_the_old_one() -> None:
    # CodeRabbit, PR #3118: an object renamed from `api` to `service` keeps
    # its member under the new binding only.
    registry = FunctionRegistryTrie()
    registry["m.run"] = NodeType.FUNCTION
    registry.mark_object_binding("m.run", "m.api", "run")
    registry.mark_object_binding("m.run", "m.service", "run")

    assert registry.object_binding_member("m.api", "run") is None
    assert registry.object_binding_member("m.service", "run") == "m.run"


def test_a_member_a_later_spread_may_replace_gets_no_exact_caller(
    calls: dict[tuple[str, str], str],
) -> None:
    # `...base` after `run` replaces it, so `api.run()` is not `api`'s own
    # `run` (`spread.run@2`; `spread.run` is `base`'s).
    targets = _targets(calls, "spread.use")
    assert targets.get("spread.run@2") != cs.EdgeResolution.EXACT, targets


def test_a_callback_declaration_shadows_the_module_object(
    calls: dict[tuple[str, str], str],
) -> None:
    exact = {
        src
        for (src, callee), res in calls.items()
        if src.startswith("cbdecl.use")
        and callee == "cbdecl.run"
        and res == cs.EdgeResolution.EXACT
    }
    assert not exact, calls
