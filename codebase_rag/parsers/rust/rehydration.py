"""What an incremental sync reads back from the graph for the Rust definitions
it did not re-parse: the return types a receiver is typed from, and the trait
impls whose implementer has no node to hang an IMPLEMENTS edge on."""

from collections.abc import Iterable, Mapping

from tree_sitter import Parser

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, ResultRow
from ...utils import qn_markers
from ...utils.path_utils import walk_position
from . import utils as rs_utils


def _registration_order(
    row: ResultRow,
) -> tuple[tuple[tuple[str, ...], str], tuple[int, int]]:
    """Where a definition row's qn fell among its same-named twins.

    A clean build registers files in walk order, and within a file the first
    twin it meets takes the natural qn and each later one an `@line` (then
    `@line_col`) marker in document order. The marker alone cannot order
    twins from two files: an inline `mod b` and the file `b.rs` both declare
    into `a.b` (#2805).
    """
    qn = row.get(cs.KEY_QUALIFIED_NAME)
    path = row.get(cs.KEY_PATH)
    position = qn_markers.marker_position(qn) if isinstance(qn, str) else None
    return walk_position(path if isinstance(path, str) else ""), position or (0, -1)


def rehydrate_return_types(
    rows: list[ResultRow],
    parsers: Mapping[cs.SupportedLanguage, Parser],
    function_registry: FunctionRegistryTrieProtocol,
    return_types: dict[str, str],
) -> None:
    # Pass 2 records a Rust fn's return type only while it parses the
    # file, and a chained or call-bound receiver (`Get::new(k).m()`,
    # `let b = Builder::new(); b.m()`) types from that record. Without it
    # an unchanged file's fn types nothing, and the method falls to a
    # name-only match a clean index never makes (issue #2559).
    records = [
        (_registration_order(row), record)
        for row in rows
        if (record := _return_type_record(row, function_registry)) is not None
    ]
    records.sort(key=lambda ordered: ordered[0])
    if not records:
        return
    # Read only now: the parser view loads a grammar on first access, and
    # a project with no Rust must not pay for one.
    parser = parsers.get(cs.SupportedLanguage.RUST)
    if parser is None:
        return
    reduced: dict[tuple[str, str | None], str | None] = {}
    restored: dict[str, str] = {}
    # Methods of two trait impls on one type share a key (`S.run` and
    # `S.run@13`), and parsing writes each one's type over the last in
    # registration order. The rows come in no fixed order, so they are
    # replayed in that order and the last typed one stands (#2805).
    for _position, (key, annotation, impl_target) in records:
        if (annotation, impl_target) not in reduced:
            reduced[(annotation, impl_target)] = (
                rs_utils.return_type_name_from_annotation(
                    parser, annotation, impl_target
                )
            )
        if name := reduced[(annotation, impl_target)]:
            restored[key] = name
    for key, name in restored.items():
        # A re-parsed file's own record is current; keep it.
        return_types.setdefault(key, name)


def _return_type_record(
    row: ResultRow, function_registry: FunctionRegistryTrieProtocol
) -> tuple[str, str, str | None] | None:
    """(record key, return annotation, impl target) for a Rust fn row."""
    qn = row.get(cs.KEY_QUALIFIED_NAME)
    label = row.get(cs.KEY_LABEL)
    path = row.get(cs.KEY_PATH)
    annotation = row.get(cs.KEY_RETURN_TYPE)
    if not (
        isinstance(qn, str)
        and isinstance(path, str)
        and isinstance(annotation, str)
        and annotation
        and path.endswith(cs.EXT_RS)
    ):
        return None
    if label == cs.NodeLabel.FUNCTION.value:
        # A free fn has no impl, so its `Self` names nothing.
        return qn, annotation, None
    if label != cs.NodeLabel.METHOD.value:
        return None
    # Pass 2 keys an impl method by its impl target and name, which the
    # natural qn spells, and records none for a trait's own methods.
    natural = qn_markers.natural_qn(qn)
    owner = natural.rpartition(cs.SEPARATOR_DOT)[0]
    if function_registry.get(owner) == cs.NodeLabel.INTERFACE:
        return None
    return natural, annotation, owner.rpartition(cs.SEPARATOR_DOT)[2]


def may_hide_impls(
    interface_qn: str, closed: set[str], definition_paths: Mapping[str, str]
) -> bool:
    # An impl of a Rust trait whose every method has a default body may
    # define nothing, and one for a type with no node (`impl Visit for ()
    # {}`) then leaves no trace in the graph: the pairs read back would
    # count one implementer short and invent a sole-implementer edge a
    # clean index does not emit. Such a trait, read back rather than
    # parsed, keeps to its parsed pairs as before. A trait parsed this run
    # has every implementer that left a trace re-parsed beside it.
    path = definition_paths.get(interface_qn)
    return path is not None and path.endswith(cs.EXT_RS) and interface_qn not in closed


def nodeless_impls(
    override_rows: list[ResultRow], function_registry: FunctionRegistryTrieProtocol
) -> Iterable[tuple[str, str]]:
    """(implementer, trait) for each Rust impl whose type has no node."""
    # `impl FloatErrors for u64` has no node to hang an IMPLEMENTS edge
    # on, so its methods' OVERRIDES name the pair. Only such a type: one
    # with a node has its IMPLEMENTS edge, and a trait's default method
    # overriding a supertrait's names no implementer at all.
    for row in override_rows:
        method = row.get(cs.KEY_FROM_QN)
        base = row.get(cs.KEY_TO_QN)
        if not isinstance(method, str) or not isinstance(base, str):
            continue
        implementer = method.rpartition(cs.SEPARATOR_DOT)[0]
        trait = base.rpartition(cs.SEPARATOR_DOT)[0]
        if (
            implementer
            and implementer not in function_registry
            and function_registry.get(trait) == cs.NodeLabel.INTERFACE
        ):
            yield implementer, trait
