"""Edit algebra op 2: `change_signature(qn, new_params, mapping)` (issue #1533).

A parameter list changed by hand is where call sites drift: one caller
missed, a positional value silently shifted into the wrong slot. The graph
knows every site, so the change is a graph operation:

1. read the definition's parameter list and, for a method on a hierarchy,
   the list of every override in both directions (they must agree);
2. work out where each new parameter's value comes from at a call site: an
   old parameter carried by name or by index, or a literal the mapping
   supplies for every site;
3. rewrite the definition(s) and every graph-known call site through the
   span patcher (issue #1529), stage the result in a transaction (issue
   #1528), and commit, or roll back and report why.

A site the mapping cannot complete -- a value the caller never passed for a
parameter without a default, a splat, a surplus argument, a keyword the
definition does not declare -- is left exactly as written and reported as
`unmapped`; so is a site the graph bound by guesswork (`heuristic`,
`overload`, `dynamic`) unless the caller accepts the risk with
`allow_heuristic`. With a re-ingest the applied change is measured through
the structural delta and undone when its postcondition contract (issue
#1531) fails: every site of the changed signature must read as mapped or
be in the unmapped list.

Python only for now: the header and the argument lists are read with the
Python grammar, and a definition in any other language refuses (issue
#1908).
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node, Parser

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn
from ..graph_updater import ReingestAborted
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.utils import _is_static_decorator
from .contract import Reingest, Verdict, change_signature_expectation, measure, verify
from .patcher import Patcher, PatcherError
from .sites import AMBIGUOUS, call_node_at, hierarchy
from .transaction import EditTransaction, TransactionConflict, undo_transaction

# Issue tracking support for definitions in other languages.
LANGUAGES_ISSUE = 1908

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_LITERAL_PREFIX = "="
_DEFINITION = "definition"
_CALL = "call"
# Runtime types a literal may have under a builtin annotation. `float`
# admits an int the way the type checkers do; `bool` is not an `int` here
# because `flag: int = True` is almost always a mistake.
_BUILTIN_TYPES: dict[str, frozenset[type]] = {
    int.__name__: frozenset({int}),
    float.__name__: frozenset({int, float}),
    complex.__name__: frozenset({int, float, complex}),
    str.__name__: frozenset({str}),
    bool.__name__: frozenset({bool}),
    bytes.__name__: frozenset({bytes}),
    list.__name__: frozenset({list}),
    tuple.__name__: frozenset({tuple}),
    dict.__name__: frozenset({dict}),
    set.__name__: frozenset({set}),
    frozenset.__name__: frozenset({frozenset}),
}


class ParamSpec(NamedTuple):
    name: str
    text: str  # as written in a header: `b: str = 'x'`
    annotation: str | None
    has_default: bool


class SignatureSite(NamedTuple):
    kind: str  # "definition" or "call"
    path: str
    line: int
    col: int
    owner: str  # the qualified name the site belongs to
    resolution: str | None


class UnmappedSite(NamedTuple):
    owner: str
    path: str
    line: int | None
    col: int | None
    reason: str


class SignatureRefused(ValueError):
    """The change cannot be stated safely; nothing was changed."""


class SignatureReport(NamedTuple):
    qualified_name: str
    old_params: tuple[str, ...]
    new_params: tuple[str, ...]
    applied: bool
    transaction_id: str
    files: tuple[str, ...]
    sites: tuple[SignatureSite, ...]
    unmapped: tuple[UnmappedSite, ...]
    hierarchy: tuple[str, ...]
    diff: str
    message: str
    verdict: Verdict | None = None
    # True when a rollback re-ingest failed after the files were restored:
    # the graph may hold a partial picture and must be rebuilt.
    graph_incomplete: bool = False


class _Source(NamedTuple):
    """Where a new parameter's value comes from at a call site."""

    index: int | None = None  # an old parameter, by position
    literal: str | None = None  # the same text at every site


class _Header(NamedTuple):
    qn: str
    path: str
    span: tuple[int, int]  # byte span of the `parameters` node
    line: int
    col: int
    receiver: str | None  # `self`/`cls`, kept verbatim and never remapped
    params: list[ParamSpec]
    function: Node  # the `function_definition`, for its body
    source: bytes


class _Binding(NamedTuple):
    text: str
    keyword: bool
    position: int  # index among the site's arguments, for keyword order
    span: tuple[int, int]  # byte span of the value, for edits nested in it


class _Edit(NamedTuple):
    path: str
    span: tuple[int, int]
    text: str


class _Candidate(NamedTuple):
    """A call site the graph located and the mapping can read, not yet rendered."""

    site: SignatureSite
    span: tuple[int, int]  # byte span of the argument list
    text: str  # the argument list as written
    bound: dict[int, _Binding]


class _Unmapped(Exception):
    """A call site the mapping cannot rewrite; carries the reason."""


# --- the mapping ------------------------------------------------------------------


def parse_mapping(entries: Iterable[str]) -> dict[str, str]:
    """`NEW=SOURCE` entries as the CLI takes them, into the mapping.

    The split is on the first `=`, so `n==1` maps `n` to the literal `1`.
    """
    mapping: dict[str, str] = {}
    for entry in entries:
        new, sep, source = entry.partition(_LITERAL_PREFIX)
        if not sep or not new:
            raise SignatureRefused(cs.SIGNATURE_BAD_MAP.format(entry=entry))
        mapping[new] = source
    return mapping


