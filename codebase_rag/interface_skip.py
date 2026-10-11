"""Which dependents of a re-parsed file a sync may leave alone (issue #2531).

A sync re-parses every file that depends on a changed one, because a change
can move what a dependent binds to. When the changed file's interface
fingerprint (`parsers/interface_fingerprint.py`) is what the graph stored for
it, nothing a dependent resolves against moved: the dependent would bind
exactly as it does now, so its edges into the file are captured and restored
by qualified name instead, the way the edges of every other unchanged file
already are.

That restore is only as good as what it can bring back, so a dependent still
re-parses when one of its edges with the changed file is anything but a
restorable one -- an edge kind the capture leaves behind, an edge from the
changed file into the dependent (its own parse may have written it), or a
target whose qualified name a position may have minted (`f@12`,
`anonymous_3_4`), which an edit above it renames. And a file holding a
function that invokes a callable another file passes it keeps no
dependent's edges at all: the flow edge to that callable starts in the file
and is written by the passer's parse. These functions read the rows the
graph answered with; the queries and their failure handling stay with the
updater.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from . import constants as cs
from .types_defs import ResultRow, ResultValue


def stable_interface_modules(
    rows: list[ResultRow],
    fresh: Mapping[str, str],
    longer_projects: Collection[str],
) -> dict[str, str]:
    """{path: module qn} of each file whose stored fingerprint is `fresh[path]`.

    A path with no Module, or with more than one under this project, is not
    stable: there is nothing unambiguous to compare against. A module of a
    longer project (`proj.sub` under `proj`) can share a relative path with
    one of ours, so its rows are dropped first.
    """
    stored: dict[str, list[tuple[str, ResultValue]]] = {}
    for row in rows:
        path = row.get(cs.KEY_PATH)
        qn = row.get(cs.KEY_QUALIFIED_NAME)
        if (
            isinstance(path, str)
            and isinstance(qn, str)
            and not any(_within(qn, project) for project in longer_projects)
        ):
            stored.setdefault(path, []).append(
                (qn, row.get(cs.KEY_INTERFACE_FINGERPRINT))
            )
    return {
        path: modules[0][0]
        for path, modules in stored.items()
        if path in fresh and len(modules) == 1 and modules[0][1] == fresh[path]
    }


def without_flow_sources(
    stable: Mapping[str, str], rows: list[ResultRow]
) -> dict[str, str]:
    """`stable` less every file a row names as the start of a callback flow
    edge: that edge is written by the parse of whoever passed the callback,
    so the dependents must re-parse with the file, as before."""
    sources = {path for row in rows if isinstance(path := row.get(cs.KEY_PATH), str)}
    return {path: qn for path, qn in stable.items() if path not in sources}


def _within(qualified_name: str, project: str) -> bool:
    return qualified_name == project or qualified_name.startswith(
        project + cs.SEPARATOR_DOT
    )


def callers_to_reparse(
    rows: list[ResultRow], candidates: Collection[str], modules: Mapping[str, str]
) -> set[str]:
    """The candidate callers holding an edge the inbound restore cannot
    bring back unchanged; `modules` maps each stable file to its module qn."""
    return {
        caller
        for row in rows
        if isinstance(caller := row.get(cs.KEY_CALLER_PATH), str)
        and caller in candidates
        and not _restorable(row, modules)
    }


def _restorable(row: ResultRow, modules: Mapping[str, str]) -> bool:
    caller_label = row.get(cs.KEY_CALLER_LABEL)
    target_label = row.get(cs.KEY_TARGET_LABEL)
    target_path = row.get(cs.KEY_TARGET_PATH)
    return (
        row.get(cs.KEY_INBOUND) is True
        and row.get(cs.KEY_REL) in cs.INTERFACE_RESTORED_RELS
        and isinstance(caller_label, str)
        and cs.NODE_UNIQUE_CONSTRAINTS.get(caller_label) == cs.KEY_QUALIFIED_NAME
        and target_label in cs.INTERFACE_RESTORED_TARGET_LABELS
        and isinstance(target_path, str)
        and _position_free(row.get(cs.KEY_TARGET_QN), modules.get(target_path))
    )


def _position_free(target_qn: ResultValue, module_qn: str | None) -> bool:
    """Whether a target's name cannot have been minted from its position.

    Every position-derived name cgr mints carries the position as digits
    (`@12`, `@12_5`, `anonymous_3_4`, `lambda_3_4`, `iife_func_3_4`), so a
    name below its module with no digit in it is safe. A digit for any other
    reason (`sha256`) only costs that caller a re-parse.
    """
    if not isinstance(target_qn, str) or module_qn is None:
        return False
    if target_qn == module_qn:
        return True
    head, separator, tail = target_qn.partition(module_qn + cs.SEPARATOR_DOT)
    return (
        not head
        and bool(separator)
        and not any(character.isdigit() for character in tail)
    )
