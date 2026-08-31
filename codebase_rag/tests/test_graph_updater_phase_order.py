"""`GraphUpdater.run` resolution phases must keep their documented order.

`run` ends in a long resolution pipeline whose steps depend on each other:
a pass reads state that an earlier pass registers. Every one of those
dependencies is currently recorded ONLY as a prose comment ("After
rehydration: ...", "Last containment step: ..."). Prose does not fail a
build, and reordering two of these steps corrupts the graph SILENTLY --
no exception, just wrong nodes.

That is not hypothetical. Issue #1552: `resolve_deferred_cpp_methods` ran
BEFORE `_rehydrate_registry_from_graph`, so an incremental re-parse of a
`.cpp` whose class lives in an unchanged header could not see the class,
fell back to a module-anchored qn, and left a phantom second node for one
method. Commit `1ba25c7e` fixed it by moving the call after rehydration.

The order is also easy to revert by ACCIDENT, and the accident survives
every cheap review check. A merge resolution that keeps every call -- an
identical call multiset, nothing deleted, a clean `git diff --stat` --
can still restore the pre-fix sequence, because order is a property of the
SEQUENCE and membership queries cannot see it.

The test is STRUCTURAL, over the AST of `run`, for the same reasons the
handler-lock guard is (see `test_mcp_read_handler_lock.py`):

- The behavioural test for #1552 needs a C++ parser and SKIPS without one,
  so on a base install nothing guards the order at all.
- A behavioural test per constraint would need a fixture per language that
  happens to exercise the path; most of these constraints have none.
- Most importantly, this guards the RULE. A behavioural test catches the
  one reordering whose fixture it owns. This catches any reordering of any
  pinned pair, including in code paths no fixture reaches.

Each pair below is a dependency stated in `graph_updater.py`'s own
comments, quoted in the `reason`. This deliberately pins PAIRS rather than
freezing the whole sequence: inserting a new phase, or reordering two steps
with no dependency between them, is legitimate and must stay easy. Only the
documented dependencies are load-bearing, so only those are enforced -- a
guard that failed on every legitimate edit would be deleted rather than
obeyed.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "graph_updater.py"

# (earlier, later, reason). `earlier` must be called before `later` in
# `GraphUpdater.run`. Every reason is quoted from the comment that states
# the dependency at the call site.
_ORDER: tuple[tuple[str, str, str], ...] = (
    (
        "_rehydrate_registry_from_graph",
        "resolve_deferred_cpp_methods",
        "an out-of-class method's class is only known once the registry is "
        "read back from the graph; resolving earlier binds it to a "
        "module-anchored fallback qn (issue #1552)",
    ),
    (
        "resolve_deferred_cpp_methods",
        "_resolve_hybrid_macro_calls",
        "an out-of-class method's span is recorded only once its class "
        "binding resolves, and a macro use inside such a method must "
        "attribute to it, not the Module",
    ),
    (
        "_rehydrate_registry_from_graph",
        "_resolve_hybrid_expansion_calls",
        "an expansion call's callee join needs spans for unchanged files too",
    ),
    (
        "_rehydrate_registry_from_graph",
        "resolve_deferred_forward_declarations",
        'the "does a real definition exist?" check must see definitions in '
        "files an incremental run did not re-parse, or a forward declaration "
        "whose definition lives in an unchanged file is kept as a phantom",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_cpp_artifacts",
        "a kept forward-declared TYPE also proves the name is a class, not a macro",
    ),
    (
        "resolve_deferred_cpp_artifacts",
        "resolve_deferred_cpp_prototypes",
        "a recovery-registered definition also counts when dropping a "
        "prototype that duplicates a bodied definition",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_cpp_inherits",
        "a base whose only representation is a kept forward declaration "
        "still resolves to a real node",
    ),
    (
        "finalise_rust_mod_scope_uses",
        "resolve_deferred_inherits",
        "inline-mod import maps commit before the deferred inheritance pass, "
        "which re-resolves module-anchored trait guesses through them",
    ),
    (
        "resolve_deferred_cpp_methods",
        "resolve_deferred_parent_links",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "resolve_deferred_go_methods",
        "resolve_deferred_parent_links",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_parent_links",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "_process_function_calls",
        "flush_deferred_import_edges",
        "a C# namespace import lands on the modules the file actually "
        "resolved entities from, and those resolutions are recorded during "
        "call processing (issue #1347)",
    ),
)


def _run_function() -> ast.FunctionDef:
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GraphUpdater":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run":
                    return item
    raise AssertionError("GraphUpdater.run not found in graph_updater.py")


def _call_lines() -> dict[str, list[int]]:
    """Every attribute call in `run`, mapped to the lines it is called on.

    Keyed by attribute name only: these phases are reached through several
    receivers (`self`, `self.factory.definition_processor`,
    `self.factory.import_processor`) and the receiver is not what the order
    depends on.
    """
    lines: dict[str, list[int]] = {}
    for node in ast.walk(_run_function()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            lines.setdefault(node.func.attr, []).append(node.func.lineno)
    return {name: sorted(found) for name, found in lines.items()}


def test_every_pinned_phase_is_actually_called() -> None:
    """A renamed or deleted phase must fail here, not silently unpin itself.

    Without this, the ordering test below would pass vacuously for any pair
    whose calls no longer exist -- the guard would go green by not looking,
    which is the failure mode `test_mcp_read_handler_lock.py` documents.
    """
    called = _call_lines()
    pinned = {name for earlier, later, _ in _ORDER for name in (earlier, later)}
    missing = sorted(name for name in pinned if name not in called)
    assert not missing, (
        f"{missing} are pinned by this test but no longer called in "
        "GraphUpdater.run. If a phase was renamed or removed, update _ORDER "
        "-- do not delete the constraint without checking the dependency it "
        "records is genuinely gone."
    )


def test_resolution_phases_keep_their_documented_order() -> None:
    """Each documented dependency holds in the source order of `run`."""
    called = _call_lines()
    violations: list[str] = []
    for earlier, later, reason in _ORDER:
        # Compare the LAST call of `earlier` against the FIRST of `later`:
        # the dependency is that every `earlier` has completed, so a second
        # `earlier` after `later` breaks it just as a swap does.
        earlier_line = called[earlier][-1]
        later_line = called[later][0]
        if earlier_line > later_line:
            violations.append(
                f"{earlier} (line {earlier_line}) must run BEFORE {later} "
                f"(line {later_line}): {reason}"
            )
    assert not violations, "GraphUpdater.run phase order violated:\n" + "\n".join(
        violations
    )
