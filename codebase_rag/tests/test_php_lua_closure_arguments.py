"""Issue #2925: a PHP or Lua closure passed as a call argument is referenced.

An anonymous function handed straight to a call (`array_map(function ($x)
{...}, $xs)`, `usort($xs, fn ...)`, a route closure, Lua's `table.sort(t,
function ... end)`) gets a node but no incoming edge, so dead-code reported
every one; the identical JS/TS code gets a REFERENCES edge from the caller.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

PHP = """\
<?php
namespace App;

function render(string $view): string { return $view; }

function double_all(array $xs): array {
    return array_map(function ($x) { return $x * 2; }, $xs);
}

function sort_desc(array $xs): array {
    usort($xs, fn ($a, $b) => $b <=> $a);
    return $xs;
}

function boot($router): void {
    $router->get('/', function () { return render('home'); });
}

function named_callback(array $xs): array {
    return array_map('strtoupper', $xs);
}
"""

LUA = """\
local M = {}

local function render(view)
  return view
end

local function view()
  return "page"
end

function M.sort_desc(t)
  table.sort(t, function(a, b) return a > b end)
  return t
end

function M.boot(router)
  router.get("/", function() return render("home") end)
end

local function by_length(a, b)
  return #a < #b
end

function M.sort_by_length(t)
  table.sort(t, by_length)
  return t
end

function M.sort_by_local(t)
  local cmp = function(a, b) return a < b end
  table.sort(t, cmp)
  return t
end

function M.read_before_local()
  render(view)
  local view = "later"
  return view
end

function M.shadow_local()
  local view = "home"
  return render(view)
end

function M.shadow_param(view)
  return render(view)
end

function M.shadow_loop(t)
  for _, view in ipairs(t) do render(view) end
end

function M.shadow_upvalue(t, view)
  table.sort(t, function(a, b) return render(view) < render(a) end)
end

function M.nearer_local_function(t, cmp)
  local function cmp(a, b) return a < b end
  table.sort(t, cmp)
end

function M.assigned_later(t)
  local cmp
  cmp = function(a, b) return a < b end
  table.sort(t, cmp)
end

function M.assigned_from_nil(t)
  local cmp = nil
  cmp = function(a, b) return a < b end
  table.sort(t, cmp)
end

function M.newer_function_local(t, cb)
  local cb = function(a, b) return a < b end
  table.sort(t, cb)
end

function M.shadow_repeat()
  repeat local view = "text" until render(view)
end

function M.shadow_reassigned_value()
M.function_then_value()
  local view = "home"
  view = "about"
  return render(view)
end

function M.function_then_value()
  local view = function() return "page" end
  view = "text"
  return render(view)
end

