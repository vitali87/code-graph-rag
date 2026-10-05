"""Calls to Lua colon-methods (`function T:m()`) are linked (issue #2481).

`function Account:deposit(v)` registers as `Account:deposit`, and none of the
four ways a program calls it produced a CALLS edge: `self:report()` inside a
method, `acc:deposit(5)` on a `T.new(...)` result, `Account.deposit(acc, 1)`
through the class table, and `util:method()` on a `require`d module. So
`cgr dead-code` flagged every colon-method (and whatever only they call) as
unreachable. The dot-defined functions of the same tables (`Account.new`,
`M.add`, `M.mul = function`) resolved all along and are the control here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import get_relationships, run_updater

OOP_LUA = """\
-- oop.lua
local Account = {}
Account.__index = Account

function Account.new(balance)
  local self = setmetatable({}, Account)
  self.balance = balance
  return self
end
function Account:deposit(v)
  self.balance = self.balance + v
  return self:report()
end

function Account:report()
  return self.balance
end

local acc = Account.new(10)
acc:deposit(5)
Account.deposit(acc, 1)
return Account
"""

UTIL_LUA = """\
local M = {}

local function private_helper(x)
  return x * 2
end

function M.add(a, b)
  return a + b
end

M.mul = function(a, b)
  return a * b
end

function M:method()
  return private_helper(1)
end

return M
"""

MAIN_LUA = """\
local util = require("lib.util")
local add = util.add

local function run()
  return util.add(1, 2) + util.mul(2, 3) + add(4, 5) + util:method()
end

return run()
"""

TELLER_LUA = """\
local Account = require("oop")

local function open()
  local a = Account.new(1)
  a:deposit(2)
  return Account.report(a)
end

return open
"""

SHAPES_LUA = """\
local Shape = {}
Shape.__index = Shape

function Shape.new()
  local self = setmetatable({}, Shape)
  self:init()
  return self
end

function Shape:init()
  self.n = 0
end

function Shape:area()
  return 0
end

function Shape:describe()
  local function label()
    return self:area()
  end
  return label()
end

function Shape.twice(self)
  return self
end

local Registry = {}
function Registry:new()
  local obj = setmetatable({}, self)
  obj:register()
  return obj
end
function Registry:register()
  return true
end
function Registry:start()
  return true
end

local s = Shape.new()
s.area(s)
local c = Shape:new()
local r = Registry:new()
r:start()
s:twice()
"""

AMBIGUOUS_LUA = """\
local Account = {}
Account.__index = Account
function Account:deposit(v) return v end
function Account:report() return 0 end

local Ledger = {}
Ledger.__index = Ledger
function Ledger:deposit(v) return v end
function Ledger:withdraw(v) return v end

local function report()
  return 1
end

function Account:settle()
  return self:report() + report()
end

local function process(thing)
  return thing:deposit(1)
end

local Util = {}
function Util.count() return 0 end
function Util.size() return 0 end

local a = setmetatable({}, Account)
a:withdraw(1)
local n = Util.count()
n:size()
return process
"""

PY_ACCOUNTS = """\
class Account:
    def deposit(self, v):
        return v


def run(acc, other):
    Account.deposit(acc, 1)
    return other.deposit(2)
