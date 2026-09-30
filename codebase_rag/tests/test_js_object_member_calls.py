# A JS/TS object literal's function value is not a name in scope (issue #2435).
#
# ky's test/retry.ts imports `{setTimeout as delay}` from
# 'node:timers/promises' and calls `await delay(100)`, while the same file
# passes dozens of options objects such as `{retry: {delay: () => 0}}`. Each
# property's function registered under its bare key (`retry.delay` has no
# segment for the object), so every `delay(...)` call reached all of them as
# `overload` (1,785 false edges in ky). A property's function is reached
# through its object (`options.retry.delay()`), never by the key alone: a bare
# call must not bind to it by name, in the same file, from an enclosing
# function, or from another file.
#
# The negative tests pin what stays: member calls, calls bound by an import or
# a `require` destructure to an object's member, real bindings such as a
# function declaration or `const f = () => ...`, the references a passed
# object of callbacks gives its values, and calls made inside those values.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)
from evals.cgr_graph import _StatefulIngestor

# The issue's minimal reproduction, verbatim.
_ISSUE_REPRO = (
    "import {setTimeout as delay} from 'node:timers/promises';\n"
    "const opts1 = {retry: {delay: () => 0}};\n"
    "const opts2 = {retry: {delay: () => 10}};\n"
    "export async function run() { await delay(5); }\n"
)

# ky's test/retry.ts shape: the options object and the imported `delay` call
# share one test callback, so the enclosing-scope walk found the object's
# `delay` before the import was ever consulted.
_KY_SHAPE = (
    "import {setTimeout as delay} from 'node:timers/promises';\n"
    "test('retries', async t => {\n"
    "  await ky('u', {retry: {delay: () => 0}});\n"
    "  await delay(5);\n"
    "});\n"
)

_Edges = dict[tuple[str, str, str], str | None]


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, source in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


def _index(root: Path, files: dict[str, str], mock_ingestor: MagicMock) -> str:
    _write(root, files)
    create_and_run_updater(root, mock_ingestor, skip_if_missing="javascript")
    return root.name


def _edges(mock_ingestor: MagicMock) -> _Edges:
    edges: _Edges = {}
    for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.REFERENCES):
        for c in get_relationships(mock_ingestor, rel):
            props = c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {})
            key = (str(rel), c.args[0][2], c.args[2][2])
            edges[key] = (props or {}).get(cs.KEY_RESOLUTION)
    return edges


def _callees(edges: _Edges, caller: str) -> dict[str, str | None]:
    return {
        callee: resolution
        for (rel, source, callee), resolution in edges.items()
        if rel == cs.RelationshipType.CALLS and source == caller
    }


def _function_props(mock_ingestor: MagicMock) -> dict[str, dict]:
    return {
        c.args[1][cs.KEY_QUALIFIED_NAME]: c.args[1]
        for c in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }


# ---------------------------------------------------------------------------
# A bare call never binds to an object literal's function value by its key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("a.test.ts", _ISSUE_REPRO),
        ("a.test.js", _ISSUE_REPRO),
        (
            "a.test.ts",
            _ISSUE_REPRO.replace("delay: () => 0", "delay: function () { return 0 }"),
        ),
        ("a.test.ts", _ISSUE_REPRO.replace("delay: () => 0", "delay() { return 0 }")),
    ],
    ids=["issue-ts", "issue-js", "function-expression", "method-shorthand"],
)
def test_imported_call_never_reaches_same_file_object_members(
    temp_repo: Path, mock_ingestor: MagicMock, path: str, source: str
) -> None:
    # `delay` is bound by the import, so the call targets node:timers, which
    # the graph does not hold: no edge at all.
    project = _index(temp_repo / "repro", {path: source}, mock_ingestor)
    module = f"{project}.{path.rsplit('.', 1)[0]}"
    assert _callees(_edges(mock_ingestor), f"{module}.run") == {}