-- Module-level calls root the module functions here, keeping the
-- export-roots gap (#2578) out of the dead-code assertion.
M.sort_desc({3, 1, 2})
M.boot(require("router"))
M.sort_by_length({"ccc", "a"})
M.sort_by_local({2, 1})
M.read_before_local()
M.shadow_local()
M.shadow_param("x")
M.shadow_loop({"x"})
M.shadow_upvalue({1}, "x")
M.nearer_local_function({2, 1}, 1)
M.assigned_later({2, 1})
M.assigned_from_nil({2, 1})
M.newer_function_local({2, 1}, 1)
M.shadow_repeat()
M.shadow_reassigned_value()
M.function_then_value()

return M
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("cb") / "cb"
    _write(root, "src/routes.php", PHP)
    _write(root, "app.lua", LUA)
    return _index(root, MagicMock())


def _anonymous_refs(graph: RecordedGraph, caller: str) -> list[str]:
    prefix = f"{graph.project}."
    return sorted(
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == cs.RelationshipType.REFERENCES
        and src.removeprefix(prefix) == caller
        and ".anonymous_" in dst
    )


@pytest.mark.parametrize(
    "caller",
    [
        "src.routes.double_all",
        "src.routes.sort_desc",
        "src.routes.boot",
        "app.M.sort_desc",
        "app.M.boot",
    ],
    ids=["php-array-map", "php-arrow-fn", "php-route", "lua-table-sort", "lua-route"],
)
def test_a_closure_argument_is_referenced_by_its_caller(
    graph: RecordedGraph, caller: str
) -> None:
    refs = _anonymous_refs(graph, caller)
    assert len(refs) == 1
    assert refs[0].startswith(f"{caller}.anonymous_")


class _DeadCodeGraph:
    def __init__(self, graph: RecordedGraph) -> None:
        labels = {qn: props[cs.KEY_LABEL] for qn, props in graph.nodes.items()}
        self._nodes: list[ResultRow] = [
            {
                cs.KEY_LABEL: props[cs.KEY_LABEL],
                cs.KEY_QUALIFIED_NAME: qn,
                cs.KEY_NAME: props.get(cs.KEY_NAME),
                cs.KEY_PATH: props.get(cs.KEY_PATH),
                cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                cs.KEY_DECORATORS: props.get(cs.KEY_DECORATORS, []),
                cs.KEY_IS_EXPORTED: props.get(cs.KEY_IS_EXPORTED, False),
                cs.KEY_OVERRIDES_EXTERNAL: props.get(cs.KEY_OVERRIDES_EXTERNAL, False),
            }
            for qn, props in graph.nodes.items()
            if props[cs.KEY_LABEL]
            in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
        ]
        self._rels: list[ResultRow] = [
            {
                cs.KEY_FROM_LABEL: labels.get(src),
                cs.KEY_FROM_QN: src,
                cs.KEY_REL_TYPE: rel,
                cs.KEY_TO_LABEL: labels.get(dst),
                cs.KEY_TO_QN: dst,
            }
            for src, rel, dst, _props in graph.edges
        ]

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._nodes if query == cq.CYPHER_DEAD_CODE_NODES else self._rels


def test_dead_code_reports_no_closure_argument(graph: RecordedGraph) -> None:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = [
        str(row[cs.KEY_QUALIFIED_NAME]).removeprefix(f"{graph.project}.")
        for row in collect_dead_code(_DeadCodeGraph(graph), graph.project, config)
    ]
    assert not [qn for qn in reported if ".anonymous_" in qn]


def _targets(graph: RecordedGraph, caller: str) -> set[str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if src == f"{prefix}{caller}"
        and rel in (cs.RelationshipType.REFERENCES, cs.RelationshipType.CALLS)
    }


@pytest.mark.parametrize(
    ("caller", "target"),
    [
        ("app.M.sort_by_length", "app.by_length"),
        ("app.M.sort_by_local", "app.M.sort_by_local.cmp"),
        ("app.M.read_before_local", "app.view"),
        # The definition pass registers a `local function` under its module.
        ("app.M.nearer_local_function", "app.cmp"),
        ("app.M.assigned_later", "app.M.assigned_later.cmp"),
        ("app.M.assigned_from_nil", "app.M.assigned_from_nil.cmp"),
        ("app.M.newer_function_local", "app.M.newer_function_local.cb"),
    ],
    ids=[
        "local-function",
        "function-valued-local",
        "read-before-the-local",
        "local-function-nearer-than-a-parameter",
        "function-assigned-after-a-bare-local",
        "function-assigned-after-a-nil-local",
        "function-local-nearer-than-a-parameter",
    ],
)
def test_a_named_lua_function_argument_is_referenced(
    graph: RecordedGraph, caller: str, target: str
) -> None:
    assert target in _targets(graph, caller)


# Negative: what must not change.


@pytest.mark.parametrize(
    "caller",
    [
        "app.M.shadow_local",
        "app.M.shadow_param",
        "app.M.shadow_loop",
        "app.M.shadow_upvalue",
        "app.M.shadow_repeat",
        "app.M.shadow_reassigned_value",
    ],
    ids=[
        "local-value",
        "parameter",
        "loop-variable",
        "upvalue",
        "repeat-body-local-in-until",
        "value-reassigned-a-value",
    ],
)
def test_a_lua_value_shadowing_a_function_binds_nothing(
    graph: RecordedGraph, caller: str
) -> None:
    # `render(view)` passes the local/parameter `view`, not the module
    # function `view` it hides.
    assert "app.view" not in _targets(graph, caller)


def test_a_function_local_reassigned_a_value_binds_nothing(
    graph: RecordedGraph,
) -> None:
    # `view` holds "text" when it is passed: the last assignment before the
    # read decides, not the function it first held (CodeRabbit, PR #2974).
    targets = _targets(graph, "app.M.function_then_value")
    assert not [t for t in targets if t.endswith(".view")]


def test_a_php_string_callable_binds_nothing_first_party(graph: RecordedGraph) -> None:
    prefix = f"{graph.project}."
    targets = {
        dst
        for src, rel, dst, _props in graph.edges
        if src == f"{prefix}src.routes.named_callback"
        and rel in (cs.RelationshipType.REFERENCES, cs.RelationshipType.CALLS)
    }
    assert not {t for t in targets if t.startswith(prefix)}