"""


@pytest.fixture
def lua_oop_project(temp_repo: Path) -> Path:
    project = temp_repo / "luamod"
    (project / "lib").mkdir(parents=True)
    (project / "oop.lua").write_text(OOP_LUA, encoding="utf-8")
    (project / "lib" / "util.lua").write_text(UTIL_LUA, encoding="utf-8")
    (project / "main.lua").write_text(MAIN_LUA, encoding="utf-8")
    (project / "teller.lua").write_text(TELLER_LUA, encoding="utf-8")
    return project


@pytest.fixture
def lua_shapes_project(temp_repo: Path) -> Path:
    project = temp_repo / "shapes"
    project.mkdir()
    (project / "shapes.lua").write_text(SHAPES_LUA, encoding="utf-8")
    return project


@pytest.fixture
def lua_ambiguous_project(temp_repo: Path) -> Path:
    project = temp_repo / "ambig"
    project.mkdir()
    (project / "bank.lua").write_text(AMBIGUOUS_LUA, encoding="utf-8")
    (project / "accounts.py").write_text(PY_ACCOUNTS, encoding="utf-8")
    return project


def _calls(project: Path, mock_ingestor: MagicMock) -> set[tuple[str, str, int, str]]:
    """(caller, callee, line, resolution) per CALLS edge, qns project-relative."""
    run_updater(project, mock_ingestor, skip_if_missing="lua")
    prefix = f"{project.name}."
    found: set[tuple[str, str, int, str]] = set()
    for rel in get_relationships(mock_ingestor, cs.RelationshipType.CALLS):
        props = rel.args[3] if len(rel.args) > 3 else rel.kwargs.get("properties")
        if not props:
            continue
        found.add(
            (
                rel.args[0][2].removeprefix(prefix),
                rel.args[2][2].removeprefix(prefix),
                props[cs.KEY_LINE],
                str(props[cs.KEY_RESOLUTION]),
            )
        )
    return found


def _edges_from(
    calls: set[tuple[str, str, int, str]], caller: str, line: int
) -> set[tuple[str, str]]:
    return {(callee, res) for c, callee, ln, res in calls if c == caller and ln == line}


EXACT = cs.EdgeResolution.EXACT.value
HEURISTIC = cs.EdgeResolution.HEURISTIC.value


class TestIssueCallShapes:
    """The four call spellings the issue lists, each on its own line."""

    def test_self_colon_call_inside_method(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_oop_project, mock_ingestor)
        assert _edges_from(calls, "oop.Account:deposit", 12) == {
            ("oop.Account:report", EXACT)
        }

    def test_colon_call_on_constructor_result(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_oop_project, mock_ingestor)
        assert _edges_from(calls, "oop", 20) == {("oop.Account:deposit", EXACT)}

    def test_dot_call_through_class_table(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_oop_project, mock_ingestor)
        assert _edges_from(calls, "oop", 21) == {("oop.Account:deposit", EXACT)}

    def test_colon_call_on_required_module(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_oop_project, mock_ingestor)
        assert ("main.run", "lib.util.M:method", 5, EXACT) in calls


class TestReceiverTypes:
    """Each receiver-type source the issue names, beyond its own example."""

    def test_setmetatable_result_inside_constructor(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes.Shape.new", 6) == {
            ("shapes.Shape:init", EXACT)
        }

    def test_self_in_closure_nested_in_method(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `self` is an upvalue of `label`, bound by the enclosing `Shape:`
        # method.
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes.label", 20) == {("shapes.Shape:area", EXACT)}

    def test_setmetatable_with_self_as_metatable(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The PIL `setmetatable(obj, self)` idiom inside `Registry:new`.
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes.Registry:new", 32) == {
            ("shapes.Registry:register", EXACT)
        }

    def test_dot_call_on_instance_binds_colon_method(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes", 43) == {("shapes.Shape:area", EXACT)}

    def test_colon_call_binds_dot_defined_function(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Shape:new()` passes Shape as the first argument of the dot-defined
        # `Shape.new`; `s:twice()` passes `s` as `Shape.twice`'s `self`.
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes", 44) == {("shapes.Shape.new", EXACT)}
        assert _edges_from(calls, "shapes", 47) == {("shapes.Shape.twice", EXACT)}

    def test_colon_call_on_colon_constructor_result(
        self, lua_shapes_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Registry` has colon-methods only, so it has no node of its own in
        # the registry trie; its instance was never typed.
        calls = _calls(lua_shapes_project, mock_ingestor)
        assert _edges_from(calls, "shapes", 46) == {("shapes.Registry:start", EXACT)}

    def test_instance_of_required_class(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The required module returns its `Account` table under the same name.
        calls = _calls(lua_oop_project, mock_ingestor)
        assert _edges_from(calls, "teller.open", 5) == {("oop.Account:deposit", EXACT)}
        assert _edges_from(calls, "teller.open", 6) == {("oop.Account:report", EXACT)}


class TestNegative:
    """What must NOT bind, and neighbouring behaviour that stays as it was."""

    def test_unknown_receiver_does_not_bind_exact(
        self, lua_ambiguous_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `thing` could be an Account or a Ledger (or neither): no edge may
        # claim to know which.
        calls = _calls(lua_ambiguous_project, mock_ingestor)
        assert not {
            edge for edge in _edges_from(calls, "bank.process", 20) if edge[1] == EXACT
        }

    def test_typed_receiver_does_not_borrow_other_tables_method(
        self, lua_ambiguous_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `a` is an Account; only Ledger defines `withdraw`.
        calls = _calls(lua_ambiguous_project, mock_ingestor)
        assert ("bank.Ledger:withdraw", EXACT) not in _edges_from(calls, "bank", 28)

    def test_local_function_and_same_named_method_stay_apart(
        self, lua_ambiguous_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_ambiguous_project, mock_ingestor)
        edges = _edges_from(calls, "bank.Account:settle", 16)
        assert ("bank.Account:report", EXACT) in edges
        assert ("bank.report", EXACT) in edges
        assert len(edges) == 2

    def test_plain_function_table_result_is_not_typed(
        self, lua_ambiguous_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # `Util` has no colon-methods, so `Util.count()` is not a constructor
        # and `n` stays untyped: `n:size()` must not claim `Util.size` exactly.
        calls = _calls(lua_ambiguous_project, mock_ingestor)
        assert ("bank.Util.size", EXACT) not in _edges_from(calls, "bank", 30)

    def test_dot_functions_resolve_as_before(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_oop_project, mock_ingestor)
        assert {
            ("main.run", "lib.util.M.add", 5, EXACT),
            ("main.run", "lib.util.M.mul", 5, EXACT),
            ("main.run", "lib.util.M.add", 5, HEURISTIC),
            ("oop", "oop.Account.new", 19, EXACT),
        } <= calls

    def test_colon_method_body_is_still_its_own_caller(
        self, lua_oop_project: Path, mock_ingestor: MagicMock
    ) -> None:
        # The definition side was right all along: `M:method`'s body calls
        # are credited to it, which is why wiring its callers clears
        # `private_helper` from dead-code too.
        calls = _calls(lua_oop_project, mock_ingestor)
        assert ("lib.util.M:method", "lib.util.private_helper", 16, EXACT) in calls

    def test_python_calls_are_untouched(
        self, lua_ambiguous_project: Path, mock_ingestor: MagicMock
    ) -> None:
        calls = _calls(lua_ambiguous_project, mock_ingestor)
        assert _edges_from(calls, "accounts.run", 7) == {
            ("accounts.Account.deposit", EXACT)
        }
        assert not any(
            callee.startswith("bank.")
            for caller, callee, _, _ in calls
            if caller.startswith("accounts")
        )
