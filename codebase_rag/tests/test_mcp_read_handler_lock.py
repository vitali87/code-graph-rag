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

The rule is enforced DEFAULT-DENY: every async handler must hold the lock or
be named in `_NON_GRAPH_HANDLERS`. The first version of this file made the
claim above while checking only an explicit `_GRAPH_READERS` inventory, so a
reader missing from that set was never examined -- the suite went green by not
looking. Greptile demonstrated it by adding an unlocked reader that the tests
did not notice. The docstring was accurate about the intent and wrong about
the mechanism, which is the more dangerous way for a test to be wrong: it
reads as covering a rule it does not enforce.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "mcp" / "tools.py"

# Handlers that read the graph and must therefore serialise against the
# rebuild. Kept as a named inventory so the failure message can say which
# handler tore, but it is NOT what makes the guard total -- see
# `_NON_GRAPH_HANDLERS` and the default-deny test below.
_GRAPH_READERS = frozenset(
    {
        "flow_verdict",
        "explain_traceback",
        "rank_root_causes",
        "list_projects",
        "semantic_search",
        "query_code_graph",
        "get_code_snippet",
        # Duplicate detection spans several graph reads (fingerprints, then
        # skipped-symbol coverage) and a node id is only meaningful within one
        # generation, so both must serialise against the rebuild.
        "find_duplicate_code",
        "get_function_source",
        # The agent reads the graph through the RAW tool objects rather than
        # the handlers above, so its bypass is total: none of the wrappers
        # apply. It needs the lock for the whole run because its answer is
        # composed across several tool calls.
        "ask_agent",
    }
)

# Handlers that touch NO graph state and therefore need no lock. This list is
# the load-bearing one, because the guard is default-deny: every async handler
# in tools.py must either hold the lock or be named here.
#
# That inversion is the whole point. An earlier version checked only
# `_GRAPH_READERS`, so a graph reader added tomorrow and forgotten from that
# set was never examined at all -- the test passed by not looking, which is
# indistinguishable from passing because the handler was locked. Greptile
# found this by mutation: it added an unlocked reader and the suite stayed
# green.
#
# Inferring readers instead was the obvious alternative and it does not work
# here. `semantic_search`, `query_code_graph`, `get_code_snippet` and
# `ask_agent` reach the graph through captured `_tool` objects and never
# mention `self.ingestor`, so a "reaches self.ingestor" heuristic misses four
# handlers that genuinely need the lock. Enumerating the SAFE ones is
# checkable by reading each body once; enumerating the dangerous ones is not.
#
# All three below delegate to file-system or AST tools. Adding a handler here
# is a deliberate claim that it touches no graph state. The write tools left
# this list with issue #1525: they re-ingest what they wrote and read the
# graph for the structural delta, under the lock.
_NON_GRAPH_HANDLERS = frozenset(
    {
        "structural_search",
        "read_file",
        "list_directory",
    }
)

# `find_duplicate_code` and `get_function_source` were absent from the list
# above while #1443 was unmerged: the names appeared in tools.py without being
# async handlers, so naming them would have failed
# `test_every_named_handler_actually_exists` on a premise that had not landed.
# This branch IS #1443, so they are async handlers here and are now listed.
# The control is what said when to add them.

# Handlers that mutate and already hold the lock; included so the test fails
# if one ever loses it.
#
# `index_repository` and `update_repository` are the load-bearing pair: they
# DELETE AND REBUILD the graph, which is the only reason a reader needs the
# lock at all. An earlier version of this file listed only delete_project and
# wipe_database and still claimed to pin the invariant "from both directions"
# -- so the two handlers whose locks make every reader's lock meaningful were
# the two it did not guard.
_GRAPH_WRITERS = frozenset(
    {
        "index_repository",
        "update_repository",
        "delete_project",
        "wipe_database",
        # The write tools re-ingest under the lock (issue #1525).
        "surgical_replace_code",
        "write_file",
        "structural_replace",
    }
)


