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

The table is NOT a complete census of `run`'s ordering constraints, and a
green run here is not evidence that an unpinned reordering is safe. Three
constraints are left unpinned, each named with its reason in the note above
`_ORDER`. Treat this as a ratchet: when you find a documented dependency
that is not in the table, add it -- two of the entries below were added
that way after a review showed the note's reason for excluding them no
longer held.

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

# Real constraints in `run` that are deliberately NOT pinned. Recorded
# rather than silently omitted: a reader who finds these comments and no
# matching entry should know they were considered and why they were left
# out. Each would need the check weakened in a way that costs more than the
# pair is worth.
#
# * "LIBCLANG must run before Pass 2" / "HYBRID must run after Pass 2" --
#   both are `_run_cpp_frontend`, called at two sites under opposite mode
#   gates, so neither ordering holds of the NAME, which is all a
#   line-number comparison can see.
# * "The delombok state commits ONLY here, after every pass and the graph
#   flush" -- `flush_all` is called twice, so it is a poor anchor.
# * "Before the partial join on an incremental run: rebuild the type
#   locations for unchanged .cs files" (#1229) -- a genuine
#   `_rehydrate_csharp_type_locations` -> `_join_csharp_partials`
#   dependency, but the comment sits above the enclosing `if`, not above
#   the call. `_comment_above` reads the block directly above a call, and
#   widening it to look through an enclosing statement would risk
#   attaching a neighbouring phase's comment.
#
# (earlier, later, marker, reason). `earlier` must be called before `later`
# in `GraphUpdater.run`. `marker` is the phrase the call-site comment uses to
# name that dependency -- the source states several in prose ("After
# rehydration", "After artifact resolution") rather than by attribute name,
# so the phrase is recorded per pair instead of guessed from the identifier.
# A marker prefixed `earlier:` is checked above the EARLIER call instead.
# `reason` is the dependency itself, quoted from that comment.
_ORDER: tuple[tuple[str, str, str, str], ...] = (
    # -- around Pass 2 (`_process_files`) --
    (
        "_run_csharp_frontend",
        "_process_files",
        "earlier:must run before pass 2",
        "the Roslyn frontend produces a base-classification oracle that "
        "split_csharp_bases consults while ingesting each type's "
        "INHERITS/IMPLEMENTS edges during Pass 2",
    ),
    (
        "_run_go_frontend",
        "_process_files",
        "earlier:before pass 2",
        "Go facts are read at Pass 3 and the name-alias target index is "
        "filled during Pass 2, so they must load before it",
    ),
    (
        "_rehydrate_go_type_locations",
        "_join_go_implements",
        # Stated above the earlier call, in the block that follows it.
        "earlier:col-keyed indexes",
        "the Go IMPLEMENTS join resolves against locations Pass 2 filled "
        "only for re-parsed files, so the col-keyed indexes must be "
        "rehydrated first (issue #1240)",
    ),
    (
        "_process_files",
        "_join_csharp_partials",
        "after pass 2",
        "the Roslyn declaration locations resolve against the Class qns "
        "Pass 2 just registered",
    ),
    (
        "_process_files",
        "_join_go_implements",
        "after pass 2",
        "both ends resolve against the go_type_locations index Pass 2 just registered",
    ),
    (
        "_process_files",
        "_run_python_frontend",
        "after pass 2",
        "the Jedi facts join Pass 3 calls against the function_locations "
        "Pass 2 filled, and it needs the parsed-file list Pass 2 produced "
        "(issue #1183)",
    ),
    (
        "_process_files",
        "_run_java_frontend",
        "pass 2 registered",
        "the Java facts resolve against the method name-token locations "
        "Pass 2 registered (issue #1181)",
    ),
    # -- the deferred-resolution pipeline --
    (
        "_rehydrate_registry_from_graph",
        "resolve_deferred_cpp_methods",
        "after rehydration",
        "an out-of-class method's class is only known once the registry is "
        "read back from the graph; resolving earlier binds it to a "
        "module-anchored fallback qn (issue #1552)",
    ),
    (
        "resolve_deferred_cpp_methods",
        "_resolve_hybrid_macro_calls",
        "after resolve_deferred_cpp_methods",
        "an out-of-class method's span is recorded only once its class "
        "binding resolves, and a macro use inside such a method must "
        "attribute to it, not the Module",
    ),
    (
        "_rehydrate_registry_from_graph",
        "_resolve_hybrid_expansion_calls",
        "after rehydration",
        "an expansion call's callee join needs spans for unchanged files too",
    ),
    (
        "_rehydrate_registry_from_graph",
        "resolve_deferred_forward_declarations",
        "after rehydration",
        'the "does a real definition exist?" check must see definitions in '
        "files an incremental run did not re-parse, or a forward declaration "
        "whose definition lives in an unchanged file is kept as a phantom",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_cpp_artifacts",
        "forward declarations",
        "a kept forward-declared TYPE also proves the name is a class, not a macro",
    ),
    (
        "resolve_deferred_cpp_artifacts",
        "resolve_deferred_cpp_prototypes",
        "after artifact resolution",
        "a recovery-registered definition also counts when dropping a "
        "prototype that duplicates a bodied definition",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_cpp_inherits",
        "after forward declarations",
        "a base whose only representation is a kept forward declaration "
        "still resolves to a real node",
    ),
    (
        "finalise_rust_mod_scope_uses",
        "resolve_deferred_inherits",
        # Stated above the EARLIER call, and "inline-mod" is unique to it --
        # the later call's comment is about registry completeness generally
        # and would match a generic marker without being about this pair.
        "earlier:inline-mod",
        "inline-mod import maps commit before the deferred inheritance pass, "
        "which re-resolves module-anchored trait guesses through them",
    ),
    (
        "resolve_deferred_cpp_methods",
        "resolve_deferred_parent_links",
        "deferred c++ methods",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "resolve_deferred_go_methods",
        "resolve_deferred_parent_links",
        "go receivers",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "resolve_deferred_forward_declarations",
        "resolve_deferred_parent_links",
        "forward declarations",
        "every node-registering pass must finish before parent qns are "
        "verified against the registry",
    ),
    (
        "_process_function_calls",
        "_emit_csharp_query_calls",
        "after pass 3",
        "LINQ query-operator edges join after Pass 3 with the complete "
        "function-location registry: both ends must be registered nodes",
    ),
    (
        "_process_files",
        "_emit_pending_endpoints",
        "every module is parsed now",
        "router mount prefixes may be cross-module, so every module must be "
        "parsed before they can resolve (issue #877)",
    ),
    (
        "_process_files",
        "analyze",
        "definition pass already emitted",
        "the ast-grep findings post-pass links to the Modules the definition "
        "pass already emitted, so it cannot run before them without leaving "
        "dangling edges",
    ),
    (
        "_process_function_calls",
        "flush_deferred_import_edges",
        "after pass 3",
        "a C# namespace import lands on the modules the file actually "
        "resolved entities from, and those resolutions are recorded during "
        "call processing (issue #1347)",
    ),
)