def test_imported_call_never_reaches_object_member_in_same_callback(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(temp_repo / "kyshape", {"test/retry.ts": _KY_SHAPE}, mock_ingestor)
    member = f"{project}.test.retry.delay"
    callers = {
        source
        for (rel, source, callee) in _edges(mock_ingestor)
        if rel == cs.RelationshipType.CALLS and callee == member
    }
    assert callers == set()


def test_unbound_call_never_reaches_object_member_in_another_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "crossfile",
        {
            "src/run.ts": "export async function run() { await delay(5); }\n",
            "src/opts.ts": "export const opts = {retry: {delay: () => 0}};\n",
        },
        mock_ingestor,
    )
    assert _callees(_edges(mock_ingestor), f"{project}.src.run.run") == {}


def test_object_member_is_not_visible_in_enclosing_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `{delay: ...}` inside `outer` registers as `outer.delay`, which the
    # enclosing-scope walk offered to `sibling` as if `delay` were a local.
    project = _index(
        temp_repo / "enclosing",
        {
            "lib/a.js": (
                "function outer () {\n"
                "  const inner = {delay: () => 0}\n"
                "  function sibling () { return delay() }\n"
                "  return [inner, sibling]\n"
                "}\n"
                "module.exports = outer\n"
            ),
        },
        mock_ingestor,
    )
    assert f"{project}.lib.a.outer.delay" not in _callees(
        _edges(mock_ingestor), f"{project}.lib.a.outer.sibling"
    )


