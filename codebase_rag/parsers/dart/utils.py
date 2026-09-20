from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import decode_node_text


def dart_get_name(node: Node) -> str | None:
    # The single source of truth for Dart declaration names; language_spec's
    # DART_FQN_SPEC delegates here. Most Dart declarations expose a `name`
    # field (functions, getters, setters, classes, enums, extensions).
    # Constructors/factories and mixins do not: their LAST bare `identifier`
    # child is the declared name (`C.named` -> `named`, `mixin Swimmer` ->
    # `Swimmer`, a default constructor `C(...)` -> `C`). The constructor check
    # comes FIRST: the grammar's `name` field on constructor_signature is the
    # CLASS identifier, which would collapse every named constructor into a
    # duplicate of the default one.
    if node.type in cs.DART_CONSTRUCTOR_SIGNATURE_TYPES:
        ids = [c for c in node.named_children if c.type == cs.TS_IDENTIFIER and c.text]
        if ids:
            return decode_node_text(ids[-1].text)
        return None
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text:
        return decode_node_text(name_node.text)
    ids = [c for c in node.named_children if c.type == cs.TS_IDENTIFIER and c.text]
    if ids:
        return decode_node_text(ids[-1].text)
    return None


def dart_definition_end_point(node: Node) -> tuple[int, int]:
    """End point of a captured Dart function/method, including its body.

    The grammar splits a definition into a `*_signature` node and a sibling
    `function_body`, so the signature's own end excludes the body. A signature
    under a `method_signature`/`declaration` wrapper takes the wrapper's
    following `function_body` sibling; a top-level signature takes its own.
    Any non-signature node returns its end point unchanged.
    """
    if node.type not in cs.DART_SIGNATURE_TYPES:
        return node.end_point
    base = node
    if node.parent is not None and node.parent.type in cs.DART_SIGNATURE_WRAPPERS:
        base = node.parent
    following = base.next_named_sibling
    if following is not None and following.type == cs.TS_DART_FUNCTION_BODY:
        return following.end_point
    return base.end_point


def dart_body_node(node: Node) -> Node | None:
    """The sibling `function_body` completing a captured signature, or None."""
    if node.type not in cs.DART_SIGNATURE_TYPES:
        return None
    base = node
    if node.parent is not None and node.parent.type in cs.DART_SIGNATURE_WRAPPERS:
        base = node.parent
    following = base.next_named_sibling
    if following is not None and following.type == cs.TS_DART_FUNCTION_BODY:
        return following
    return None


def dart_definition_end_byte(node: Node) -> int:
    """End byte of a captured Dart definition, including its sibling body."""
    body = dart_body_node(node)
    if body is not None:
        return body.end_byte
    return node.end_byte


def _selector_member_name(selector: Node) -> str | None:
    # `.m` / `?.m` -> "m"; an index selector (`[i]`) or a nested
    # argument_part has no static member name.
    for child in selector.named_children:
        if child.type in (
            cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CONDITIONAL_ASSIGNABLE_SELECTOR,
        ):
            for inner in child.named_children:
                if inner.type == cs.TS_IDENTIFIER and inner.text:
                    return decode_node_text(inner.text)
            return None
    return None


def _first_identifier_text(node: Node) -> str | None:
    for inner in node.named_children:
        if inner.type == cs.TS_IDENTIFIER and inner.text:
            return decode_node_text(inner.text)
    return None


_CALL_HOP = "()"


def _selector_has_argument_part(node: Node) -> bool:
    return node.type == cs.TS_DART_SELECTOR and any(
        child.type == cs.TS_DART_ARGUMENT_PART for child in node.named_children
    )


def _relational_operator_text(node: Node) -> str | None:
    # The relational operator joining a relational_expression's operands.
    for child in node.children:
        if child.type == cs.TS_DART_RELATIONAL_OPERATOR and child.text:
            return decode_node_text(child.text)
    return None