# `run` may delegate its deferred-resolution stages to a helper so that the
# scoped re-ingest path (#1524) can reuse the same sequence. The ordering
# constraints hold wherever those stages LIVE, not only in `run`, so the
# guard scans `run` plus the helpers it delegates to. Without this, extracting
# the block would make every pinned pair vacuous -- the anti-vacuity test
# catches that as a loud failure, and this is the correct response to it.
#
# `_DELEGATES` is a name this file READS rather than calls, so a rename here
# yields an empty enumeration rather than an error -- and an empty
# enumeration is zero violations, which is indistinguishable from
# compliance. That is why `test_every_pinned_phase_is_actually_called` is
# load-bearing twice over: it catches a pinned phase leaving `run`, AND a
# delegate name going stale. Verified: renaming the helper makes all three
# tests fail loudly rather than pass vacuously.
_DELEGATES = ("_resolve_deferred_definitions",)


def _run_function() -> ast.FunctionDef:
    """`GraphUpdater.run`, the entry point whose phase order is pinned."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GraphUpdater":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "run":
                    return item
    raise AssertionError("GraphUpdater.run not found in graph_updater.py")


def _ordered_functions() -> list[ast.FunctionDef]:
    """`run` and any delegate helper, in source order.

    Source order matters: the pinned pairs are compared by line number, and a
    helper defined ABOVE `run` still executes where `run` calls it. Sorting by
    `lineno` keeps a constraint spanning both bodies meaningful.
    """
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"))
    found: list[ast.FunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GraphUpdater":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and (
                    item.name == "run" or item.name in _DELEGATES
                ):
                    found.append(item)
    if not found:
        raise AssertionError("GraphUpdater.run not found in graph_updater.py")
    return sorted(found, key=lambda f: f.lineno)


def _call_lines() -> dict[str, list[int]]:
    """Every attribute call in `run`, mapped to the lines it is called on.

    Keyed by attribute name only: these phases are reached through several
    receivers (`self`, `self.factory.definition_processor`,
    `self.factory.import_processor`) and the receiver is not what the order
    depends on.
    """
    lines: dict[str, list[int]] = {}
    for fn in _ordered_functions():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                lines.setdefault(node.func.attr, []).append(node.func.lineno)
    return {name: sorted(found) for name, found in lines.items()}


def _comment_above(line: int) -> str:
    """The contiguous `#` comment block immediately above `line`.

    The call may be an assignment wrapped over several lines
    (`x = (\\n    self.factory...`), so skip back over any continuation
    lines before looking for the comment block.
    """
    source = _SOURCE.read_text(encoding="utf-8").splitlines()
    index = line - 1  # `line` is 1-based
    while index > 0:
        stripped = source[index - 1].strip()
        if stripped.startswith("#") or not stripped:
            break
        if stripped.endswith("(") or stripped.endswith("="):
            index -= 1
            continue
        break
    block: list[str] = []
    while index > 0 and source[index - 1].strip().startswith("#"):
        block.insert(0, source[index - 1].strip().lstrip("#").strip())
        index -= 1
    return " ".join(block)


def test_every_pinned_dependency_is_still_explained_at_its_call_site() -> None:
    """The constraint and the prose that justifies it must not drift apart.

    `_ORDER` records WHY each pair is ordered, quoted from the comment at
    the call site. If that comment is deleted or rewritten to say something
    else, the table keeps asserting an order whose reason no longer exists
    anywhere in the source -- and an unexplained ordering constraint is how
    a real dependency decays into cargo that the next reader reorders or
    deletes because nothing says it matters.

    This checks only that the call site still carries SOME comment naming
    its dependency, not that the wording matches: pinning exact prose would
    fail on every copy-edit, and a guard that punishes legitimate edits gets
    deleted rather than obeyed.

    Which call site carries the justification is per pair. Most state it
    above the LATER call ("After rehydration: ..."), but a constraint phrased
    forwards states it above the EARLIER one ("Inline-mod import maps commit
    BEFORE the deferred inheritance pass below"). A marker prefixed
    `earlier:` is checked against the earlier call's comment instead.
    Getting this wrong makes a pair VACUOUS rather than loud: checking the
    wrong side matched a neighbouring comment about something else, so
    deleting the only prose that justified the Rust pair left this test
    green.

    KNOWN LIMIT, measured rather than assumed. `marker in comment` is a
    MEMBERSHIP test, so it sees what the comment lacks but not what it says
    IN ADDITION. A rewrite that keeps the marker word and reverses the claim
    ("After rehydration this used to matter, but the registry is now
    populated eagerly, so the ordering is no longer load-bearing") passes
    here. Whole-value equality would close that, as it does for a Cypher
    projection, but a comment is free prose that must stay editable, and a
    guard failing on every copy-edit gets deleted rather than obeyed.

    Moving a pinned call is fine as long as its comment travels with it:
    relocating `_resolve_hybrid_expansion_calls` together with its two
    comment lines passes, while moving the call alone fails here naming that
    call. That is deliberate, not a false alarm -- an ordering constraint
    stranded from its explanation is the state this test exists to prevent,
    and the remedy is to bring the comment along, not to relax the check.

    SECOND KNOWN LIMIT: `_call_lines` keys on the attribute NAME and ignores
    the receiver, because these phases are reached through several
    (`self`, `self.factory.definition_processor`,
    `self.factory.import_processor`, `self.finding_analyzer`) and the
    receiver is not what the order depends on. Eleven of the pinned names
    are not `GraphUpdater` methods at all, so this file never checks they
    exist on the object that owns them -- a rename on a processor class
    would leave these tests green while `run` calls something absent. That
    is caught by running the code, not here.
    The collision direction is fail-SAFE, verified rather than assumed: an
    unrelated receiver gaining a same-named call inside `run` (e.g. a second
    `analyze()`) makes both the ordering and drift tests FAIL loudly. A
    false positive someone must investigate, never a silent pass.

    That membership limit is REACHABLE, not theoretical: the rewrite above
    is an ordinary English sentence someone writes on believing a constraint
    was superseded, and it passes all three tests. What makes it tolerable
    is measured, not argued -- rewrite the comment that way AND then act on
    it by moving the call, and
    `test_resolution_phases_keep_their_documented_order` FAILS. The prose
    can be made to lie; the order cannot.

    That limit is acceptable only because it is not the load-bearing guard.
    `test_resolution_phases_keep_their_documented_order` reads no prose at
    all, so the CODE-corrupting defect -- the reordering itself -- is caught
    whatever the comments say (verified: reversing `1ba25c7e`'s hunk with
    every comment left pristine still fails it). What this test adds is that
    a constraint cannot silently lose its explanation; what it does not
    promise is that the surviving explanation is still TRUE.
    """
    called = _call_lines()
    unexplained: list[str] = []
    for earlier, later, marker, _reason in _ORDER:
        if marker.startswith("earlier:"):
            marker, site, line = marker[len("earlier:") :], earlier, called[earlier][-1]
        else:
            site, line = later, called[later][0]
        comment = _comment_above(line).lower()
        if marker not in comment:
            unexplained.append(
                f"the {earlier} -> {later} dependency is stated above "
                f"{site} (line {line}), but that comment no longer says "
                f"{marker!r}: {comment[:110]!r}"
            )
    assert not unexplained, (
        "A pinned ordering dependency lost the comment that justifies it:\n"
        + "\n".join(unexplained)
    )


def test_every_pinned_phase_is_actually_called() -> None:
    """A renamed or deleted phase must fail here, not silently unpin itself.

    Without this, the ordering test below would pass vacuously for any pair
    whose calls no longer exist -- the guard would go green by not looking,
    which is the failure mode `test_mcp_read_handler_lock.py` documents.
    """
    called = _call_lines()
    pinned = {name for earlier, later, _m, _r in _ORDER for name in (earlier, later)}
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
    for earlier, later, _marker, reason in _ORDER:
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