def _resolve_sources(
    new: Sequence[ParamSpec], old: Sequence[ParamSpec], mapping: Mapping[str, str]
) -> list[_Source | None]:
    """One source per new parameter; None where the mapping says nothing."""
    old_names = [p.name for p in old]
    new_names = [p.name for p in new]
    for name in mapping:
        if name not in new_names:
            raise SignatureRefused(
                cs.SIGNATURE_MAPPING_UNKNOWN_NEW.format(
                    name=name, names=cs.SEPARATOR_COMMA_SPACE.join(new_names)
                )
            )
    sources: list[_Source | None] = []
    fed: dict[int, str] = {}
    for spec in new:
        source = _source_for(spec, mapping.get(spec.name), old_names)
        if source is not None and source.index is not None:
            if source.index in fed:
                raise SignatureRefused(
                    cs.SIGNATURE_MAPPING_DUPLICATE.format(
                        old=old_names[source.index],
                        first=fed[source.index],
                        second=spec.name,
                    )
                )
            fed[source.index] = spec.name
        sources.append(source)
    return sources


def _source_for(
    spec: ParamSpec, text: str | None, old_names: Sequence[str]
) -> _Source | None:
    if text is None:
        # Not mentioned: the old parameter of the same name, if any.
        if spec.name in old_names:
            return _Source(index=old_names.index(spec.name))
        return None
    if text.startswith(_LITERAL_PREFIX):
        literal = text[len(_LITERAL_PREFIX) :].strip()
        if not literal:
            raise SignatureRefused(
                cs.SIGNATURE_MAPPING_EMPTY_LITERAL.format(name=spec.name)
            )
        return _Source(literal=literal)
    if text.isdigit():
        index = int(text)
        if index >= len(old_names):
            raise SignatureRefused(
                cs.SIGNATURE_MAPPING_BAD_INDEX.format(
                    name=spec.name, index=index, count=len(old_names)
                )
            )
        return _Source(index=index)
    if text in old_names:
        return _Source(index=old_names.index(text))
    raise SignatureRefused(
        cs.SIGNATURE_MAPPING_UNKNOWN_OLD.format(
            name=spec.name,
            source=text,
            names=cs.SEPARATOR_COMMA_SPACE.join(old_names),
        )
    )


# --- the new parameter list -----------------------------------------------------


def _parse_new_param(text: str, old: Mapping[str, ParamSpec]) -> ParamSpec:
    """A new parameter as the caller spelled it.

    A bare identifier naming an old parameter carries that parameter over
    with its annotation and default; anything else must parse as exactly
    one plain positional-or-keyword parameter.
    """
    text = text.strip()
    if _IDENTIFIER_RE.fullmatch(text) and text in old:
        return old[text]
    probe = cs.SIGNATURE_PARAM_PROBE.format(text=text)
    try:
        module = ast.parse(probe)
    except (SyntaxError, ValueError) as error:
        raise SignatureRefused(cs.SIGNATURE_BAD_PARAM.format(text=text)) from error
    function = module.body[0] if len(module.body) == 1 else None
    if not isinstance(function, ast.FunctionDef):
        raise SignatureRefused(cs.SIGNATURE_BAD_PARAM.format(text=text))
    args = function.args
    plain = (
        len(args.args) == 1
        and not args.posonlyargs
        and not args.kwonlyargs
        and args.vararg is None
        and args.kwarg is None
    )
    if not plain:
        raise SignatureRefused(cs.SIGNATURE_BAD_PARAM.format(text=text))
    arg = args.args[0]
    annotation = (
        ast.get_source_segment(probe, arg.annotation)
        if arg.annotation is not None
        else None
    )
    return ParamSpec(arg.arg, text, annotation, bool(args.defaults))


def _accepted_types(node: ast.expr) -> set[type] | None:
    """Runtime types a literal may take under this annotation.

    None when the annotation is not one this check reads (a project class, a
    `typing` alias other than Optional/Union, an attribute): those are not
    refused, merely not checked.
    """
    if isinstance(node, ast.Constant) and node.value is None:
        return {type(None)}
    if isinstance(node, ast.Name):
        accepted = _BUILTIN_TYPES.get(node.id)
        return set(accepted) if accepted is not None else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_of([node.left, node.right])
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return _subscript_types(node.value, node.slice)
    return None


def _subscript_types(head: ast.Name, index: ast.expr) -> set[type] | None:
    """`Optional[X]`, `Union[X, Y]`, or a subscripted builtin like `list[int]`."""
    if head.id == cs.PY_TYPING_OPTIONAL:
        inner = _accepted_types(index)
        return inner | {type(None)} if inner is not None else None
    if head.id == cs.PY_TYPING_UNION:
        members = list(index.elts) if isinstance(index, ast.Tuple) else [index]
        return _union_of(members)
    # `list[int]` is a list; the element type is not checked.
    return _accepted_types(head)


def _union_of(members: Iterable[ast.expr]) -> set[type] | None:
    accepted: set[type] = set()
    for member in members:
        part = _accepted_types(member)
        if part is None:
            return None
        accepted |= part
    return accepted