def _own_scope_nodes(node: ast.AST) -> list[ast.AST]:
    """Every node belonging to this function's OWN scope.

    `ast.walk` descends into nested functions and classes, so a handler whose
    inner helper takes the lock counts as locked while its own body reads the
    graph unprotected. Reproduced in review on #1475: a handler containing
    `async def inner()` that holds the lock, plus an unguarded outer read,
    was reported as locked.

    Nested scopes are pruned rather than inspected: a lock acquired inside a
    closure is held only while that closure runs, so it cannot protect the
    outer body's reads whatever it names.
    """
    own: list[ast.AST] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(node))
    while stack:
        current = stack.pop()
        if isinstance(
            current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda
        ):
            continue
        own.append(current)
        stack.extend(ast.iter_child_nodes(current))
    return own


def _locks_in_own_body(node: ast.AsyncFunctionDef) -> bool:
    """Whether this function's OWN body contains `async with self._ingestor_lock`."""
    for inner in _own_scope_nodes(node):
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


def _every_return_is_locked(node: ast.AsyncFunctionDef) -> bool:
    """Whether EVERY value-returning path of this function runs under the lock.

    This is the per-path property, and the reason the containment check
    `_locks_in_own_body` is not enough to clear a delegate. Containment asks
    *can this function lock*; a `_run_ingest` that grows

        if self._fast_path:
            return await asyncio.to_thread(sync_fn)
        async with self._ingestor_lock:
            return await asyncio.to_thread(sync_fn)

    still contains the `async with` and still reaches the ingestor unlocked on
    one branch. Measured: propagating containment to the delegate passed that
    mutation, which is #1443's finding one frame deeper.

    A `return` inside the `async with` is locked. A bare `return` or `return
    None` outside it is not a graph result and does not need the lock; anything
    else outside it is a value produced without the lock and fails the check.
    The handler delegating here is a pure `return await`, so the delegate's
    returned value IS the handler's graph result.
    """
    locked_returns: set[int] = set()
    for inner in _own_scope_nodes(node):
        if not isinstance(inner, ast.AsyncWith):
            continue
        if not any(
            isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "_ingestor_lock"
            and isinstance(item.context_expr.value, ast.Name)
            and item.context_expr.value.id == "self"
            for item in inner.items
        ):
            continue
        for descendant in ast.walk(inner):
            if isinstance(descendant, ast.Return):
                locked_returns.add(id(descendant))

    handler_returns: set[int] = set()
    for inner in _own_scope_nodes(node):
        if not isinstance(inner, ast.Try):
            continue
        for clause in inner.handlers:
            for descendant in ast.walk(clause):
                if isinstance(descendant, ast.Return):
                    handler_returns.add(id(descendant))

    saw_locked_return = False
    for inner in _own_scope_nodes(node):
        if not isinstance(inner, ast.Return):
            continue
        if id(inner) in locked_returns:
            saw_locked_return = True
            continue
        # A bare `return` / `return None` yields no graph data, so it is not an
        # unlocked read. Nor is a return from an `except` handler: control only
        # reaches it because the locked body raised, so nothing was ingested and
        # the value is an error message rather than graph state. `_run_ingest`
        # returns `cs.MCP_INDEX_ERROR.format(...)` there, which is why treating
        # every unlocked return as a violation rejected correct code. Any OTHER
        # unlocked return is a value produced without the lock.
        if id(inner) in handler_returns:
            continue
        if inner.value is not None and not (
            isinstance(inner.value, ast.Constant) and inner.value.value is None
        ):
            return False
    return saw_locked_return


