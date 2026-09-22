"""Parameter specs, mapping sources and the result types of `change_signature`
(issue #1533). Split from `signature.py`; no tree-sitter walking here.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .contract import Verdict

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


def sites_for(sites: Iterable[SignatureSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]


def unmapped_for(sites: Iterable[UnmappedSite]) -> list[dict[str, object]]:
    return [dict(site._asdict()) for site in sites]