def _check_literal(spec: ParamSpec, literal: str) -> None:
    """Refuse a literal that cannot be a value of the declared type."""
    if spec.annotation is None:
        return
    try:
        value = ast.literal_eval(literal)
        annotation = ast.parse(spec.annotation, mode="eval").body
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        # Not a literal (`=LIMIT`), or an annotation Python cannot parse:
        # nothing to compare.
        return
    accepted = _accepted_types(annotation)
    if accepted is not None and type(value) not in accepted:
        raise SignatureRefused(
            cs.SIGNATURE_LITERAL_MISMATCH.format(
                literal=literal, name=spec.name, annotation=spec.annotation
            )
        )


def _new_specs(new_params: Sequence[str], old: Sequence[ParamSpec]) -> list[ParamSpec]:
    by_name = {spec.name: spec for spec in old}
    specs = [_parse_new_param(text, by_name) for text in new_params]
    seen: set[str] = set()
    defaulted = False
    for spec in specs:
        if spec.name in seen:
            raise SignatureRefused(cs.SIGNATURE_DUPLICATE_NEW.format(name=spec.name))
        seen.add(spec.name)
        if spec.has_default:
            defaulted = True
        elif defaulted:
            raise SignatureRefused(
                cs.SIGNATURE_REQUIRED_AFTER_DEFAULT.format(name=spec.name)
            )
    return specs


# --- the header -----------------------------------------------------------------


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode(cs.ENCODING_UTF8)


def _is_static(function: Node, source: bytes) -> bool:
    """Decorated `@staticmethod`: every parameter is one the caller passes.

    Read the way the indexer reads decorators, by the last name, so the
    qualified `@builtins.staticmethod` counts too.
    """
    parent = function.parent
    if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
        return False
    return _is_static_decorator(
        [
            _text(child, source)
            for child in parent.children
            if child.type == cs.TS_PY_DECORATOR
        ]
    )


def _find_definition(root: Node, name: str, start: int, end: int) -> Node | None:
    """The `function_definition` named `name` starting within lines start..end."""
    wanted = name.encode(cs.ENCODING_UTF8)
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if (
            node.type == cs.TS_PY_FUNCTION_DEFINITION
            and start <= node.start_point[0] + 1 <= end
        ):
            ident = node.child_by_field_name(cs.FIELD_NAME)
            if ident is not None and ident.text == wanted:
                return node
        if node.start_point[0] < end and node.end_point[0] >= start - 1:
            stack.extend(reversed(node.children))
    return None


def _param_spec(node: Node, source: bytes) -> ParamSpec | None:
    """A plain positional-or-keyword parameter, or None for anything else."""
    text = _text(node, source)
    if node.type == cs.TS_PY_IDENTIFIER:
        return ParamSpec(text, text, None, False)
    if node.type == cs.TS_PY_TYPED_PARAMETER:
        # `*args: int` is a typed_parameter over a splat pattern.
        first = node.named_children[0] if node.named_children else None
        annotation = node.child_by_field_name(cs.FIELD_TYPE)
        if first is None or first.type != cs.TS_PY_IDENTIFIER or annotation is None:
            return None
        return ParamSpec(_text(first, source), text, _text(annotation, source), False)
    if node.type in (cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER):
        name = node.child_by_field_name(cs.FIELD_NAME)
        if name is None or name.type != cs.TS_PY_IDENTIFIER:
            return None
        annotation = node.child_by_field_name(cs.FIELD_TYPE)
        return ParamSpec(
            _text(name, source),
            text,
            _text(annotation, source) if annotation is not None else None,
            True,
        )
    return None


# --- renamed parameters in the body ----------------------------------------------

# Scopes of their own inside a function body. A comprehension binds only its
# `for` targets, so one that merely reads the parameter follows the rename;
# a nested function, lambda or class can bind the name in ways the body walk
# cannot see (an assignment there is a fresh local), so any use of the name
# inside one refuses.
_COMPREHENSIONS = frozenset(
    {
        cs.TS_PY_LIST_COMPREHENSION,
        cs.TS_PY_SET_COMPREHENSION,
        cs.TS_PY_DICTIONARY_COMPREHENSION,
        cs.TS_PY_GENERATOR_EXPRESSION,
    }
)
_OWN_SCOPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA, cs.TS_PY_CLASS_DEFINITION}
)
_REBINDING_STATEMENTS = frozenset(
    {
        cs.TS_PY_GLOBAL_STATEMENT,
        cs.TS_PY_NONLOCAL_STATEMENT,
        cs.TS_PY_IMPORT_STATEMENT,
        cs.TS_PY_IMPORT_FROM_STATEMENT,
    }
)
_OPAQUE_TO_THE_WALK = _OWN_SCOPES | _REBINDING_STATEMENTS


def _is_a_reference(node: Node) -> bool:
    """A bare identifier, not the `.attr` of an attribute or a keyword's name."""
    parent = node.parent
    if parent is None:
        return True
    if parent.type == cs.TS_PY_ATTRIBUTE:
        return parent.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE) != node
    if parent.type == cs.TS_PY_KEYWORD_ARGUMENT:
        return parent.child_by_field_name(cs.FIELD_NAME) != node
    return True


def _mentions(node: Node, name: bytes) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_PY_IDENTIFIER and current.text == name:
            return True
        stack.extend(current.children)
    return False


def _comprehension_binds(node: Node, name: bytes) -> bool:
    return any(
        child.type == cs.TS_PY_FOR_IN_CLAUSE
        and (left := child.child_by_field_name(cs.FIELD_LEFT)) is not None
        and _mentions(left, name)
        for child in node.children
    )