@pytest.mark.parametrize(
    "member",
    ["{delay: () => 0}", "{delay () { return 0 }}"],
    ids=["arrow", "method-shorthand"],
)
def test_bare_call_to_real_function_skips_same_named_object_member(
    temp_repo: Path, mock_ingestor: MagicMock, member: str
) -> None:
    # The object's `delay` takes the `@line` twin of the real function's qn,
    # and the call fanned out onto both as `overload`.
    project = _index(
        temp_repo / "mixed",
        {
            "lib/a.js": (
                "function delay () { return 1 }\n"
                f"const o = {member}\n"
                "function run () { return delay() }\n"
                "module.exports = { o, run }\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.lib.a"
    assert _callees(_edges(mock_ingestor), f"{module}.run") == {
        f"{module}.delay": cs.EdgeResolution.EXACT
    }


def test_wrapper_member_does_not_call_itself(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `{parse: (s) => parse(s)}` forwards to the module's `parse`; the call
    # inside it bound to the arrow itself as well, a false recursion.
    project = _index(
        temp_repo / "wrapper",
        {
            "lib/a.js": (
                "function parse (s) { return s }\n"
                "module.exports = {parse: (s) => parse(s)}\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.lib.a"
    assert _callees(_edges(mock_ingestor), f"{module}.parse@2") == {
        f"{module}.parse": cs.EdgeResolution.EXACT
    }


def test_object_members_are_marked_and_bindings_are_not(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The mark is what an incremental run reads back for unchanged files.
    project = _index(
        temp_repo / "marks",
        {
            "lib/a.js": (
                "const o = {\n"
                "  arrow: () => 0,\n"
                "  fn: function () { return 1 },\n"
                "  shorthand () { return 2 },\n"
                "}\n"
                "function decl () { return 3 }\n"
                "const bound = () => 4\n"
                "exports.exported = function () { return 5 }\n"
                "module.exports.o = o\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.lib.a"
    marked = {
        qn
        for qn, props in _function_props(mock_ingestor).items()
        if props.get(cs.KEY_IS_OBJECT_MEMBER)
    }
    assert marked == {f"{module}.arrow", f"{module}.fn", f"{module}.shorthand"}


def _stateful_index(root: Path, store: _StatefulIngestor, force: bool) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(ingestor=store, repo_path=root, parsers=parsers, queries=queries).run(
        force=force
    )


def _stateful_calls_from(store: _StatefulIngestor, caller_suffix: str) -> set[str]:
    return {
        str(target)
        for _, source, rel, _, target in store.edges
        if rel == cs.RelationshipType.CALLS.value
        and str(source).endswith(caller_suffix)
    }


def test_incremental_sync_of_the_caller_adds_no_edge(temp_repo: Path) -> None:
    # Re-parsing only src/run.ts leaves src/opts.ts to rehydration from the
    # graph, so the object member's mark has to survive the round trip.
    parsers, _ = load_parsers()
    if "typescript" not in parsers:
        pytest.skip("typescript parser not available")
    run_source = "export async function run() { await delay(5); }\n"
    _write(
        temp_repo,
        {
            "src/run.ts": run_source,
            "src/opts.ts": "export const opts = {retry: {delay: () => 0}};\n",
        },
    )
    store = _StatefulIngestor()
    _stateful_index(temp_repo, store, force=True)
    assert not _stateful_calls_from(store, ".src.run.run")

    (temp_repo / "src" / "run.ts").write_text(
        run_source + "// touched\n", encoding="utf-8"
    )
    _stateful_index(temp_repo, store, force=False)
    assert not _stateful_calls_from(store, ".src.run.run")


# ---------------------------------------------------------------------------
# Negative tests: what must stay exactly as it was
# ---------------------------------------------------------------------------


def test_this_call_still_reaches_sibling_object_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A member call reaches the object's function through the object.
    project = _index(
        temp_repo / "thiscall",
        {
            "lib/a.js": (
                "const o = {\n"
                "  delay () { return 1 },\n"
                "  run () { return this.delay() },\n"
                "}\n"
                "module.exports = o\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.lib.a"
    assert f"{module}.delay" in _callees(_edges(mock_ingestor), f"{module}.run")


@pytest.mark.parametrize(
    ("caller_path", "binding"),
    [
        ("lib/a.js", "const { delay, wait } = require('./opts')"),
        ("lib/a.mjs", "import { delay, wait } from './opts.js'"),
    ],
    ids=["require-destructure", "esm-named-import"],
)
def test_call_bound_to_an_imported_object_member_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock, caller_path: str, binding: str
) -> None:
    # CommonJS exports an object literal; the importer's binding names its
    # member, so the call does reach it.
    project = _index(
        temp_repo / "imported",
        {
            "lib/opts.js": (
                "module.exports = {\n"
                "  delay: () => 0,\n"
                "  wait: function () { return 1 },\n"
                "}\n"
            ),
            caller_path: (
                f"{binding}\nfunction caller () {{ return delay() + wait() }}\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.{caller_path.rsplit('.', 1)[0].replace('/', '.')}"
    assert _callees(_edges(mock_ingestor), f"{module}.caller") == {
        f"{project}.lib.opts.delay": cs.EdgeResolution.EXACT,
        f"{project}.lib.opts.wait": cs.EdgeResolution.EXACT,
    }


def test_real_bindings_still_resolve_by_bare_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A declaration and a declarator arrow are names in scope; a CommonJS
    # `exports.x` member keeps the cross-file name fallback it had.
    project = _index(
        temp_repo / "bindings",
        {
            "lib/a.js": (
                "function decl () { return 1 }\n"
                "const bound = () => 2\n"
                "exports.later = function () { return 3 }\n"
                "function run () { return decl() + bound() }\n"
            ),
            "lib/b.js": "function other () { return bound() + later() }\n",
        },
        mock_ingestor,
    )
    edges = _edges(mock_ingestor)
    a = f"{project}.lib.a"
    assert _callees(edges, f"{a}.run") == {
        f"{a}.decl": cs.EdgeResolution.EXACT,
        f"{a}.bound": cs.EdgeResolution.EXACT,
    }
    assert _callees(edges, f"{project}.lib.b.other") == {
        f"{a}.bound": cs.EdgeResolution.HEURISTIC,
        f"{a}.later": cs.EdgeResolution.HEURISTIC,
    }


def test_passed_object_still_references_its_function_values(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The options object handed to a call is what keeps its `delay` callback
    # alive for dead-code, not the bare `delay(...)` next to it.
    project = _index(temp_repo / "passed", {"test/retry.ts": _KY_SHAPE}, mock_ingestor)
    module = f"{project}.test.retry"
    assert (
        str(cs.RelationshipType.REFERENCES),
        module,
        f"{module}.delay",
    ) in _edges(mock_ingestor)


def test_calls_inside_an_object_member_keep_their_caller(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = _index(
        temp_repo / "inside",
        {
            "lib/a.js": (
                "function helper () { return 1 }\n"
                "const o = {tick: () => helper()}\n"
                "module.exports = o\n"
            ),
        },
        mock_ingestor,
    )
    module = f"{project}.lib.a"
    assert _callees(_edges(mock_ingestor), f"{module}.tick") == {
        f"{module}.helper": cs.EdgeResolution.EXACT
    }


def test_named_function_expression_value_still_calls_itself(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `{walk: function walk (n) {...}}` binds `walk` inside its own body, so
    # its recursion is a real call; its name is left to the function
    # expression rules, not to this object-member mark.
    project = _index(
        temp_repo / "recursion",
        {
            "lib/a.js": (
                "const o = {walk: function walk (n) { return walk(n - 1) }}\n"
                "module.exports = o\n"
            ),
        },
        mock_ingestor,
    )
    walk = f"{project}.lib.a.walk"
    assert _callees(_edges(mock_ingestor), walk) == {walk: cs.EdgeResolution.EXACT}


def test_import_bound_call_keeps_the_whole_group(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `require('./x').parse` IS the exported object's wrapper, so an import of
    # `parse` keeps both same-named definitions as before; only a bare name
    # the module does not import drops the object's member.
    project = _index(
        temp_repo / "importgroup",
        {
            "lib/x.js": (
                "function parse (s) { return s }\n"
                "module.exports = {parse: (s) => parse(s)}\n"
            ),
            "lib/a.js": (
                "const { parse } = require('./x')\n"
                "function caller () { return parse('1') }\n"
            ),
        },
        mock_ingestor,
    )
    x = f"{project}.lib.x"
    assert set(_callees(_edges(mock_ingestor), f"{project}.lib.a.caller")) == {
        f"{x}.parse",
        f"{x}.parse@2",
    }


def _exported_functions(root: Path, tmp_path: Path) -> dict[str, bool]:
    out = tmp_path / "export"
    out.mkdir()
    ingestor = ProtobufFileIngestor(str(out), split_index=False)
    parsers, queries = load_parsers()
    GraphUpdater(ingestor, root, parsers, queries).run()
    index = pb.GraphCodeIndex()
    index.ParseFromString((out / cs.PROTOBUF_INDEX_FILE).read_bytes())
    return {
        node.function.qualified_name: node.function.is_object_member
        for node in index.nodes
        if node.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) == "function"
    }


def test_the_mark_survives_a_protobuf_export(temp_repo: Path, tmp_path: Path) -> None:
    # The export is the graph's portable form; a reader rebuilding the
    # registry from it needs the mark to tell a key's value from a binding.
    _write(temp_repo, {"a.ts": _ISSUE_REPRO})
    exported = _exported_functions(temp_repo, tmp_path)

    members = {qn for qn, marked in exported.items() if qn.endswith(".delay")}
    assert members, exported
    assert all(exported[qn] for qn in members), exported


def test_a_real_binding_exports_unmarked(temp_repo: Path, tmp_path: Path) -> None:
    _write(temp_repo, {"a.ts": _ISSUE_REPRO})
    exported = _exported_functions(temp_repo, tmp_path)

    runs = [qn for qn in exported if qn.endswith(".run")]
    assert runs, exported
    assert not any(exported[qn] for qn in runs), exported