def _construction_class_name(node: Node) -> str | None:
    # The class a construction-expression receiver builds, for the two shapes
    # that are NOT an identifier + argument_part selector (issue #2015):
    #
    #   `new X(1).m`   -> new_expression, and `const X(1).m` ->
    #      const_object_expression: distinct node types, both carrying the
    #      class as their first type_identifier (a `type_arguments` child
    #      may follow).
    #   `X<int>(1).m`                 -> relational_expression: the grammar
    #      reads `<`/`>` as comparisons, so the class is the identifier
    #      leading the `X < int` left operand, and the call's parens are a
    #      sibling parenthesized_expression.
    #
    # Returning the bare class name lets the caller emit it as a normal
    # `C` + `()` chain, so the receiver types through the same resolver path
    # the bare `X(1).m` form already uses.
    if node.type in (
        cs.TS_DART_NEW_EXPRESSION,
        cs.TS_DART_CONST_OBJECT_EXPRESSION,
    ):
        return _explicit_construction_name(node)
    return _mis_parsed_generic_name(node)


def _construction_name_parts(node: Node) -> tuple[list[str], str | None]:
    """The leading `type_identifier`s of a construction expression, and the
    trailing `identifier` if one follows them."""
    type_names: list[str] = []
    trailing: str | None = None
    for child in node.named_children:
        if not child.text:
            continue
        if child.type == cs.TS_DART_TYPE_IDENTIFIER:
            type_names.append(decode_node_text(child.text))
        elif child.type == cs.TS_DART_IDENTIFIER:
            trailing = decode_node_text(child.text)
            break
        elif type_names:
            break
    return type_names, trailing


def _explicit_construction_name(node: Node) -> str | None:
    """`new X(1)` / `const X(1)`: the class, keeping an import prefix and
    dropping a named constructor."""
    # Node type alone cannot split these, because the trailing
    # `identifier` means different things:
    #   `new X(1)`           -> type_identifier(X)
    #   `new p.X(1)`         -> type_identifier(p) + identifier(X)
    #   `new X.named(1)`     -> type_identifier(X) + identifier(named)
    #   `new p.X.named(1)`   -> type_identifier(p, X) + identifier(named)
    # The rule is POSITIONAL: every type_identifier belongs to the type
    # (an import prefix is kept so the resolver can fold it against the
    # import map, issue #2033), and a trailing identifier is the CLASS
    # only when no type_identifier has named it yet -- otherwise it is a
    # named constructor, which is not part of the receiver's type and
    # made the read resolve against `X.named` rather than `X`.
    # Keep every type_identifier, then the trailing identifier, and let
    # the CALLER drop a named constructor. The node types cannot settle
    # it here: after one type_identifier the trailing identifier is the
    # CLASS in `new p.X(1)` (prefix + class) and the CONSTRUCTOR in
    # `new X.named(1)` (class + constructor), identically shaped. The
    # resolver holds the import map and the registry, so it can tell a
    # prefix from a class; this function cannot (issue #2033).
    #
    # A dotted name must therefore resolve as a whole OR fall back to its
    # head: `X.named` is not a definition while `X` is, which is exactly
    # how the caller recognises a named constructor.
    type_names, trailing = _construction_name_parts(node)
    if not type_names:
        return trailing
    # Two type_identifiers already name prefix AND class (`new p.X.named`),
    # so a trailing identifier is the named constructor and is dropped.
    # With exactly one, the trailing identifier is the CLASS and the
    # type_identifier was the prefix (`new p.X(1)`); with none it is a
    # named constructor on a bare class (`new X.named(1)`), which main
    # dropped by returning the first type_identifier and which must keep
    # being dropped or the read resolves against `X.named`.
    if len(type_names) == 1 and trailing is not None:
        return f"{type_names[0]}{cs.SEPARATOR_DOT}{trailing}"
    return cs.SEPARATOR_DOT.join(type_names)


