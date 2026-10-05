"""Lua `t["key"] = function` bodies keep their calls, and a module's returned
value roots dead-code (issue #2578).

Two gaps made `cgr dead-code` report most of a Lua library's public API:

1. A function assigned to a bracket-indexed target (`tests["clamp"] =
   function() ... end`) was registered as `anonymous_<row>_<col>` and the
   call pass skipped its body, so every call in it was lost. The dotted form
   (`tests.dotted = function`) and `local function named()` were linked, and
   are the controls here.
2. The value a module returns (`return M`, `return named`, `return { ... }`)
   is its public API, but nothing in it was a reachability root, so a public
   function with no in-repo caller (or whose callers were themselves
   unrooted) was reported as dead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import dead_code_from_graph, default_dead_code_config
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _capture

PROJECT = "proj"

# The issue's minimal repro, plus the other ways a module fills its table
# (colon method, assigned function, bracket key) and two local functions:
# one the API calls (live through it) and one nothing reaches (dead).
MOD_LUA = """\
local M = {}
local function private_helper(x) return x end
local function unused_private() return 0 end
function M.clamp(x, lo, hi) return private_helper(math.min(math.max(x, lo), hi)) end
function M.unused_public(x) return x end
function M:describe() return self end
M.assigned = function() return 1 end
M["bracketed"] = function() return 2 end
M["x.y"] = function() return 3 end
return M
"""

USE_LUA = """\
local mod = require "mod"
local tests = {}
tests["clamp"] = function()
  assert(mod.clamp(8, 5, 10) == 8)
end
tests.dotted = function()
  return mod.clamp(1, 2, 3)
end
local function named()
  return mod.clamp(4, 5, 6)
end
tests["a"]["b"] = function() return mod.clamp(0, 0, 0) end
tests['quoted'] = function() return mod.clamp(1, 1, 1) end
for _, t in pairs(tests) do t() end
return named
"""

# Keys a dotted name cannot spell: the function keeps its generated name
# but its body's calls are no longer dropped.
KEYS_LUA = """\
local lume = require "mod"
local tests = {}
tests["lume.clamp"] = function()
  return lume.clamp(1, 2, 3)
end
tests["has space"] = function() return lume.clamp(0, 0, 0) end
tests["end"] = function() return lume.clamp(0, 0, 1) end
tests[1] = function() return lume.clamp(0, 1, 1) end
tests["x.y"] = function()
  pcall(function() lume.clamp(3, 3, 3) end)
end
tests["ok"] = 42
"""

RET_TABLE_LUA = """\
local function helper() return 1 end
local function local_fn() return helper() end
local function private() return 2 end
local function outer()
  local function local_fn() return 3 end
  return { inner = function() return 4 end }
end
return {
  name = function() return helper() end,
  alias = local_fn,
  nested = { deep = function() return 5 end },
  function() return helper() end,
}
"""

RET_FN_LUA = """\
local function helper() return 1 end
return function() return helper() end
"""

SCRIPT_LUA = """\
local function lonely() return 1 end
lonely_table = {}
"""

CFG_LUA = """\
local cfg = { a = 1, b = "x", ["c"] = true, nested = { d = 2 }, [k] = false }
cfg["e"] = 42
cfg.f = "s"
return cfg
"""

# The module table handed out through a chunk-level alias, both ways round:
# `return api` where `api = M`, and members added through `api` while `M`
# itself is returned (Greptile, PR #2617). `other` is aliased but never
# returned, so nothing reaches its member.
ALIAS_LUA = """\
local M = {}
function M.f() return 1 end
local api = M
local other = {}
local other_alias = other
function other_alias.h() return 3 end
return api
"""

ALIAS_BACK_LUA = """\
local M = {}
local api = M
function api.g() return 2 end
return M
"""

# A `local M` (or a parameter `M`) inside a function or block is not the
# chunk's `M`, so its members are not the module's (Greptile, PR #2617). A
# function that assigns the chunk's own `M` without redeclaring it, and a
# member defined after a `do` block that shadowed `M`, still are.
SHADOW_LUA = """\
local M = {}
function M.used() return 1 end
local function build()
  local M = {}
  function M.other() return 2 end
  M.assigned = function() return 3 end
  M["bracketed"] = function() return 4 end
  return M