def _body_references(
    header: _Header, old: str, new: str, renamed_away: Iterable[str]
) -> list[tuple[int, int]]:
    """Byte spans of the parameter `old` in the body, to become `new`.

    Refuses when the name is used inside a nested scope or re-bound by a
    statement, since the walk cannot tell those uses from the parameter's,
    and when `new` is already read in the body, since the parameter would
    then shadow it. A name that is itself being renamed away does not count
    as taken: swapping two parameters is a rename in each direction.
    """
    body = header.function.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    wanted = old.encode(cs.ENCODING_UTF8)
    taken = None if new in renamed_away else new.encode(cs.ENCODING_UTF8)
    rebinds = cs.SIGNATURE_BODY_REBINDS.format(old=old, qn=header.qn, new=new)
    shadows = cs.SIGNATURE_BODY_NAME_TAKEN.format(old=old, qn=header.qn, new=new)
    spans: list[tuple[int, int]] = []
    stack: list[Node] = [body]
    while stack:
        node = stack.pop()
        if node.type in _OPAQUE_TO_THE_WALK:
            _refuse_mentions(node, wanted, taken, rebinds, shadows)
            continue
        if node.type in _COMPREHENSIONS and _comprehension_binds(node, wanted):
            raise SignatureRefused(rebinds)
        if node.type == cs.TS_PY_IDENTIFIER and _is_a_reference(node):
            if node.text == wanted:
                spans.append((node.start_byte, node.end_byte))
            elif node.text == taken:
                raise SignatureRefused(shadows)
        stack.extend(node.children)
    return spans


def _refuse_mentions(
    node: Node, wanted: bytes, taken: bytes | None, rebinds: str, shadows: str
) -> None:
    """A nested scope or re-binding statement may mention neither name.

    The walk cannot tell a use of the old name there from the parameter's;
    and a nested scope reading the new name would start reading the renamed
    parameter instead of whatever it read before.
    """
    if _mentions(node, wanted):
        raise SignatureRefused(rebinds)
    if taken is not None and _mentions(node, taken):
        raise SignatureRefused(shadows)


# --- call sites -----------------------------------------------------------------


def _bind_arguments(
    args: Node, source: bytes, old: Sequence[ParamSpec]
) -> dict[int, _Binding]:
    """Old parameter index -> the value this site passes for it."""
    names = [p.name for p in old]
    children = args.named_children
    positional_kinds = {
        cs.TS_PY_KEYWORD_ARGUMENT,
        cs.TS_PY_LIST_SPLAT,
        cs.TS_PY_DICTIONARY_SPLAT,
        cs.TS_COMMENT,
    }
    given = sum(1 for child in children if child.type not in positional_kinds)
    if given > len(old):
        raise _Unmapped(
            cs.SIGNATURE_SITE_TOO_MANY.format(given=given, declared=len(old))
        )
    bound: dict[int, _Binding] = {}
    positional = 0
    for position, child in enumerate(children):
        text = _text(child, source)
        if child.type in (
            cs.TS_PY_LIST_SPLAT,
            cs.TS_PY_DICTIONARY_SPLAT,
            cs.TS_COMMENT,
        ):
            raise _Unmapped(cs.SIGNATURE_SITE_UNREADABLE.format(text=text))
        if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
            key = child.child_by_field_name(cs.FIELD_NAME)
            value = child.child_by_field_name(cs.FIELD_VALUE)
            assert key is not None and value is not None
            name = _text(key, source)
            if name not in names:
                raise _Unmapped(cs.SIGNATURE_SITE_UNKNOWN_KEYWORD.format(name=name))
            index = names.index(name)
            if index in bound:
                raise _Unmapped(cs.SIGNATURE_SITE_DUPLICATE.format(name=name))
            bound[index] = _Binding(
                _text(value, source), True, position, (value.start_byte, value.end_byte)
            )
            continue
        bound[positional] = _Binding(
            text, False, position, (child.start_byte, child.end_byte)
        )
        positional += 1
    return bound


def _fold(
    candidate: _Candidate, edits: Sequence[_Edit]
) -> tuple[dict[int, _Binding], list[_Edit]]:
    """The site's bindings with the planned edits inside them applied.

    A recursive call carries the body rename in its arguments; a call nested
    in another call's arguments carries the inner site's rewrite. Folding
    those into the value text lets the enclosing site be one edit, and the
    folded edits are returned so the caller can drop them from the plan.
    Any other edit overlapping the argument list cannot be folded, and the
    site is left unmapped.
    """
    path = candidate.site.path
    start, end = candidate.span
    inside = [
        edit
        for edit in edits
        if edit.path == path and edit.span[0] < end and start < edit.span[1]
    ]
    folded: dict[int, _Binding] = {}
    consumed: list[_Edit] = []
    for index, binding in candidate.bound.items():
        lo, hi = binding.span
        nested = [e for e in inside if lo <= e.span[0] and e.span[1] <= hi]
        if not nested:
            folded[index] = binding
            continue
        raw = bytearray(binding.text.encode(cs.ENCODING_UTF8))
        for edit in sorted(nested, key=lambda e: e.span[0], reverse=True):
            raw[edit.span[0] - lo : edit.span[1] - lo] = edit.text.encode(
                cs.ENCODING_UTF8
            )
        folded[index] = binding._replace(text=raw.decode(cs.ENCODING_UTF8))
        consumed.extend(nested)
    if len(consumed) != len(inside):
        raise _Unmapped(cs.SIGNATURE_SITE_OVERLAPS)
    return folded, consumed