def _mis_parsed_generic_name(node: Node) -> str | None:
    """`X<int>(1)`: the grammar reads the angle brackets as comparisons, so
    the class leads the left operand of a `<` ... `>` relational pair."""
    if node.type != cs.TS_DART_RELATIONAL_EXPRESSION:
        return None
    # Match the mis-parsed-generic shape: exactly
    # [relational_expression, relational_operator, parenthesized_expression],
    # the parens standing in for the call's arguments. `(a < b).hashCode >
    # lo.height` carries extra selector/identifier children and is excluded.
    children = node.named_children
    if [child.type for child in children[:3]] != [
        cs.TS_DART_RELATIONAL_EXPRESSION,
        cs.TS_DART_RELATIONAL_OPERATOR,
        cs.TS_DART_PARENTHESIZED_EXPRESSION,
    ]:
        return None
    # The operators must read `<` then `>`, the order type arguments impose.
    # This rules out `a > b < (1).x`, but NOT `a < b > (1).x`, which the
    # grammar makes token-for-token identical to `X<int>(1).x`. That residual
    # ambiguity is left to the resolver: the name is only emitted as a
    # receiver, and a leading identifier that is not a known class resolves
    # to nothing, so a comparison yields no edge rather than a wrong one.
    if _relational_operator_text(children[0]) != cs.DART_ANGLE_OPEN:
        return None
    if _relational_operator_text(node) != cs.DART_ANGLE_CLOSE:
        return None
    # The left spine is `X < int`, or `p.X < int` under an import prefix, so
    # the class is the leading identifier plus any member selectors before
    # the operator (issue #2033). Any other shape (a parenthesized or call
    # left operand) is not a construction.
    spine = children[0].named_children
    if not spine or spine[0].type != cs.TS_DART_IDENTIFIER or not spine[0].text:
        return None
    names = [decode_node_text(spine[0].text)]
    for child in spine[1:]:
        if child.type != cs.TS_DART_SELECTOR:
            break
        if (member := _selector_member_name(child)) is None:
            return None
        names.append(member)
    return cs.SEPARATOR_DOT.join(names)


def _construction_receiver_class(
    node: Node, allow_ambiguous: bool = False
) -> str | None:
    # The class constructed by the receiver hop AT this chain position.
    # `new X(1).m` / `const X(1).m` reach the construction node itself and are
    # unambiguous. In the mis-parsed generic (`X<int>(1).m`) the chain instead
    # bottoms out at the call's `(1)`, whose PARENT relational_expression
    # carries the class; that shape is token-for-token identical to the
    # comparison `a < b > (1).m`, so it is only read as a construction when
    # the caller can reject a shadowing local (allow_ambiguous). The CALL path
    # cannot, and does not need it: `X<int>(1).m()` already binds through the
    # pre-existing argument_part hop.
    if node.type == cs.TS_DART_PARENTHESIZED_EXPRESSION:
        if not allow_ambiguous:
            return None
        parent = node.parent
        return _construction_class_name(parent) if parent is not None else None
    return _construction_class_name(node)


def _walk_chain(
    node: Node | None, allow_calls: bool = False, allow_ambiguous: bool = False
) -> list[str] | None:
    # Backward walk over a selector chain, shared by plain and cascade calls:
    # None means the chain is broken (index selector, arbitrary expression)
    # and has no static name; an empty list means it bottomed out at
    # `this`/`super`. With allow_calls, a call hop in the receiver
    # (`Base(args).m()`, `factory().m()`) contributes a `()` marker so the
    # resolver's chained path can type the receiver from the callee's return
    # type or constructor class; without it (cascade path) a call-result
    # receiver stays unresolvable.
    parts_rev: list[str] = []
    while node is not None:
        if allow_calls and _selector_has_argument_part(node):
            parts_rev.append(_CALL_HOP)
            node = node.prev_named_sibling
            continue
        if allow_calls and (
            class_name := _construction_receiver_class(node, allow_ambiguous)
        ):
            # A construction receiver is its own base: the class name plus a
            # call hop, with nothing further to walk behind it.
            parts_rev.append(_CALL_HOP)
            parts_rev.append(class_name)
            break
        part = _chain_part(node)
        if part is None:
            return None
        if part == _CHAIN_STOP:
            break
        parts_rev.append(part)
        if node.type == cs.TS_DART_IDENTIFIER:
            break
        node = node.prev_named_sibling
    return list(reversed(parts_rev))


def _assemble_chain(tokens: list[str]) -> str:
    # A `()` marker attaches to the preceding hop with no separator
    # (`Base` + `()` -> `Base()`); every other hop is dot-joined.
    out = ""
    for token in tokens:
        if token == _CALL_HOP:
            out += _CALL_HOP
        elif out:
            out += cs.SEPARATOR_DOT + token
        else:
            out = token
    return out


def _cascade_call_name(call_node: Node) -> str | None:
    # `obj..m()` holds the argument_part inside the cascade_section; every
    # section shares the ONE base receiver, so skip earlier sibling sections,
    # then walk the receiver chain exactly like a plain call; an
    # `obj.field..m()` cascade must keep its full receiver, or the bare member
    # name could bind an unrelated same-name function.
    parts = [
        name
        for child in call_node.named_children
        if child.type == cs.TS_DART_CASCADE_SELECTOR
        and (name := _first_identifier_text(child)) is not None
    ]
    if not parts:
        return None
    base = call_node.prev_named_sibling
    while base is not None and base.type == cs.TS_DART_CASCADE_SECTION:
        base = base.prev_named_sibling
    receiver = _walk_chain(base)
    if receiver is None:
        return None
    return cs.SEPARATOR_DOT.join(receiver + parts)