def _sole_delegate(node: ast.AsyncFunctionDef) -> str | None:
    """The name of the same-class method this handler is a pure delegation to.

    Only `return await self.<helper>(...)` as the WHOLE body qualifies. The
    narrowness is the point, and it is what keeps #1475 rejected: that shape was
    a handler holding the lock in a nested `async def` while its own body did an
    unguarded read. A body that is nothing but the delegated call has no other
    statement left to leave unprotected, so the delegate's lock is the handler's
    lock. Anything else -- a call among other statements, a call on something
    other than `self` -- returns None and the handler is judged on its own body.
    """
    if len(node.body) != 1:
        return None
    statement = node.body[0]
    if not isinstance(statement, ast.Return) or not isinstance(
        statement.value, ast.Await
    ):
        return None
    call = statement.value.value
    if not isinstance(call, ast.Call):
        return None
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    ):
        return func.attr
    return None


def _holds_ingestor_lock(
    node: ast.AsyncFunctionDef, methods: dict[str, ast.AsyncFunctionDef]
) -> bool:
    """Whether every path through this handler that reaches the graph is locked.

    A handler locks in its own body, or it is a PURE delegation to a same-class
    async method that does. Delegation is followed exactly one frame, and the
    delegate is checked with the SAME per-path property, not a containment one:
    "the delegate mentions the lock somewhere" would answer *can this delegate
    lock*, when the question is *does every ingesting path lock*. A delegate that
    later grows a fast path around its `async with` would satisfy containment
    while leaving a path unlocked, which is issue #1443's finding one frame
    deeper.

    One frame, because the delegate must itself be a lock-holding body under the
    same rule; a chain of pure delegations that never locks is still unlocked and
    still fails.
    """
    if _locks_in_own_body(node):
        return True
    delegate = _sole_delegate(node)
    if delegate is None:
        return False
    target = methods.get(delegate)
    return target is not None and _every_return_is_locked(target)


def _handlers() -> dict[str, ast.AsyncFunctionDef]:
    """Every async METHOD defined directly on a class in tools.py.

    Scoped to class bodies rather than `ast.walk` over the module, because the
    default-deny check treats an unrecognised async function as a failure: a
    module-level async helper is not an MCP handler and would be a false
    positive that trains readers to add exemptions to silence the guard.

    Direct children only, so a nested closure inside a handler is not mistaken
    for a handler of its own.
    """
    tree = ast.parse(_TOOLS.read_text(encoding="utf-8"))
    found: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if isinstance(item, ast.AsyncFunctionDef):
                found[item.name] = item
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
        if name in handlers and not _holds_ingestor_lock(handlers[name], handlers)
    )

    assert not missing, (
        f"{len(missing)} graph-read handler(s) do not hold _ingestor_lock, so "
        "an index/update rebuild can tear their read (issue #1471):\n"
        + "\n".join(missing)
    )


def test_every_handler_is_either_locked_or_declared_graph_free() -> None:
    """Default-deny: the guard that actually makes this total.

    Every async handler must hold `_ingestor_lock` or be named in
    `_NON_GRAPH_HANDLERS`. A new graph reader added without the lock and
    without a declaration fails HERE, which is the property the module
    docstring claims and the `_GRAPH_READERS` check alone did not deliver.

    The distinction matters because the omission is silent. A reader missing
    from an explicit inventory is not reported as unguarded -- it is simply
    never examined, and the suite goes green for a handler that can return a
    torn graph read. Requiring an affirmative declaration turns forgetting
    into a failure instead of a pass.
    """
    handlers = _handlers()

    undeclared = sorted(
        name
        for name, node in handlers.items()
        if name not in _NON_GRAPH_HANDLERS and not _holds_ingestor_lock(node, handlers)
    )

    assert not undeclared, (
        f"{len(undeclared)} MCP handler(s) neither hold _ingestor_lock nor are "
        "declared graph-free in _NON_GRAPH_HANDLERS. If a handler reads or "
        "writes the graph, wrap its body in `async with self._ingestor_lock`; "
        "if it touches no graph state, add it to _NON_GRAPH_HANDLERS with a "
        "reason (issue #1471):\n" + "\n".join(undeclared)
    )