def _render_arguments(
    new: Sequence[ParamSpec],
    sources: Sequence[_Source | None],
    bound: Mapping[int, _Binding],
) -> str:
    """The site's new argument list, parenthesised.

    Positional values keep positional form in the new order until a value
    goes by keyword or a defaulted parameter is left out; from there every
    later value is spelled by keyword, since it can no longer sit in its
    slot. Values that were keywords keep their original relative order
    under their new names; values forced to keyword form follow in the new
    parameter order.
    """
    positional: list[str] = []
    keywords: list[tuple[tuple[int, int], str]] = []
    forced = False
    for order, (spec, source) in enumerate(zip(new, sources, strict=True)):
        found = _value_at_site(spec, source, bound)
        if found is None:
            forced = True
            continue
        value, binding = found
        if binding is not None and binding.keyword:
            forced = True
            keywords.append(((0, binding.position), f"{spec.name}={value}"))
        elif forced:
            keywords.append(((1, order), f"{spec.name}={value}"))
        else:
            positional.append(value)
    keywords.sort(key=lambda item: item[0])
    parts = positional + [text for _rank, text in keywords]
    return f"({cs.SEPARATOR_COMMA_SPACE.join(parts)})"


def _value_at_site(
    spec: ParamSpec, source: _Source | None, bound: Mapping[int, _Binding]
) -> tuple[str, _Binding | None] | None:
    """The value this site passes for `spec`, with the binding it came from.

    None when the parameter's default stands in; unmapped when there is
    neither a value nor a default.
    """
    if source is not None and source.literal is not None:
        return source.literal, None
    if source is not None and source.index in bound:
        binding = bound[source.index]
        return binding.text, binding
    if spec.has_default:
        return None
    raise _Unmapped(cs.SIGNATURE_SITE_NO_VALUE.format(name=spec.name))


def _render_site(
    candidate: _Candidate,
    new: Sequence[ParamSpec],
    sources: Sequence[_Source | None],
    edits: list[_Edit],
) -> _Edit | None:
    """The site's edit, with any edit planned inside its arguments folded in.

    The folded edits leave the plan only once the site renders, so a site
    the mapping cannot complete keeps the inner rewrites as they were.
    """
    bound, consumed = _fold(candidate, edits)
    text = _render_arguments(new, sources, bound)
    for edit in consumed:
        edits.remove(edit)
    if not consumed and text == candidate.text:
        return None
    return _Edit(candidate.site.path, candidate.span, text)


def _check_hierarchy(
    qn: str, headers: Sequence[_Header], old_names: Sequence[str]
) -> None:
    """Every override declares the same parameter names as the definition."""
    for header in headers[1:]:
        theirs = [p.name for p in header.params]
        if theirs != list(old_names):
            raise SignatureRefused(
                cs.SIGNATURE_HIERARCHY_MISMATCH.format(
                    qn=qn,
                    member=header.qn,
                    theirs=cs.SEPARATOR_COMMA_SPACE.join(theirs),
                    ours=cs.SEPARATOR_COMMA_SPACE.join(old_names),
                )
            )


def _definition_edits(
    header: _Header,
    new: Sequence[ParamSpec],
    carried: set[str],
    renamed: Sequence[tuple[str, str]],
) -> list[_Edit]:
    """The header's new parameter list, and every body reference renamed."""
    own = {p.name: p for p in header.params}
    texts = [own[spec.name].text if spec.name in carried else spec.text for spec in new]
    if header.receiver is not None:
        texts.insert(0, header.receiver)
    edits = [
        _Edit(header.path, header.span, f"({cs.SEPARATOR_COMMA_SPACE.join(texts)})")
    ]
    renamed_away = {old for old, _new in renamed}
    for old_name, new_name in renamed:
        # A default is evaluated at DEFINITION time, in the enclosing scope,
        # so `def f(a, b=a)` renamed a->x writes `def f(x, b=a)` and raises
        # NameError the moment the module loads. `_body_references` walks the
        # body only, so it cannot see the reference (Copilot, PR #1533).
        _refuse_default_reading(header, texts, old_name, new_name)
        edits.extend(
            _Edit(header.path, span, new_name)
            for span in _body_references(header, old_name, new_name, renamed_away)
        )
    return edits


def _refuse_default_reading(
    header: _Header, texts: Sequence[str], old: str, new: str
) -> None:
    """Refuse when a parameter's default reads the name being renamed."""
    pattern = re.compile(rf"\b{re.escape(old)}\b")
    for text in texts:
        name, sep, default = text.partition(_LITERAL_PREFIX)
        if not sep or not pattern.search(default):
            continue
        raise SignatureRefused(
            cs.SIGNATURE_DEFAULT_REFERENCES_RENAMED.format(
                old=old, qn=header.qn, new=new, param=name.strip()
            )
        )


def _params_of(params: Node, source: bytes, qn: str) -> list[ParamSpec]:
    """Every parameter as a plain spec, or a refusal naming the odd one."""
    specs: list[ParamSpec] = []
    for child in params.named_children:
        spec = _param_spec(child, source)
        if spec is None:
            raise SignatureRefused(
                cs.SIGNATURE_UNSUPPORTED_PARAMS.format(qn=qn, text=_text(child, source))
            )
        specs.append(spec)
    return specs


