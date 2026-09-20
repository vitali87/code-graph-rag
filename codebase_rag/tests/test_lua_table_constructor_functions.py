"""A Lua function built into a table constructor is named by its field key.

`local s = { set_namespace = function(self, ns) ... end, set = function ... }`
declares two functions. They were both named `s`, after the enclosing
assignment's left side, and told apart only by an `@line` suffix: the field
key never reached the graph, so on plenary.nvim zero functions were named
`set_namespace`, `set_fallback`, `__index` or `__call` although the source
defines dozens that way (issue #1631). The declaration form
(`function Job.chain()`) was already right and is the control here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SAY_LUA = (
    "local s = {\n"
    "  set_namespace = function(self, ns) return ns end,\n"
    '  ["set"] = function(self, k) return k end,\n'
    "  sub = { f = function() return 1 end },\n"
    "}\n"
    "local Job = {}\n"
    "function Job.chain(a) return a end\n"
    "local t = {}\n"
    "t.__index = function() end\n"
    "M.__meta = { __call = function() end }\n"
    "return s\n"
)


def _functions(root: Path) -> dict[str, str]:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.LUA not in parsers:
        pytest.skip("lua parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return {
        str(uid): str(props.get(cs.KEY_NAME))
        for (label, uid), props in store.nodes.items()
        if label == cs.NodeLabel.FUNCTION.value
    }


def test_table_constructor_functions_take_their_field_key(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "say.lua").write_text(SAY_LUA, encoding="utf-8")

    functions = _functions(root)

    # Identifier key, bracketed string key, nested constructor, and a
    # constructor assigned to a dotted target: each function sits under the
    # table it is built into and carries its own key as its name.
    assert functions.get("proj.say.s.set_namespace") == "set_namespace", functions
    assert functions.get("proj.say.s.set") == "set", functions
    assert functions.get("proj.say.s.sub.f") == "f", functions
    assert functions.get("proj.say.M.__meta.__call") == "__call", functions
    # Nothing is named after the table any more, and nothing needs a
    # duplicate-name suffix to stay distinct.
    assert "proj.say.s" not in functions, functions
    assert not any("@" in qn for qn in functions), functions
    # The controls: the declaration and plain-assignment forms are unchanged.
    assert functions.get("proj.say.Job.chain") == "chain", functions
    assert functions.get("proj.say.t.__index") == "t.__index", functions


REVIEW_LUA = (
    "local function helper() end\n"
    "local function helper2() end\n"
    "local s = { [k] = function() end, { run = function() end } }\n"
    "local handlers = { { run = function() helper() end }, { run = function() end } }\n"
    "local result = register({ on_event = function() helper2() end })\n"
    "local function outer() return { f = function() helper() end } end\n"
    "local p = { f = (function() helper() end) }\n"
    "return { setup = function() helper() end }\n"
)


def _calls(root: Path) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return {
        (str(src), str(dst))
        for (_sl, src, rel, _tl, dst) in store.edges
        if rel == cs.RelationshipType.CALLS.value
    }


def test_keys_that_name_nothing_fall_back_and_named_fields_keep_their_calls(
    tmp_path: Path,
) -> None:
    """The three shapes the local review found (#1631 review).

    A computed `[k]` key and a positional entry are not fields with a name, so
    their functions are ANONYMOUS: falling back to the assignment form named
    both after the table (`s`, `s@1`) and conflated unrelated callbacks under
    one identity (#1750 review). A table nested in a POSITIONAL entry has no
    field on the outer list either, so `handlers.run` must not be invented.
    A field function whose table is not itself assigned (a returned table, a
    table passed as an ARGUMENT, a table returned from a function) is named
    by its keys alone and keeps the CALLS edges of its body, because the call
    pass names callers through the same helper the definition pass does. The
    argument case is pinned with an assignment around it: `result` receives
    `register`'s return value, not the table, so `result.on_event` would be a
    false identity (#1750 review).
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.lua").write_text(REVIEW_LUA, encoding="utf-8")

    functions = _functions(root)
    assert "proj.mod.s.k" not in functions, functions
    for owner in ("proj.mod.s", "proj.mod.handlers", "proj.mod.result"):
        assert owner not in functions, (owner, functions)
        assert not any(
            qn.startswith((f"{owner}.", f"{owner}{cs.DUP_QN_MARKER}"))
            for qn in functions
        ), (owner, functions)
    anonymous = [
        qn for qn, name in functions.items() if name.startswith(cs.PREFIX_ANONYMOUS)
    ]
    assert len(anonymous) == 4, (
        f"the two nameless `s` entries and both `handlers` entries are anonymous: {functions}"
    )
    for qn, name in (
        ("proj.mod.setup", "setup"),
        ("proj.mod.on_event", "on_event"),
        ("proj.mod.outer.f", "f"),
        # A parenthesised function value is still the field's value
        # (CodeRabbit, #1750).
        ("proj.mod.p.f", "f"),
    ):
        assert functions.get(qn) == name, (qn, functions)

    calls = _calls(root)
    for caller in ("proj.mod.setup", "proj.mod.outer.f", "proj.mod.p.f"):
        assert (caller, "proj.mod.helper") in calls, (caller, sorted(calls))
    assert ("proj.mod.on_event", "proj.mod.helper2") in calls, sorted(calls)