end
local function extend(M)
  function M.added() return 5 end
end
local function install()
  function M.late() return 6 end
end
install()
do
  local M = {}
  function M.scoped() return 7 end
end
for _, M in ipairs({}) do
  function M.looped() return 8 end
end
function M.after() return 9 end
return M
"""

FILES = {
    "mod.lua": MOD_LUA,
    "use.lua": USE_LUA,
    "keys.lua": KEYS_LUA,
    "ret_table.lua": RET_TABLE_LUA,
    "ret_fn.lua": RET_FN_LUA,
    "script.lua": SCRIPT_LUA,
    "cfg.lua": CFG_LUA,
    "alias.lua": ALIAS_LUA,
    "alias_back.lua": ALIAS_BACK_LUA,
    "shadow.lua": SHADOW_LUA,
}


class _Graph:
    def __init__(self, root: Path) -> None:
        ingestor = _capture(root, PROJECT)
        self.functions: dict[str, dict[str, object]] = {
            str(uid).removeprefix(f"{PROJECT}."): dict(props)
            for (label, uid), props in ingestor.nodes.items()
            if label == cs.NodeLabel.FUNCTION.value
        }
        self.calls: set[tuple[str, str]] = {
            (
                str(src).removeprefix(f"{PROJECT}."),
                str(dst).removeprefix(f"{PROJECT}."),
            )
            for (_sl, src, rel, _tl, dst) in ingestor.rels
            if rel == cs.RelationshipType.CALLS.value
        }
        self.dead = {
            qn.removeprefix(f"{PROJECT}.")
            for qn in dead_code_from_graph(
                ingestor.nodes,
                list(ingestor.rels),
                f"{PROJECT}.",
                default_dead_code_config(include_tests=False, include_classes=False),
            )
        }

    def callees(self, caller: str) -> set[str]:
        return {dst for src, dst in self.calls if src == caller}

    def qn_at(self, module: str, line: int) -> str:
        """The function defined on 1-based `line` of `module`, whatever
        duplicate-name suffix its qn carries."""
        return next(
            qn
            for qn, props in self.functions.items()
            if qn.startswith(f"{module}.") and props[cs.KEY_START_LINE] == line
        )


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> _Graph:
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.LUA not in parsers:
        pytest.skip("lua parser not available")
    root = tmp_path_factory.mktemp("lua2578") / PROJECT
    root.mkdir()
    for name, body in FILES.items():
        (root / name).write_text(body, encoding="utf-8")
    return _Graph(root)


def _anonymous(source: str, line_prefix: str, module: str) -> str:
    """The generated qn of the function on the line starting `line_prefix`."""
    row, line = next(
        (i, text)
        for i, text in enumerate(source.splitlines())
        if text.startswith(line_prefix)
    )
    col = line.index("function")
    return f"{module}.{cs.PREFIX_ANONYMOUS}{row}_{col}"


class TestBracketKeyFunctions:
    def test_string_key_names_the_function_like_the_dotted_form(
        self, graph: _Graph
    ) -> None:
        # `tests["clamp"] = function` is `tests.clamp = function`: same qn,
        # same name the dotted assignment form gets.
        assert "use.tests.clamp" in graph.functions, sorted(graph.functions)
        assert graph.functions["use.tests.clamp"][cs.KEY_NAME] == "tests.clamp"
        assert graph.functions["use.tests.dotted"][cs.KEY_NAME] == "tests.dotted"

    def test_string_key_body_keeps_its_calls(self, graph: _Graph) -> None:
        assert graph.callees("use.tests.clamp") == {"mod.M.clamp"}

    def test_chained_and_single_quoted_keys(self, graph: _Graph) -> None:
        assert graph.callees("use.tests.a.b") == {"mod.M.clamp"}
        assert graph.callees("use.tests.quoted") == {"mod.M.clamp"}

    def test_bracket_key_on_module_table_is_its_member(self, graph: _Graph) -> None:
        assert graph.functions["mod.M.bracketed"][cs.KEY_NAME] == "M.bracketed"

    @pytest.mark.parametrize(
        "line_prefix",
        [
            'tests["lume.clamp"]',
            'tests["has space"]',
            'tests["end"]',
            "tests[1]",
            'tests["x.y"]',
        ],
    )
    def test_key_a_name_cannot_spell_stays_anonymous_but_keeps_calls(
        self, graph: _Graph, line_prefix: str
    ) -> None:
        # A dotted qn would forge a nesting (`tests.lume.clamp`) or hold a
        # space or a keyword, so the function keeps its generated name, the
        # form every language gives a function nothing names. Its calls
        # were dropped with the name; now they are its own.
        qn = _anonymous(KEYS_LUA, line_prefix, "keys")
        assert qn in graph.functions, (qn, sorted(graph.functions))
        assert graph.callees(qn) == {"mod.M.clamp"}


class TestModuleExports:
    def test_exported_members_of_the_module_table_are_roots(
        self, graph: _Graph
    ) -> None:
        for qn in (
            "mod.M.clamp",
            "mod.M.unused_public",
            "mod.M:describe",
            "mod.M.assigned",
            "mod.M.bracketed",
        ):
            assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True, qn
            assert qn not in graph.dead, qn
        # Called only from exported `M.clamp`: live through it.
        assert "mod.private_helper" not in graph.dead

    def test_member_under_a_key_no_name_spells_is_a_root(self, graph: _Graph) -> None:
        # `M["x.y"] = function` is nameless, but still a member of `M`.
        qn = _anonymous(MOD_LUA, 'M["x.y"]', "mod")
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True
        assert qn not in graph.dead

    def test_returned_function_is_a_root(self, graph: _Graph) -> None:
        # `return named`: the module's value IS this function.
        assert graph.functions["use.named"][cs.KEY_IS_EXPORTED] is True
        assert "use.named" not in graph.dead

    def test_returned_table_constructor_roots_its_entries(self, graph: _Graph) -> None:
        # `return { name = function ... end, alias = local_fn, nested = {...} }`
        for qn in ("ret_table.name", "ret_table.local_fn", "ret_table.nested.deep"):
            assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True, qn
            assert qn not in graph.dead, qn
        assert "ret_table.helper" not in graph.dead

    def test_directly_returned_function_expression_is_a_root(
        self, graph: _Graph
    ) -> None:
        qn = _anonymous(RET_FN_LUA, "return function", "ret_fn")
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True
        # Rooting it would be moot if its body's calls were still dropped.
        assert graph.callees(qn) == {"ret_fn.helper"}
        assert "ret_fn.helper" not in graph.dead

    def test_positional_entry_of_returned_table_is_a_root(self, graph: _Graph) -> None:
        qn = _anonymous(RET_TABLE_LUA, "  function()", "ret_table")
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True
        assert graph.callees(qn) == {"ret_table.helper"}


class TestAliasesAndShadowing:
    def test_module_table_returned_through_a_local_alias_roots_its_members(
        self, graph: _Graph
    ) -> None:
        # `local api = M; return api`: `require` hands out `M` itself.
        qn = graph.qn_at("alias", 2)
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True, qn
        assert qn not in graph.dead, qn

    def test_member_added_through_an_alias_of_the_returned_table_is_a_root(
        self, graph: _Graph
    ) -> None:
        qn = graph.qn_at("alias_back", 3)
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True, qn
        assert qn not in graph.dead, qn

    @pytest.mark.parametrize(
        "line",
        [
            5,  # `local M` in `build`: `function M.other`
            6,  # ... `M.assigned = function`
            7,  # ... `M["bracketed"] = function`
            11,  # parameter `M` of `extend`
            19,  # `local M` in a `do` block
            22,  # loop variable `M`
        ],
    )
    def test_member_of_a_shadowing_local_table_is_not_exported(
        self, graph: _Graph, line: int
    ) -> None:
        qn = graph.qn_at("shadow", line)
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is not True, qn
        assert qn in graph.dead, qn


class TestNegative:
    """What must NOT change, and what must still be reported."""

    @pytest.mark.parametrize("line", [2, 14, 24])
    def test_members_of_the_chunk_table_stay_exported_beside_a_shadow(
        self, graph: _Graph, line: int
    ) -> None:
        # `M.used`; `M.late`, assigned from a function that never redeclares
        # `M`; `M.after`, defined once the `do` block's `local M` is gone.
        qn = graph.qn_at("shadow", line)
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is True, qn
        assert qn not in graph.dead, qn

    def test_member_of_an_aliased_table_nobody_returns_is_not_exported(
        self, graph: _Graph
    ) -> None:
        qn = graph.qn_at("alias", 6)
        assert graph.functions[qn][cs.KEY_IS_EXPORTED] is not True, qn
        assert qn in graph.dead, qn

    def test_no_forged_nesting_from_a_dotted_string_key(self, graph: _Graph) -> None:
        assert not any(qn.startswith("keys.tests.") for qn in graph.functions), sorted(
            graph.functions
        )

    def test_declaration_and_local_function_forms_are_unchanged(
        self, graph: _Graph
    ) -> None:
        assert graph.functions["mod.M.clamp"][cs.KEY_NAME] == "clamp"
        assert graph.functions["mod.M.assigned"][cs.KEY_NAME] == "M.assigned"
        assert graph.functions["use.named"][cs.KEY_NAME] == "named"
        assert graph.callees("use.tests.dotted") == {"mod.M.clamp"}
        assert graph.callees("use.named") == {"mod.M.clamp"}
        assert graph.callees("mod.M.clamp") == {"mod.private_helper"}

    def test_new_bodies_calls_are_not_credited_to_the_module(
        self, graph: _Graph
    ) -> None:
        # The module's own top-level calls are `require` and the `pairs`
        # loop; `mod.clamp` is only ever called from inside a function.
        assert "mod.M.clamp" not in graph.callees("use")
        assert "mod.M.clamp" not in graph.callees("keys")

    def test_callback_inside_a_bracket_function_still_bubbles_up(
        self, graph: _Graph
    ) -> None:
        # Only a function that IS the assigned value takes the bracket
        # target; a callback in its body stays a nameless callback whose
        # calls belong to the enclosing function, as everywhere else.
        inner = _anonymous(KEYS_LUA, "  pcall(function()", "keys")
        assert inner in graph.functions, (inner, sorted(graph.functions))
        assert graph.callees(inner) == set()

    def test_table_of_plain_values_emits_no_function_nodes(self, graph: _Graph) -> None:
        assert not any(qn.startswith("cfg.") for qn in graph.functions), sorted(
            graph.functions
        )

    def test_bracket_assignment_of_a_non_function_emits_nothing(
        self, graph: _Graph
    ) -> None:
        assert "keys.tests.ok" not in graph.functions

    def test_functions_outside_the_returned_value_are_still_dead(
        self, graph: _Graph
    ) -> None:
        for qn in (
            "mod.unused_private",
            "ret_table.private",
            "ret_table.outer",
            # A local shadowing an exported name is a different binding.
            graph.qn_at("ret_table", 5),
            # A `return` inside a function returns from that function, not
            # from the module.
            "ret_table.outer.inner",
            # A file that returns nothing exports nothing.
            "script.lonely",
        ):
            assert qn in graph.functions, (qn, sorted(graph.functions))
            assert graph.functions[qn][cs.KEY_IS_EXPORTED] is not True, qn
            assert qn in graph.dead, qn

    def test_unreturned_table_functions_are_not_exported(self, graph: _Graph) -> None:
        # `tests` is never returned, whatever its keys spell.
        for qn in (
            "use.tests.clamp",
            "use.tests.dotted",
            _anonymous(KEYS_LUA, 'tests["lume.clamp"]', "keys"),
            _anonymous(KEYS_LUA, "tests[1]", "keys"),
        ):
            props = graph.functions.get(qn, {})
            assert props.get(cs.KEY_IS_EXPORTED) is not True, qn