def _take_receiver(
    qn: str, label: object, specs: list[ParamSpec], node: Node, source: bytes
) -> str | None:
    """Pop a method's `self`/`cls` off `specs`; a static method has none.

    A method whose first parameter is named anything else refuses: `this`
    would be remapped as a parameter and every bound call would lose its
    receiver.

    Staticness is decided BEFORE the name: `@staticmethod def f(self, x)`
    is legal Python and its `self` is an ordinary parameter, so popping it
    by name dropped a real one from the header and every call site's
    binding shifted by one (Copilot, PR #1912).
    """
    if label != cs.NodeLabel.METHOD or not specs:
        return None
    if _is_static(node, source):
        return None
    if specs[0].name in cs.PY_RECEIVER_NAMES:
        return specs.pop(0).text
    raise SignatureRefused(
        cs.SIGNATURE_UNUSUAL_RECEIVER.format(qn=qn, name=specs[0].name)
    )


# --- the operation ---------------------------------------------------------------


class SignatureChanger:
    """Plan and apply one signature change against a project's graph."""

    def __init__(
        self,
        repo_root: Path,
        fetch_all: QueryFn,
        project_name: str,
        reingest: Reingest | None = None,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.fetch_all = fetch_all
        self.project = project_name
        # With a re-ingest the change is held to its postcondition contract
        # (issue #1531): the delta of what it wrote is measured and the
        # transaction undone when the contract fails.
        self.reingest = reingest
        parsers, _queries = load_parsers()
        self._parsers = dict(parsers)
        self._parser: Parser = self._parsers[cs.SupportedLanguage.PYTHON]

    # -- planning --

    def plan(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> tuple[SignatureReport, list[_Edit]]:
        """Everything the change touches, and the edits that do it."""
        patcher = Patcher(self.repo_root, parsers=self._parsers)
        members = hierarchy(self.fetch_all, self.project, qn)
        headers = [self._header(member, patcher) for member in members]
        old = headers[0].params
        old_names = [p.name for p in old]
        _check_hierarchy(qn, headers, old_names)
        new = _new_specs(new_params, old)
        sources = _resolve_sources(new, old, mapping or {})
        for spec, source in zip(new, sources, strict=True):
            if source is not None and source.literal is not None:
                _check_literal(spec, source.literal)
        # A bare old name carries that parameter's own spelling over (each
        # override keeps its own); spelling a kept parameter out again
        # re-annotates it, so only an unchanged spec counts as carried.
        by_name = {spec.name: spec for spec in old}
        carried = {spec.name for spec in new if by_name.get(spec.name) == spec}
        # An old parameter fed to a new one of another name is renamed, and
        # the body must follow or the definition would be broken.
        renamed = [
            (old_names[source.index], spec.name)
            for spec, source in zip(new, sources, strict=True)
            if source is not None
            and source.index is not None
            and old_names[source.index] != spec.name
        ]
        sites: list[SignatureSite] = []
        edits: list[_Edit] = []
        for header in headers:
            edits.extend(_definition_edits(header, new, carried, renamed))
            sites.append(
                SignatureSite(
                    _DEFINITION,
                    header.path,
                    header.line,
                    header.col,
                    header.qn,
                    cs.EdgeResolution.EXACT,
                )
            )
        unmapped = self._collect_sites(
            headers, old, new, sources, allow_heuristic, patcher, sites, edits
        )
        report = SignatureReport(
            qualified_name=qn,
            old_params=tuple(old_names),
            new_params=tuple(spec.name for spec in new),
            applied=False,
            transaction_id="",
            files=(),
            sites=tuple(sites),
            unmapped=tuple(unmapped),
            hierarchy=tuple(members),
            diff="",
            message=cs.SIGNATURE_PLANNED.format(
                count=sum(1 for s in sites if s.kind == _CALL), skipped=len(unmapped)
            ),
        )
        return report, edits

    def _header(self, qn: str, patcher: Patcher) -> _Header:
        definition = graph_query.definition(self.fetch_all, self.project, qn, None)
        path = definition["path"]
        if not definition["found"] or not path:
            raise SignatureRefused(cs.RENAME_UNKNOWN.format(qn=qn))
        if get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.PYTHON:
            raise SignatureRefused(
                cs.SIGNATURE_NOT_PYTHON.format(qn=qn, path=path, issue=LANGUAGES_ISSUE)
            )
        try:
            source = patcher.source(path)
        except PatcherError as error:
            raise SignatureRefused(
                cs.SIGNATURE_DEFINITION_UNREADABLE.format(qn=qn, path=path, error=error)
            ) from error
        start = definition["start_line"] or 1
        end = definition["end_line"] or start
        name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        root = self._parser.parse(source).root_node
        node = _find_definition(root, name, start, end)
        params = node.child_by_field_name(cs.FIELD_PARAMETERS) if node else None
        if node is None or params is None:
            raise SignatureRefused(cs.SIGNATURE_NO_HEADER.format(qn=qn, path=path))
        specs = _params_of(params, source, qn)
        receiver = _take_receiver(qn, definition["label"], specs, node, source)
        return _Header(
            qn,
            path,
            (params.start_byte, params.end_byte),
            params.start_point[0] + 1,
            params.start_point[1],
            receiver,
            specs,
            node,
            source,
        )

    def _collect_sites(
        self,
        headers: Sequence[_Header],
        old: Sequence[ParamSpec],
        new: Sequence[ParamSpec],
        sources: Sequence[_Source | None],
        allow_heuristic: bool,
        patcher: Patcher,
        sites: list[SignatureSite],
        edits: list[_Edit],
    ) -> list[UnmappedSite]:
        unmapped: list[UnmappedSite] = []
        candidates = self._candidates(headers, old, allow_heuristic, patcher, unmapped)
        # Innermost first, so a site nested in another's arguments (or a
        # body rename inside a recursive call) is already planned when the
        # enclosing site folds it into its value text.
        order = sorted(
            range(len(candidates)),
            key=lambda i: (
                candidates[i].site.path,
                candidates[i].span[1] - candidates[i].span[0],
                candidates[i].span[0],
            ),
        )
        mapped: set[int] = set()
        for index in order:
            candidate = candidates[index]
            try:
                edit = _render_site(candidate, new, sources, edits)
            except _Unmapped as skip:
                unmapped.append(
                    UnmappedSite(
                        candidate.site.owner,
                        candidate.site.path,
                        candidate.site.line,
                        candidate.site.col,
                        str(skip),
                    )
                )
                continue
            mapped.add(index)
            # Into the plan at once: an enclosing site folds it in.
            if edit is not None:
                edits.append(edit)
        # The report lists sites in the order the graph gave them.
        sites.extend(c.site for i, c in enumerate(candidates) if i in mapped)
        return unmapped

    def _candidates(
        self,
        headers: Sequence[_Header],
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
        unmapped: list[UnmappedSite],
    ) -> list[_Candidate]:
        """Every graph-known call site the mapping can read, once each."""
        candidates: list[_Candidate] = []
        seen: set[tuple[object, ...]] = set()
        for header in headers:
            for row in graph_query.callers(self.fetch_all, self.project, header.qn):
                # Chained calls share a start and differ in their end, so
                # the whole span identifies a site; sites without a
                # location are each listed, since nothing tells them apart.
                key = (
                    row["path"],
                    row["line"],
                    row["col"],
                    row["end_line"],
                    row["end_col"],
                )
                if None in key[:3] or key not in seen:
                    seen.add(key)
                    self._consider(
                        row, old, allow_heuristic, patcher, candidates, unmapped
                    )
        return candidates

    def _consider(
        self,
        row: graph_query.CallSiteRow,
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
        candidates: list[_Candidate],
        unmapped: list[UnmappedSite],
    ) -> None:
        try:
            candidates.append(self._candidate(row, old, allow_heuristic, patcher))
        except _Unmapped as skip:
            unmapped.append(
                UnmappedSite(
                    row["qualified_name"],
                    row["path"] or "",
                    row["line"],
                    row["col"],
                    str(skip),
                )
            )

    def _candidate(
        self,
        row: graph_query.CallSiteRow,
        old: Sequence[ParamSpec],
        allow_heuristic: bool,
        patcher: Patcher,
    ) -> _Candidate:
        path, line, col = row["path"], row["line"], row["col"]
        resolution = row["resolution"]
        if path is None or line is None or col is None:
            raise _Unmapped(
                cs.SIGNATURE_SITE_NO_LOCATION.format(
                    resolution=resolution or cs.EdgeResolution.DYNAMIC.value
                )
            )
        if resolution in AMBIGUOUS and not allow_heuristic:
            raise _Unmapped(cs.SIGNATURE_SITE_GUESSED.format(resolution=resolution))
        if get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.PYTHON:
            raise _Unmapped(cs.SIGNATURE_SITE_NOT_PYTHON.format(path=path))
        try:
            source = patcher.source(path)
        except PatcherError as error:
            raise _Unmapped(
                cs.SIGNATURE_SITE_UNREADABLE_FILE.format(error=error)
            ) from error
        end_line, end_col = row["end_line"], row["end_col"]
        recorded_end = (
            (end_line - 1, end_col)
            if end_line is not None and end_col is not None
            else None
        )
        root = self._parser.parse(source).root_node
        call = call_node_at(root, line, col, recorded_end)
        if call is None:
            raise _Unmapped(cs.SIGNATURE_SITE_NO_CALL)
        args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args is None or args.type != cs.TS_ARGUMENT_LIST:
            # `helper(x for x in xs)`: a generator, not a list of values.
            raise _Unmapped(
                cs.SIGNATURE_SITE_UNREADABLE.format(text=_text(args or call, source))
            )
        bound = _bind_arguments(args, source, old)
        site = SignatureSite(_CALL, path, line, col, row["qualified_name"], resolution)
        return _Candidate(
            site, (args.start_byte, args.end_byte), _text(args, source), bound
        )

    # -- staging and applying --

    def _stage(
        self, edits: Iterable[_Edit]
    ) -> tuple[EditTransaction, dict[str, object], list[str]]:
        """Patch every edit into a transaction; nothing touches the tree."""
        patcher = Patcher(self.repo_root, parsers=self._parsers)
        tx = EditTransaction(self.repo_root)
        try:
            for edit in edits:
                patcher.replace_span(edit.path, edit.span, edit.text)
            results = patcher.stage_into(tx)
        except PatcherError as error:
            # A plan the patcher cannot apply (an overlap the folding did
            # not foresee) is a refusal, never a traceback past the tool.
            tx.rollback()
            raise SignatureRefused(
                cs.SIGNATURE_STAGE_FAILED.format(error=error)
            ) from error
        broken = [key for key, result in results.items() if result.parses is False]
        return tx, dict(results), broken

    def preview(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> SignatureReport:
        """Plan and stage, return the diff, and leave the tree untouched."""
        report, edits = self.plan(qn, new_params, mapping, allow_heuristic)
        tx, results, broken = self._stage(edits)
        try:
            diff = tx.diff()
        finally:
            tx.rollback()
        message = (
            cs.SIGNATURE_PARSE_FAILED.format(
                files=cs.SEPARATOR_COMMA_SPACE.join(broken)
            )
            if broken
            else report.message
        )
        return report._replace(files=tuple(sorted(results)), diff=diff, message=message)

    def apply(
        self,
        qn: str,
        new_params: Sequence[str],
        mapping: Mapping[str, str] | None,
        allow_heuristic: bool,
    ) -> SignatureReport:
        """Plan, patch, verify and commit; the tree is untouched on failure."""
        report, edits = self.plan(qn, new_params, mapping, allow_heuristic)
        tx, results, broken = self._stage(edits)
        if broken:
            tx.rollback()
            return report._replace(
                files=tuple(sorted(results)),
                message=cs.SIGNATURE_PARSE_FAILED.format(
                    files=cs.SEPARATOR_COMMA_SPACE.join(broken)
                ),
            )
        outcome = tx.commit()
        report = report._replace(
            applied=outcome.applied,
            transaction_id=outcome.transaction_id,
            files=outcome.files,
            diff=outcome.diff,
            message=outcome.message,
        )
        if outcome.applied and self.reingest is not None:
            report = self._enforce_contract(report, allow_heuristic)
        return report

    def _enforce_contract(
        self, report: SignatureReport, allow_heuristic: bool
    ) -> SignatureReport:
        assert self.reingest is not None
        try:
            delta = measure(
                self.fetch_all,
                self.project,
                self.repo_root,
                report.files,
                self.reingest,
            )
        # The transaction has landed; a graph that cannot be measured is
        # reported, never raised past the committed edit.
        except Exception as error:  # noqa: BLE001
            logger.warning(cs.SIGNATURE_CONTRACT_UNMEASURED.format(error=error))
            return report._replace(
                verdict=None,
                # `reingest` rejects bad paths with `ValueError` and converts
                # every prologue failure to `ReingestAborted` before writing,
                # so those two mean the graph was never touched (the rule
                # `rename` and the guarded MCP callback apply).
                graph_incomplete=not isinstance(error, ValueError | ReingestAborted),
                message=cs.SIGNATURE_CONTRACT_UNMEASURED.format(error=error),
            )
        verdict = verify(
            change_signature_expectation(
                [
                    _site_key(site.path, site.line, site.col)
                    for site in report.unmapped
                    if site.line is not None
                ],
                heuristic_allowed=allow_heuristic,
            ),
            delta,
            rewritten=[
                (_site_key(site.path, site.line, site.col), site.resolution)
                for site in report.sites
                if site.kind == _CALL
            ],
        )
        if verdict.ok:
            return report._replace(verdict=verdict)
        reasons = cs.SEPARATOR_SEMICOLON_SPACE.join(verdict.failures)
        try:
            # This change's own transaction, not whatever is newest: a later
            # edit stacked on it refuses the rollback instead.
            undo_transaction(self.repo_root, report.transaction_id)
        except TransactionConflict as conflict:
            logger.warning(str(conflict))
            return report._replace(
                verdict=verdict,
                message=cs.SIGNATURE_ROLLBACK_REFUSED.format(reasons=reasons),
            )
        try:
            self.reingest(list(report.files))
        # The files are restored; the graph may have lost the subtree the
        # re-ingest deleted before failing. Say so, never raise.
        except Exception as error:  # noqa: BLE001
            logger.warning(
                cs.SIGNATURE_ROLLBACK_UNMEASURED.format(reasons=reasons, error=error)
            )
            return report._replace(
                applied=False,
                verdict=verdict,
                graph_incomplete=True,
                message=cs.SIGNATURE_ROLLBACK_UNMEASURED.format(
                    reasons=reasons, error=error
                ),
            )
        return report._replace(
            applied=False,
            verdict=verdict,
            message=cs.SIGNATURE_CONTRACT_FAILED.format(reasons=reasons),
        )


def _site_key(path: str, line: int | None, col: int | None) -> str:
    """The identity the contract checks a call under: two calls can share a line."""
    return cs.CHAR_COLON.join((path, str(line), str(col)))


def change_signature(
    repo_root: Path,
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    new_params: Sequence[str],
    mapping: Mapping[str, str] | None = None,
    allow_heuristic: bool = False,
    dry_run: bool = False,
    reingest: Reingest | None = None,
) -> SignatureReport:
    """The op: plan (and refuse what cannot be stated) or plan and apply.

    With `reingest` the applied change is measured through the structural
    delta and undone when its postcondition contract fails (issue #1531).
    """
    changer = SignatureChanger(repo_root, fetch_all, project_name, reingest=reingest)
    if dry_run:
        return changer.preview(qualified_name, new_params, mapping, allow_heuristic)
    return changer.apply(qualified_name, new_params, mapping, allow_heuristic)


def sites_for(sites: Iterable[SignatureSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]


def unmapped_for(sites: Iterable[UnmappedSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