_CHAIN_STOP = ""


def _chain_part(node: Node) -> str | None:
    # One backward step over the selector chain: a member selector or the
    # base identifier contributes a name segment; `this`/`super` yield the
    # empty STOP marker (their member resolves against the caller's
    # class); anything else (index selector, a call result, an arbitrary
    # expression base) has no static name.
    match node.type:
        case cs.TS_DART_SELECTOR:
            return _selector_member_name(node)
        case cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR:
            # a super call attaches the member selector directly, without
            # the `selector` wrapper
            return _first_identifier_text(node)
        case cs.TS_DART_IDENTIFIER:
            if node.text is None:
                return None
            return decode_node_text(node.text)
        case cs.TS_DART_THIS | cs.TS_DART_SUPER:
            return _CHAIN_STOP
        case _:
            return None


def dart_call_name(call_node: Node) -> str | None:
    """The dotted name a Dart invocation targets, reassembled from siblings.

    The grammar has no call-expression node: `f(x)` is `identifier` +
    `selector(argument_part)`, `a.b(x)` is `identifier` + `selector(.b)` +
    `selector(argument_part)`, and `obj..m()` holds the `argument_part`
    inside its `cascade_section`. Walk the preceding sibling chain and
    rebuild the target; a call hop in the receiver (`Base(args).m()`,
    `factory().m()`) is preserved as `()` so the resolver's chained path can
    type it, while a chain broken by an index (`xs[0].f()`) still has no
    static name and returns None. A `this`/`super` base is dropped so the
    bare member name resolves against the caller's class.
    """
    if call_node.type == cs.TS_DART_CASCADE_SECTION:
        return _cascade_call_name(call_node)
    tokens = _walk_chain(call_node.prev_named_sibling, allow_calls=True)
    if not tokens or all(token == _CALL_HOP for token in tokens):
        return None
    return _assemble_chain(tokens)


def dart_ambiguous_construction_base(selector_node: Node) -> str | None:
    """The identifier a read's receiver would construct, when ambiguous.

    `X<int>(1).m` and the chained comparison `a < b > (1).m` parse
    identically, so the generic-construction reading of a receiver is only a
    guess (issue #2015). Returns the name that reading would construct when
    THIS read took it, for the caller to reject when a local or parameter
    shadows it (then it is a comparison, not a construction). Returns None
    for every unambiguous shape, including `new X(1).m`.

    The name is DOTTED under an import prefix (`p.Box`, issue #2033), so a
    caller matching it against bare binders must take the leading segment.
    """
    receiver = selector_node.prev_named_sibling
    if receiver is None or receiver.type != cs.TS_DART_PARENTHESIZED_EXPRESSION:
        return None
    parent = receiver.parent
    if parent is None or parent.type != cs.TS_DART_RELATIONAL_EXPRESSION:
        return None
    return _construction_class_name(parent)


def dart_member_read_name(selector_node: Node) -> str | None:
    """The dotted name a member-selector READ targets, or None.

    `marker.startYr` is `identifier` + `selector(.startYr)`: reassemble the
    receiver chain plus the member. A selector followed by an argument_part
    selector is an invocation the call pass owns; a chain broken by an index
    or arbitrary expression has no static name. A `this` base is dropped so
    the bare member resolves against the caller's class.
    """
    following = selector_node.next_named_sibling
    if following is not None and _selector_has_argument_part(following):
        return None
    member = _selector_member_name(selector_node)
    if member is None:
        return None
    receiver = _walk_chain(
        selector_node.prev_named_sibling, allow_calls=True, allow_ambiguous=True
    )
    if receiver is None:
        return None
    return _assemble_chain([*receiver, member])


