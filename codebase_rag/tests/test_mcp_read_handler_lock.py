"""Graph-read MCP handlers must hold `_ingestor_lock` (issue #1471).

`index` and `update` DELETE AND REBUILD the graph while holding that lock. A
read that does not take it can observe one generation for part of its work
and another for the rest -- returning a result that never existed as a
coherent graph state. `flow_verdict` states the rule in its own comment.

The test is STRUCTURAL, over the AST of every handler, rather than one
behavioural test per handler. Three reasons, and the third is the point:

- A behavioural test needs a live Memgraph, which this suite has not got.
- Racing an index against a read to observe a torn result is inherently
  flaky, and a flaky concurrency test is worse than none.
- Most importantly: a per-handler test guards the handlers that exist today.
  This guards the RULE, so the next graph reader added without the lock fails
  here rather than shipping. #1443 fixed two handlers with no test, and four
  more were still unlocked -- which is exactly what a per-instance fix leaves
  behind.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "mcp" / "tools.py"

# Handlers that read the graph and must therefore serialise against the
# rebuild. Named explicitly rather than inferred: a heuristic over "reaches
# self.ingestor" would silently stop covering a handler whose call shape
# changed, and the failure would look like a pass.
_GRAPH_READERS = frozenset(
    {
        "flow_verdict",
        "explain_traceback",
        "rank_root_causes",
        "list_projects",
        "semantic_search",
        "query_code_graph",
        "get_code_snippet",
    }
)

# `find_duplicate_code` and `get_function_source` are deliberately ABSENT.
# Issue #1471 states they "were brought into line in #1443", but that PR is
# still OPEN -- the names appear in tools.py without being async handlers, so
# listing them here would make the control below fail on a premise that has
# not landed. Add them when #1443 merges; the control is what will say so.

# Handlers that mutate and already hold the lock; included so the test fails
# if one ever loses it.
_GRAPH_WRITERS = frozenset({"delete_project", "wipe_database"})


def _holds_ingestor_lock(node: ast.AsyncFunctionDef) -> bool:
    """Whether the body contains `async with self._ingestor_lock`."""
    for inner in ast.walk(node):
        if not isinstance(inner, ast.AsyncWith):
            continue
        for item in inner.items:
            expr = item.context_expr
            if (
                isinstance(expr, ast.Attribute)
                and expr.attr == "_ingestor_lock"
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "self"
            ):
                return True
    return False


def _handlers() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    found: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            found[node.name] = node
    return found


def test_every_graph_reader_holds_the_ingestor_lock() -> None:
    """The rule, not the instances.

    An unlocked reader can mix graph generations mid-request, which produces
    a wrong answer rather than an error -- nothing downstream can tell.
    """
    handlers = _handlers()

    missing = sorted(
        name
        for name in _GRAPH_READERS
        if name in handlers and not _holds_ingestor_lock(handlers[name])
    )

    assert not missing, (
        f"{len(missing)} graph-read handler(s) do not hold _ingestor_lock, so "
        "an index/update rebuild can tear their read (issue #1471):\n"
        + "\n".join(missing)
    )


def test_every_named_handler_actually_exists() -> None:
    """A control: the list above must name real handlers.

    Without this, renaming a handler would silently empty the guard -- the
    test would pass because it checked nothing, which is indistinguishable
    from passing because everything is locked.
    """
    handlers = _handlers()

    unknown = sorted(_GRAPH_READERS - handlers.keys())

    assert not unknown, (
        f"{len(unknown)} name(s) in _GRAPH_READERS are not handlers in "
        "tools.py; the guard is checking nothing for them:\n" + "\n".join(unknown)
    )


def test_the_mutating_handlers_still_hold_the_lock() -> None:
    """The other side of the invariant.

    Readers only need the lock because writers take it while rebuilding. If a
    writer lost it, every reader's lock would become decorative -- so the
    property is pinned from both directions.
    """
    handlers = _handlers()

    missing = sorted(
        name
        for name in _GRAPH_WRITERS
        if name in handlers and not _holds_ingestor_lock(handlers[name])
    )

    assert not missing, missing