def test_the_graph_free_declarations_are_not_stale() -> None:
    """A control on the exemption list, which is the one place to hide a bug.

    `_NON_GRAPH_HANDLERS` suppresses the default-deny check, so a name that no
    longer exists means the exemption is dead -- and a stale exemption is how a
    renamed handler would slip back through unguarded.
    """
    handlers = _handlers()

    unknown = sorted(_NON_GRAPH_HANDLERS - handlers.keys())

    assert not unknown, (
        f"{len(unknown)} name(s) in _NON_GRAPH_HANDLERS are not handlers in "
        "tools.py; the exemption is stale and should be removed:\n" + "\n".join(unknown)
    )


def test_the_lock_detector_rejects_an_unrelated_context_manager() -> None:
    """A self-check on `_holds_ingestor_lock`, which every other test routes through.

    The detector is the single point of failure here: if it returned True for
    any `async with`, a handler holding an unrelated context manager -- an HTTP
    session, a timeout, a DIFFERENT lock -- would count as locked, and an
    actually-unlocked graph reader would ship with the suite green. Every
    assertion in this file would keep passing, because they all ask the
    detector rather than the source.

    Verified by mutation: weakening the attribute comparison to `True` and
    adding a reader that takes `self._unrelated_lock` leaves all six tests
    passing. Nothing else in the file notices.

    So the detector is exercised directly, on synthetic sources rather than on
    tools.py, because the point is what it does with input the real file does
    not currently contain.
    """
    locked = ast.parse(
        "async def h(self):\n    async with self._ingestor_lock:\n        return 1\n"
    ).body[0]
    wrong_lock = ast.parse(
        "async def h(self):\n    async with self._unrelated_lock:\n        return 1\n"
    ).body[0]
    not_self = ast.parse(
        "async def h(self):\n    async with other._ingestor_lock:\n        return 1\n"
    ).body[0]
    no_lock = ast.parse("async def h(self):\n    return 1\n").body[0]

    assert isinstance(locked, ast.AsyncFunctionDef)
    assert _holds_ingestor_lock(locked, {})

    assert isinstance(wrong_lock, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(wrong_lock, {})

    assert isinstance(not_self, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(not_self, {})

    assert isinstance(no_lock, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(no_lock, {})


def test_the_lock_detector_ignores_locks_in_nested_scopes() -> None:
    """A lock inside a closure or nested class does not protect the outer body.

    `ast.walk` descends into nested scopes, so before this was fixed a handler
    whose inner helper took the lock counted as locked while its own body read
    the graph unprotected -- an unlocked reader reported as safe. Found in
    review on #1475 and reproduced by execution.

    The distinction is semantic rather than stylistic: a lock acquired inside
    a closure is held only while that closure runs, so it cannot serialise the
    outer body's reads against a rebuild no matter what it names.

    The second axis of this detector's contract. The sibling test covers WHAT
    is locked; this covers WHERE, and the two are independent -- the earlier
    self-check pinned the attribute name and was blind to scope entirely.
    """
    nested_function = ast.parse(
        "async def h(self):\n"
        "    async def inner():\n"
        "        async with self._ingestor_lock:\n"
        "            return 1\n"
        "    return await self._graph_query_tool.function()\n"
    ).body[0]
    nested_class = ast.parse(
        "async def h(self):\n"
        "    class C:\n"
        "        async def m(self):\n"
        "            async with self._ingestor_lock:\n"
        "                return 1\n"
        "    return await self._graph_query_tool.function()\n"
    ).body[0]
    outer_and_nested = ast.parse(
        "async def h(self):\n"
        "    async with self._ingestor_lock:\n"
        "        async def inner():\n"
        "            return 1\n"
        "        return await inner()\n"
    ).body[0]

    assert isinstance(nested_function, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(nested_function, {})

    assert isinstance(nested_class, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(nested_class, {})

    # A genuine outer lock still counts even when it contains a nested scope,
    # so the pruning does not overshoot into false negatives.
    assert isinstance(outer_and_nested, ast.AsyncFunctionDef)
    assert _holds_ingestor_lock(outer_and_nested, {})


def test_the_lock_detector_follows_only_pure_delegation() -> None:
    """A handler whose whole body is `return await self.<helper>(...)`.

    #1480 refactored `index_repository` and `update_repository` into one
    `_run_ingest`, which holds the lock. Both handlers genuinely lock through
    that call, and an own-body-only detector reported both as unlocked: a false
    positive that would have blocked a correct refactor.

    Three properties, and each is the reason a weaker rule was rejected:

    - The delegate is checked with the SAME per-path property, not containment.
      A `_run_ingest` that grew a fast path around its `async with` would still
      "contain" the lock while leaving an ingesting path open.
    - Delegation must be the WHOLE body. #1475's shape -- a lock in a nested
      scope beside an unguarded outer read -- must keep failing, and it does
      because a body with a read left in it is not a pure delegation.
    - The call must be on `self`. Following an arbitrary call would resolve any
      same-named function in the file, which is not the method being invoked.
    """
    methods = {
        name: ast.parse(source).body[0]
        for name, source in {
            "_run_ingest": (
                "async def _run_ingest(self, fn):\n"
                "    async with self._ingestor_lock:\n"
                "        return await asyncio.to_thread(fn)\n"
            ),
            "_unlocked_helper": ("async def _unlocked_helper(self):\n    return 1\n"),
        }.items()
    }

    delegating = ast.parse(
        "async def h(self):\n    return await self._run_ingest(self._sync)\n"
    ).body[0]
    delegating_to_unlocked = ast.parse(
        "async def h(self):\n    return await self._unlocked_helper()\n"
    ).body[0]
    delegating_plus_read = ast.parse(
        "async def h(self):\n"
        "    await self._graph_query_tool.function()\n"
        "    return await self._run_ingest(self._sync)\n"
    ).body[0]
    not_on_self = ast.parse(
        "async def h(self):\n    return await other._run_ingest(self._sync)\n"
    ).body[0]

    assert isinstance(delegating, ast.AsyncFunctionDef)
    assert _holds_ingestor_lock(delegating, methods)

    # The delegate's lock is what makes the handler locked, so a delegate
    # without one leaves the handler unlocked however pure the delegation is.
    assert isinstance(delegating_to_unlocked, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(delegating_to_unlocked, methods)

    # #1475 stays rejected: an unguarded statement beside the delegated call
    # runs outside the delegate's lock, so this is not a pure delegation.
    assert isinstance(delegating_plus_read, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(delegating_plus_read, methods)

    # `other._run_ingest` is not this class's method, whatever it is named.
    assert isinstance(not_on_self, ast.AsyncFunctionDef)
    assert not _holds_ingestor_lock(not_on_self, methods)


def test_the_two_inventories_are_disjoint() -> None:
    """Nothing may be declared both a graph reader and graph-free.

    An overlap would be a contradiction that reads as safe: the name is
    exempted from default-deny while also claiming to need the lock.
    """
    overlap = sorted(_GRAPH_READERS & _NON_GRAPH_HANDLERS)
    assert not overlap, overlap

    both = sorted(_GRAPH_WRITERS & _NON_GRAPH_HANDLERS)
    assert not both, both


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

    unknown = sorted(_GRAPH_WRITERS - handlers.keys())
    assert not unknown, (
        f"{len(unknown)} name(s) in _GRAPH_WRITERS are not handlers; the "
        "guard is checking nothing for them:\n" + "\n".join(unknown)
    )

    missing = sorted(
        name
        for name in _GRAPH_WRITERS
        if not _holds_ingestor_lock(handlers[name], handlers)
    )

    assert not missing, (
        f"{len(missing)} rebuild/mutate handler(s) lost _ingestor_lock, which "
        "makes every reader's lock decorative:\n" + "\n".join(missing)
    )
