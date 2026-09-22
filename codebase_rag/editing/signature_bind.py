"""Reading a Python header with tree-sitter, binding call arguments to the
new parameter list and rendering the rewritten text, for `change_signature`
(issue #1533). Split from `signature.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from tree_sitter import Node

from .. import constants as cs
from ..parsers.utils import _is_static_decorator
from .signature_spec import (
    _LITERAL_PREFIX,
    ParamSpec,
    SignatureRefused,
    _Binding,
    _Candidate,
    _Edit,
    _Header,
    _Source,
    _Unmapped,
)


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


def _body_reads(header: _Header, name: str) -> bool:
    """Whether the body READS `name`.

    Through `_is_a_reference`, so `obj.a` and `helper(a=1)` do not count:
    an attribute's name and a keyword argument's label are identifiers
    spelled the same as the parameter but are not uses of it, and
    refusing on those blocked legitimate removals (CodeRabbit, PR #1533).
    """
    body = header.function.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return False
    wanted = name.encode(cs.ENCODING_UTF8)
    stack = [body]
    while stack:
        node = stack.pop()
        if (
            node.type == cs.TS_PY_IDENTIFIER
            and node.text == wanted
            and _is_a_reference(node)
        ):
            return True
        stack.extend(node.children)
    return False


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