def dart_cascade_read_name(section: Node) -> str | None:
    """The dotted name a cascade member READ targets (`m..startYr`), or None.

    A pure read section holds ONLY cascade_selector children: an
    argument_part marks an invocation the call pass owns, and any other
    child (an assignment's RHS) marks a WRITE targeting the setter. The
    shared base receiver is reassembled exactly like a cascade call's.
    """
    if any(
        child.type != cs.TS_DART_CASCADE_SELECTOR for child in section.named_children
    ):
        return None
    parts = [
        name
        for child in section.named_children
        if (name := _first_identifier_text(child)) is not None
    ]
    if not parts:
        return None
    base = section.prev_named_sibling
    while base is not None and base.type == cs.TS_DART_CASCADE_SECTION:
        base = base.prev_named_sibling
    # allow_calls: a call-result cascade (`getMarker()..startYr`) keeps its
    # `()` hop so the resolver types the receiver from the callee's declared
    # return type, exactly like a plain chained read.
    receiver = _walk_chain(base, allow_calls=True)
    if receiver is None:
        return None
    return _assemble_chain([*receiver, *parts])


def dart_return_type_name(node: Node) -> str | None:
    """The declared return type of a Dart signature, or None.

    A constructor "returns" its class (its FIRST identifier); a method or
    function signature's leading type_identifier before the name is its
    return type; void, inferred, and getter-less shapes record nothing.
    """
    if node.type in cs.DART_CONSTRUCTOR_SIGNATURE_TYPES:
        for child in node.named_children:
            if child.type == cs.TS_DART_IDENTIFIER and child.text:
                return decode_node_text(child.text)
        return None
    for child in node.named_children:
        if child.type == cs.TS_DART_TYPE_IDENTIFIER and child.text:
            return decode_node_text(child.text)
        if child.type == cs.TS_DART_IDENTIFIER:
            return None
    return None


