"""Postcondition contract for edit operations (issue #1531).

The structural delta (issue #1525) tells an agent what an edit did; an
edit-algebra operation must prove it did the right thing. `verify` takes
the operation's expectation and the delta measured after its transaction
landed and answers pass or fail with reasons, plus the tests to run.

Expectations are per operation. A rename must leave the symbol and caller
counts alone, rename exactly the hierarchy it was asked to, leave no caller
dangling and rewrite no site it resolved by guesswork unless told to. A
signature change must map every call site or list it as unmapped. A move
must leave importers updated and introduce no import cycle. Every operation
must introduce no duplicate group and leave every file parsing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import NamedTuple

from .. import constants as cs
from ..graph_query import QueryFn
from ..structural_delta import StructuralDelta, TestReach, observe
from ..types_defs import ReingestReport

Reingest = Callable[[list[str]], ReingestReport]


class Expectation(NamedTuple):
    """What an operation promises about the delta it produces."""

    operation: str
    # (old, new) qualified-name pairs the delta must report as renamed.
    renames: tuple[tuple[str, str], ...] = ()
    # Qualified names allowed to appear (a move's new home, an extract).
    added: tuple[str, ...] = ()
    # Qualified names allowed to disappear (an inline, a move's old home).
    removed: tuple[str, ...] = ()
    symbol_count_unchanged: bool = True
    # Whether renames the delta reports but this expectation never asked for
    # fail the contract. Deliberately NOT folded into
    # `symbol_count_unchanged`: an inline waives symbol COUNTS, and waiving a
    # count is not consent to rename an unrelated symbol. Descendants carried
    # by a requested rename are not "unexpected" -- see `_carried_by_ancestor`.
    no_unexpected_rename: bool = True
    caller_count_unchanged: bool = True
    no_dangling: bool = True
    no_new_cycle: bool = True
    no_new_duplicate: bool = True
    # `path:line` of the call sites the operation deliberately left alone;
    # every other site of a changed signature must read as mapped.
    unmapped: tuple[str, ...] = ()
    # Whether sites resolved by guesswork may have been rewritten.
    heuristic_allowed: bool = False


class Verdict(NamedTuple):
    ok: bool
    failures: tuple[str, ...]
    affected_tests: tuple[TestReach, ...]
    delta: StructuralDelta | None


_AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)


def rename_expectation(
    pairs: Iterable[tuple[str, str]], heuristic_allowed: bool
) -> Expectation:
    return Expectation(
        operation=cs.CONTRACT_OP_RENAME,
        renames=tuple(sorted(set(pairs))),
        heuristic_allowed=heuristic_allowed,
    )


def change_signature_expectation(
    unmapped: Iterable[str], heuristic_allowed: bool = False
) -> Expectation:
    return Expectation(
        operation=cs.CONTRACT_OP_CHANGE_SIGNATURE,
        unmapped=tuple(sorted(set(unmapped))),
        # A parameter added with a default changes no caller count.
        caller_count_unchanged=True,
        heuristic_allowed=heuristic_allowed,
    )


def move_expectation(old_qn: str, new_qn: str) -> Expectation:
    return Expectation(
        operation=cs.CONTRACT_OP_MOVE,
        renames=((old_qn, new_qn),),
    )


def _site_key(site: Mapping[str, object]) -> str:
    return f"{site.get(cs.KEY_PATH)}:{site.get(cs.KEY_LINE)}"


def _carried_by_ancestor(
    pair: tuple[str, str], expected: Iterable[tuple[str, str]]
) -> bool:
    """Is this rename the mechanical consequence of an expected one?

    True only when `old` is a strict descendant of an expected `old` AND `new`
    is that expected rename applied to it. Both halves are load-bearing: the
    separator anchor stops `helper` from swallowing the sibling `helperX`, and
    requiring the substituted new name stops `helper.inner -> assist.other`,
    where the child's own segment was renamed too and nobody asked for it.
    """
    old, new = pair
    for expected_old, expected_new in expected:
        prefix = expected_old + cs.SEPARATOR_DOT
        if old.startswith(prefix) and new == expected_new + old[len(expected_old) :]:
            return True
    return False


def _check_symbols(expectation: Expectation, delta: StructuralDelta) -> list[str]:
    failures: list[str] = []
    symbols = delta["symbols"]
    renamed = {(r["old"], r["new"]) for r in symbols["renamed"]}
    for pair in expectation.renames:
        if pair not in renamed:
            failures.append(cs.CONTRACT_RENAME_MISSING.format(old=pair[0], new=pair[1]))
    # Membership in one direction only catches a rename that did NOT happen.
    # A rename that happened as WELL leaves the operation retaining a change
    # nobody asked for, so the set must match rather than merely contain --
    # the same both-directions check `added` and `removed` already get below.
    #
    # Set equality alone is too strong, though: unlike `added`/`removed`, whose
    # elements are independent, a qualified name is a PATH. Renaming `helper`
    # necessarily moves `helper.inner` to `assist.inner` -- the child's own
    # segment did not change, its ancestor's did -- and `_hierarchy` (rename.py)
    # walks only `overrides` edges, so no descendant is ever enumerated. Demanding
    # verbatim equality would roll back every correct rename of a symbol that
    # contains a nested definition, and would make a class move (whose methods
    # all carry the class qn) unsatisfiable by construction.
    unexpected_renames = sorted(
        pair
        for pair in renamed - set(expectation.renames)
        if not _carried_by_ancestor(pair, expectation.renames)
    )
    if expectation.no_unexpected_rename and unexpected_renames:
        failures.append(
            cs.CONTRACT_RENAME_UNEXPECTED.format(
                pairs=", ".join(f"{old} -> {new}" for old, new in unexpected_renames)
            )
        )
    unexpected_added = sorted(set(symbols["added"]) - set(expectation.added))
    unexpected_removed = sorted(set(symbols["removed"]) - set(expectation.removed))
    if expectation.symbol_count_unchanged and (unexpected_added or unexpected_removed):
        failures.append(
            cs.CONTRACT_SYMBOLS_MOVED.format(
                added=", ".join(unexpected_added) or "-",
                removed=", ".join(unexpected_removed) or "-",
            )
        )
    return failures


def _check_callers(expectation: Expectation, delta: StructuralDelta) -> list[str]:
    failures: list[str] = []
    counts = delta["call_sites"]
    if expectation.caller_count_unchanged and counts["before"] != counts["after"]:
        failures.append(
            cs.CONTRACT_CALLERS_MOVED.format(
                before=counts["before"], after=counts["after"]
            )
        )
    if expectation.no_dangling and delta["dangling_callers"]:
        failures.append(
            cs.CONTRACT_DANGLING.format(
                sites=", ".join(_site_key(d) for d in delta["dangling_callers"])
            )
        )
    return failures


def _check_sites_mapped(
    expectation: Expectation, delta: StructuralDelta, rewritten: set[str]
) -> list[str]:
    """Every call site of a changed signature is mapped or explicitly unmapped.

    A site the operation rewrote is mapped by construction (the mapping
    supplied every value); a site it left alone must read `ok` or be
    listed as unmapped. `too_many` is definitive either way.
    """
    if expectation.operation != cs.CONTRACT_OP_CHANGE_SIGNATURE:
        # Only a signature change owes a mapping per site; a rename or a
        # move leaves every call's arguments as they were, and the delta's
        # arity findings list pre-existing faults in the touched files.
        return []
    unmapped = set(expectation.unmapped)
    bad: list[str] = []
    for change in delta["signature_changes"]:
        for site in change["sites"]:
            key = _site_key(site)
            verdict = site["verdict"]
            if verdict == cs.DELTA_ARITY_OK or key in unmapped:
                continue
            if verdict == cs.DELTA_ARITY_POSSIBLY_MISSING and key in rewritten:
                continue
            bad.append(f"{key} ({verdict})")
    for site in delta["arity_findings"]:
        key = _site_key(site)
        if key not in unmapped:
            bad.append(f"{key} ({site['verdict']})")
    if bad:
        return [cs.CONTRACT_SITES_UNMAPPED.format(sites=", ".join(sorted(set(bad))))]
    return []


def _check_rewrites(
    expectation: Expectation, rewritten: Iterable[tuple[str, str | None]]
) -> list[str]:
    """No site resolved by guesswork was rewritten without leave."""
    if expectation.heuristic_allowed:
        return []
    guessed = sorted({key for key, resolution in rewritten if resolution in _AMBIGUOUS})
    if guessed:
        return [cs.CONTRACT_HEURISTIC_REWRITTEN.format(sites=", ".join(guessed))]
    return []


def _check_structure(expectation: Expectation, delta: StructuralDelta) -> list[str]:
    failures: list[str] = []
    if expectation.no_new_cycle and delta["new_import_cycles"]:
        failures.append(
            cs.CONTRACT_NEW_CYCLE.format(
                cycles="; ".join(" -> ".join(c) for c in delta["new_import_cycles"])
            )
        )
    # The renamed symbol is "fresh" to the delta, so a twin that already
    # existed before the edit is reported beside it; the edit introduced no
    # duplicate, only a new name for one half of an old pair.
    expected_new = {new for _old, new in expectation.renames}
    introduced = [
        d
        for d in delta["new_duplicates"]
        if d["qualified_name"] not in expected_new
        and not any(
            d["qualified_name"].startswith(new + cs.SEPARATOR_DOT)
            for new in expected_new
        )
    ]
    if expectation.no_new_duplicate and introduced:
        failures.append(
            cs.CONTRACT_NEW_DUPLICATE.format(
                pairs=", ".join(
                    f"{d['qualified_name']} = {d['original']['qualified_name']}"
                    for d in introduced
                )
            )
        )
    return failures


def verify(
    expectation: Expectation,
    delta: StructuralDelta,
    rewritten: Iterable[tuple[str, str | None]] = (),
    parse_failures: Iterable[str] = (),
) -> Verdict:
    """Pass or fail the delta against the expectation, with reasons.

    `rewritten` lists the sites the operation rewrote as `(path:line,
    resolution)`; `parse_failures` the files the transaction found not to
    parse (the transaction refuses those itself, so a caller normally
    passes none, but the contract states the rule in one place).
    """
    rewritten = list(rewritten)
    failures = [
        *_check_symbols(expectation, delta),
        *_check_callers(expectation, delta),
        *_check_sites_mapped(expectation, delta, {key for key, _r in rewritten}),
        *_check_rewrites(expectation, rewritten),
        *_check_structure(expectation, delta),
    ]
    broken = sorted(set(parse_failures))
    if broken:
        failures.append(cs.CONTRACT_PARSE_FAILED.format(files=", ".join(broken)))
    return Verdict(
        ok=not failures,
        failures=tuple(failures),
        affected_tests=tuple(delta["tests_reaching"]),
        delta=delta,
    )


def measure(
    fetch_all: QueryFn,
    project_name: str,
    repo_root: Path,
    files: Iterable[str],
    reingest: Reingest,
) -> StructuralDelta:
    """The delta of files an operation just wrote, through the re-ingest."""
    paths = sorted(set(files))
    return observe(
        fetch_all, project_name, paths, lambda: reingest(paths), repo_root=repo_root
    )
