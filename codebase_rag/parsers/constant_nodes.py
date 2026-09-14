"""What a module DECLARES as a named constant (issue #1806).

Python only in this stage, and MODULE SCOPE only. A class-level member is
already a `Field` (issue #1805) including a Java `static final`, which carries
its `static`/`final` modifiers; emitting a `Constant` for the same declaration
would put two labels on one qualified name. The `const`/`static` forms of Go,
Rust, C# and the rest follow in a later PR, once the owner has settled whether
a class member should carry both labels.

A language with no enumerator here declares nothing, which means "NOT COVERED",
never "this language has no constants" -- the same contract `field_nodes.py`
states. Reading an empty list as an answer about the source is the error the
wording exists to prevent.

Python has no `const` keyword, so the enumerator is a naming heuristic plus an
explicit `Final` annotation. Measured on this repo by the issue's author:
**5,489 UPPER_CASE module-level assignments against 190 lowercase incidental
ones**, so the convention is followed closely enough to key on.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..services import IngestorProtocol
from .type_facts import TypeReferenceResolver
from .utils import safe_decode_text

# `MAX`, `MAX_SIZE`, `HTTP2_PREFACE`. A single leading uppercase letter, then
# uppercase, digits and underscores only -- so `Config` and `maxSize` are not
# constants while `X` is.
_UPPER_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# `__all__`, `__version__`. Module dunders are machinery, not constants, and
# `__all__` in particular is a re-export list every module carries.
_DUNDER = re.compile(r"^__.*__$")
# `Final`, `Final[int]`, `typing.Final`, `t.Final[str]`. The annotation makes a
# constant regardless of case: `Final` is the language saying so explicitly,
# where UPPER_CASE is only a convention.
_FINAL = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*Final(?:\s*\[.*\])?$", re.DOTALL)
# The argument of a `Final[...]` wrapper, so `x: Final[int]` has type `int`.
_FINAL_ARG = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*Final\s*\[(?P<arg>.*)\]$", re.DOTALL
)


class DeclaredConstant(NamedTuple):
    name: str
    # Of the NAME: 1-based line, 0-based column, matching the owner node's own
    # convention and Parameter's and Field's.
    start_line: int
    start_col: int
    # The annotation as written with a `Final[...]` wrapper unwrapped, or None
    # when the assignment carries no annotation.
    type_name: str | None
    # The right-hand side as written and stripped, or None when it is longer
    # than `cs.CONSTANT_VALUE_MAX_CHARS`. Absent rather than truncated: a
    # truncated literal reads as a complete one, which is wrong in the
    # reassuring direction.
    value: str | None
    node: Node


def declared_constants(
    module_root: Node, language: cs.SupportedLanguage | None
) -> list[DeclaredConstant]:
    """Per-language dispatch over a MODULE root node.

    Only Python is covered in this stage. Every other language returns `[]`,
    which means "NOT COVERED" and never "this module declares no constants".
    """
    if language == cs.SupportedLanguage.PYTHON:
        return python_declared_constants(module_root)
    return []


def python_declared_constants(root: Node) -> list[DeclaredConstant]:
    """Module-level constants of one Python module.

    MODULE SCOPE ONLY: direct children of the module node. A statement nested
    inside a module-level `if`, `try` or `with` is NOT included, even though it
    binds a module-level name -- a conditional definition has no single value,
    position or type, and picking one branch would be a silent choice. A
    constant declared inside a function or a class body is likewise out: a
    class member is a `Field` (issue #1805).

    A constant is an `expression_statement > assignment` whose target is a
    single bare identifier that is either

    * UPPER_CASE (`^[A-Z][A-Z0-9_]*$`), or
    * annotated `Final` / `Final[...]` / `typing.Final`, whatever its case.

    Skipped: tuple and list targets (`A, B = 1, 2` -- no one target owns the
    right-hand side), attribute targets (`obj.X = 1` binds no module name),
    augmented assignment (`X += 1` mutates, it does not declare), and dunders
    (`^__.*__$`, which covers `__all__` and `__version__`).
    """
    out: list[DeclaredConstant] = []
    for statement in root.children:
        assignment = _module_assignment(statement)
        if assignment is None:
            continue
        out.extend(_python_constants(assignment))
    return out


def _module_assignment(statement: Node) -> Node | None:
    """The `assignment` a module-level `expression_statement` holds, if any.

    `augmented_assignment` is a different node type and never matches, which
    is how `X += 1` is skipped.
    """
    if statement.type != cs.TS_PY_EXPRESSION_STATEMENT or not statement.children:
        return None
    assignment = statement.children[0]
    return assignment if assignment.type == cs.TS_PY_ASSIGNMENT else None


def _python_constants(assignment: Node) -> list[DeclaredConstant]:
    """Every constant a module-level assignment declares.

    Usually one. A CHAINED assignment (`MAX = MIN = 0`) declares several: the
    grammar nests a further `assignment` as the outer one's `right`, so each
    name binds the SAME innermost value. Walking the chain is what keeps
    `MAX.value` as `0` rather than the source text `MIN = 0` -- a malformed
    literal that reads as a valid one, which is the failure direction the
    value cap exists to avoid -- and what keeps `MIN` from being dropped
    entirely (local review).
    """
    names: list[Node] = []
    current = assignment
    while True:
        left = current.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return []
        names.append(left)
        right = current.child_by_field_name(cs.FIELD_RIGHT)
        if right is None or right.type != cs.TS_PY_ASSIGNMENT:
            break
        current = right
    # The annotation belongs to the outermost target; a chained assignment
    # cannot carry one (`A: int = B = 1` is a syntax error), so reading it
    # from `assignment` is correct for both shapes.
    value = _value_text(current)
    out: list[DeclaredConstant] = []
    for left in names:
        constant = _python_constant(assignment, left, value)
        if constant is not None:
            out.append(constant)
    return out


def _python_constant(
    assignment: Node, left: Node, value: str | None
) -> DeclaredConstant | None:
    """One constant from one target of a module-level assignment."""
    # A tuple/list target or an `obj.X` attribute target is not a bare
    # identifier, so both fall out here.
    if left.type != cs.TS_PY_IDENTIFIER:
        return None
    name = safe_decode_text(left)
    if not name or _DUNDER.match(name):
        return None
    annotation = _annotation_text(assignment)
    is_final = annotation is not None and _FINAL.match(annotation) is not None
    if not is_final and not _UPPER_CASE.match(name):
        return None
    return DeclaredConstant(
        name=name,
        start_line=left.start_point[0] + 1,
        start_col=left.start_point[1],
        type_name=_type_name(annotation),
        value=value,
        node=assignment,
    )


def _annotation_text(assignment: Node) -> str | None:
    """The annotation as written, or None when the assignment carries none."""
    type_node = assignment.child_by_field_name(cs.FIELD_TYPE)
    if type_node is None:
        return None
    return safe_decode_text(type_node) or None


def _type_name(annotation: str | None) -> str | None:
    """The declared type, with a `Final[...]` wrapper unwrapped to its argument.

    `x: Final[int]` declares an `int`; `x: Final` declares nothing, so it is
    None rather than the string "Final" -- a bare `Final` names no type and
    recording it would send `OF_TYPE` looking for a class called `Final`.
    """
    if annotation is None:
        return None
    match = _FINAL_ARG.match(annotation)
    if match is not None:
        return match.group("arg").strip() or None
    if _FINAL.match(annotation):
        return None
    return annotation


def _value_text(assignment: Node) -> str | None:
    """The right-hand side as written and stripped, capped by length.

    Over the cap the value is recorded as ABSENT rather than truncated: a
    generated lookup table can be megabytes on one line, and a truncated
    literal is indistinguishable from a complete one to every later reader.
    """
    right = assignment.child_by_field_name(cs.FIELD_RIGHT)
    if right is None:
        return None
    text = safe_decode_text(right)
    if not text:
        return None
    text = text.strip()
    if not text or len(text) > cs.CONSTANT_VALUE_MAX_CHARS:
        return None
    return text


# --- Emission -----------------------------------------------------------------
#
# The same shape as `field_nodes.emit_declared_fields`, with the Module as
# owner: gate on the capture selection, emit the node and its DEFINES_CONSTANT
# edge together (never one without the other, or the node orphans), and queue
# the annotation for the deferred OF_TYPE pass after Pass 2, when every project
# type is registered. Mirrored rather than shared so #1804's and #1805's
# modules are not edited from this branch; unifying the three is a follow-up.


class PendingConstantType(NamedTuple):
    """A constant's declared type, held until every file's types are registered."""

    constant_qn: str
    module_qn: str
    type_name: str
    # The owning file's relative path: scoped re-ingestion discards facts by
    # FILE, because two same-stem files (`foo.py`, `foo/__init__.py`) derive
    # one module qn from their paths (#1891 round 3, #1892).
    path: str


def emit_declared_constants(
    ingestor: IngestorProtocol,
    sink: list[PendingConstantType] | None,
    module_qn: str,
    module_root: Node,
    language: cs.SupportedLanguage | None,
    module_props: dict,
) -> int:
    """Constant nodes and DEFINES_CONSTANT edges for one module.

    Returns the count. Must be called AFTER the Module node is queued: a batch
    flush writes nodes before relationships, so an edge emitted first could
    match nothing (#1891's CodeRabbit finding at the method site).
    """
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.DEFINES_CONSTANT):
        return 0
    declared = declared_constants(module_root, language)
    if not declared:
        return 0
    path = module_props.get(cs.KEY_PATH)
    absolute_path = module_props.get(cs.KEY_ABSOLUTE_PATH)
    owner = (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, module_qn)
    # A repeated declaration (`THING: A = A()` then `THING = 1`) is ONE node:
    # the writes MERGE on the qualified name. But the merge is ADDITIVE, so
    # emitting both rows left the node carrying `type_name` from the first and
    # `value` from the second -- a state no source ever had (bot review). The
    # same applied to the type edge, which was emitted once per declaration.
    #
    # So collapse FIRST and emit once. Last in source order wins the whole
    # row, which is what the binding does at runtime, and an absent property
    # then genuinely clears rather than leaving the earlier one showing.
    final: dict[str, DeclaredConstant] = {}
    for constant in declared:
        final[f"{module_qn}{cs.SEPARATOR_DOT}{constant.name}"] = constant
    for constant_qn, constant in final.items():
        ingestor.ensure_node_batch(
            cs.NodeLabel.CONSTANT,
            _constant_props(constant, constant_qn, path, absolute_path),
        )
        ingestor.ensure_relationship_batch(
            owner,
            cs.RelationshipType.DEFINES_CONSTANT,
            (cs.NodeLabel.CONSTANT.value, cs.KEY_QUALIFIED_NAME, constant_qn),
        )
        if sink is not None and constant.type_name and isinstance(path, str):
            sink.append(
                PendingConstantType(constant_qn, module_qn, constant.type_name, path)
            )
    return len(final)


def _constant_props(
    constant: DeclaredConstant,
    constant_qn: str,
    path: object,
    absolute_path: object,
) -> dict:
    """The Constant node's properties; optional ones are absent, not None."""
    props: dict = {
        cs.KEY_QUALIFIED_NAME: constant_qn,
        cs.KEY_NAME: constant.name,
        cs.KEY_START_LINE: constant.start_line,
        cs.KEY_START_COL: constant.start_col,
    }
    if path is not None:
        props[cs.KEY_PATH] = path
    if absolute_path is not None:
        props[cs.KEY_ABSOLUTE_PATH] = absolute_path
    if constant.type_name:
        props[cs.KEY_TYPE_NAME] = constant.type_name
    if constant.value:
        props[cs.KEY_VALUE] = constant.value
    return props


def emit_constant_type_edges(
    pending: list[PendingConstantType],
    resolver: TypeReferenceResolver,
    ingestor: IngestorProtocol,
) -> int:
    """OF_TYPE edges for every queued constant, after Pass 2.

    One resolve per DISTINCT (declared type, module), as the sibling passes do.
    """
    memo: dict[tuple[str, str], list[str]] = {}
    emitted = 0
    for fact in pending:
        key = (fact.type_name, fact.module_qn)
        targets = memo.get(key)
        if targets is None:
            targets = memo[key] = resolver.resolve_annotation(
                fact.type_name, fact.module_qn
            )
        source = (cs.NodeLabel.CONSTANT.value, cs.KEY_QUALIFIED_NAME, fact.constant_qn)
        for target_qn in targets:
            ingestor.ensure_relationship_batch(
                source,
                cs.RelationshipType.OF_TYPE,
                (str(resolver._registry[target_qn]), cs.KEY_QUALIFIED_NAME, target_qn),
            )
            emitted += 1
    # Emptied like the sibling passes: a reused updater (watch mode) would
    # otherwise re-resolve every old fact each run and re-emit OF_TYPE from a
    # Constant that no longer exists (#1899's local review P1).
    pending.clear()
    return emitted


__all__ = [
    "DeclaredConstant",
    "PendingConstantType",
    "declared_constants",
    "emit_constant_type_edges",
    "emit_declared_constants",
    "python_declared_constants",
]