def dart_extract_uri(node: Node) -> str | None:
    """The unquoted URI of an import/export/part directive, or None."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_DART_URI and current.text:
            return decode_node_text(current.text).strip(cs.DART_QUOTE_CHARS)
        stack.extend(current.children)
    return None


def dart_local_name(uri: str) -> str:
    """A short local key for an import URI (its last path segment, no `.dart`)."""
    segment = uri.rstrip(cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)[-1]
    if segment.endswith(cs.DART_EXT):
        segment = segment[: -len(cs.DART_EXT)]
    return segment or uri


def _walk_named(node: Node) -> Iterator[Node]:
    # Depth-first over named descendants, the node itself first.
    yield node
    for child in node.named_children:
        yield from _walk_named(child)


def dart_import_prefix(import_node: Node) -> str | None:
    """The `as` prefix of a Dart import, or None when it has none.

    `import 'lib.dart' as p;` binds every name from that library under `p`,
    so a member reached through it (`p.Box`) resolves only if the PREFIX is
    registered as an import key; the file-derived key `lib` never appears in
    the source. The prefix is the `import_specification`'s own `identifier`
    child, which is where the grammar puts it for `as` and `deferred as`
    alike; a `show`/`hide` name is nested inside a `combinator` node instead,
    so scanning only DIRECT children cannot mistake one for a prefix
    (issue #2033).
    """
    # The capture is the whole `import_or_export`, so the specification sits
    # a level or two down (import_or_export > library_import >
    # import_specification); walk to it rather than assuming a direct child.
    spec = next(
        (
            node
            for node in _walk_named(import_node)
            if node.type == cs.TS_DART_IMPORT_SPECIFICATION
        ),
        None,
    )
    if spec is None:
        return None
    # The prefix is the specification's own identifier child. Any identifier
    # deeper down belongs to a `show`/`hide` combinator, not to `as`.
    for child in spec.named_children:
        if child.type == cs.TS_DART_IDENTIFIER and child.text:
            return decode_node_text(child.text)
    return None


def dart_resolve_import(uri: str, module_qn: str, project_name: str) -> str:
    """Full import target: external URIs kept verbatim, relative paths resolved.

    `dart:` and `package:` targets are external and returned unchanged. A
    relative path is resolved against the importing module's package to a
    project-internal module qn (`../utils/helper.dart` -> `project.lib.utils.helper`).
    """
    if uri.startswith(cs.DART_SCHEME_DART) or uri.startswith(cs.DART_SCHEME_PACKAGE):
        return uri
    parts = module_qn.split(cs.SEPARATOR_DOT)[:-1]
    for segment in uri.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH):
        if segment in ("", cs.PATH_CURRENT_DIR):
            continue
        if segment == cs.PATH_PARENT_DIR:
            if parts:
                parts.pop()
        else:
            parts.append(segment)
    if parts and parts[-1].endswith(cs.DART_EXT):
        parts[-1] = parts[-1][: -len(cs.DART_EXT)]
    return cs.SEPARATOR_DOT.join(parts)


# Node types that scope a Dart binding, and the subset whose binder is live
# only AFTER its own declaration (a for-in iterable and a try body precede
# their binder). Mirrors the call processor's shadow walk; kept here so the
# import processor can record spans without importing that module.
_SHADOW_SCOPE_TYPES = frozenset(
    {
        cs.TS_DART_BLOCK,
        cs.TS_DART_FUNCTION_EXPRESSION,
        cs.TS_DART_LOCAL_FUNCTION_DECLARATION,
        cs.TS_DART_FOR_STATEMENT,
        cs.TS_DART_TRY_STATEMENT,
    }
)
_STATEMENT_SCOPE_TYPES = frozenset({cs.TS_DART_FOR_STATEMENT, cs.TS_DART_TRY_STATEMENT})
_SIGNATURE_TYPES = frozenset(
    {
        cs.TS_DART_FUNCTION_SIGNATURE,
        cs.TS_DART_METHOD_SIGNATURE,
        cs.TS_DART_FORMAL_PARAMETER_LIST,
    }
)
_BODY_TYPES = frozenset({cs.TS_DART_FUNCTION_BODY, cs.TS_DART_BLOCK})
_LOCAL_DECLARATION_TYPES = frozenset(
    {
        cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION,
        cs.TS_DART_INITIALIZED_IDENTIFIER,
    }
)


def _following_body_span(signature: Node) -> tuple[int, int] | None:
    """The span of the body a signature introduces, if it has one.

    A `formal_parameter_list` has no sibling after it -- the signature ABOVE
    it is the node the body follows -- so a caller walks up and asks again
    rather than treating the first miss as "no scope".
    """
    body = signature.next_named_sibling
    while body is not None and body.type not in _BODY_TYPES:
        body = body.next_named_sibling
    return (body.start_byte, body.end_byte) if body is not None else None


def _binding_scope_span(decl: Node, root: Node) -> tuple[int, int] | None:
    """The byte span a binding shadows, or None when it scopes nothing here.

    A PARAMETER lives in the signature, which is a SIBLING of the body in
    this grammar, so walking up finds no block: its scope is the body that
    follows its signature. Falling through to the whole root instead would
    make one function's parameter shadow the entire file -- measured, it
    swallowed an unrelated read 45 bytes later.
    """
    anc = decl.parent
    while anc is not None and anc is not root:
        if anc.type in _SHADOW_SCOPE_TYPES:
            if anc.type in _STATEMENT_SCOPE_TYPES:
                return (decl.end_byte, anc.end_byte)
            return (anc.start_byte, anc.end_byte)
        if anc.type in _SIGNATURE_TYPES and (span := _following_body_span(anc)):
            return span
        anc = anc.parent
    return None


def _bound_names(node: Node) -> list[str]:
    """The name(s) a parameter or local declaration binds.

    Deliberately syntactic: `var p = 1` binds `p` whether or not any type can
    be inferred for it, which is the whole point -- an untyped local never
    reaches `local_var_types` (issue #2033).
    """
    if node.type in (cs.TS_DART_FORMAL_PARAMETER, cs.TS_DART_CATCH_PARAMETERS):
        return [
            name
            for child in node.named_children
            if child.type == cs.TS_DART_IDENTIFIER
            and child.text
            and (name := decode_node_text(child.text))
        ]
    if node.type in _LOCAL_DECLARATION_TYPES or node.type == cs.TS_DART_FOR_LOOP_PARTS:
        declared = next(
            (c for c in node.named_children if c.type == cs.TS_DART_IDENTIFIER),
            None,
        )
        if declared is not None and declared.text:
            return [decode_node_text(declared.text)]
    return []


def dart_binding_spans(
    root: Node, names: frozenset[str]
) -> dict[str, list[tuple[int, int]]]:
    """`{name: [byte span it is bound in]}` for the given names only.

    Used to tell a local or parameter that SHADOWS an import prefix from the
    prefix itself: inside one of these spans the bare name is that binding,
    so a chain rooted at it must not fold through the import map.
    """
    if not names:
        return {}
    spans: dict[str, list[tuple[int, int]]] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        for name in _bound_names(node):
            if name in names and (span := _binding_scope_span(node, root)):
                spans.setdefault(name, []).append(span)
        stack.extend(node.children)
    return spans
