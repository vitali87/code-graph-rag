from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs


def dart_get_name(node: Node) -> str | None:
    # (H) Mirror of language_spec._dart_get_name for the parsers-internal callers
    # (H) (ingest_method) that cannot import language_spec without a cycle. Most
    # (H) nodes expose a `name` field; constructors/factories/mixins take their
    # (H) LAST bare identifier child (`C.named` -> `named`, default `C(...)` -> `C`).
    # (H) The constructor check comes FIRST: the grammar's `name` field on
    # (H) constructor_signature is the CLASS identifier, which would collapse
    # (H) every named constructor into a duplicate of the default one.
    if node.type in cs.DART_CONSTRUCTOR_SIGNATURE_TYPES:
        ids = [c for c in node.named_children if c.type == cs.TS_IDENTIFIER and c.text]
        if ids:
            return ids[-1].text.decode(cs.ENCODING_UTF8)
        return None
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text:
        return name_node.text.decode(cs.ENCODING_UTF8)
    ids = [c for c in node.named_children if c.type == cs.TS_IDENTIFIER and c.text]
    if ids:
        return ids[-1].text.decode(cs.ENCODING_UTF8)
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
    # (H) `.m` / `?.m` -> "m"; an index selector (`[i]`) or a nested
    # (H) argument_part has no static member name.
    for child in selector.named_children:
        if child.type in (
            cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CONDITIONAL_ASSIGNABLE_SELECTOR,
        ):
            for inner in child.named_children:
                if inner.type == cs.TS_IDENTIFIER and inner.text:
                    return inner.text.decode(cs.ENCODING_UTF8)
            return None
    return None


def _first_identifier_text(node: Node) -> str | None:
    for inner in node.named_children:
        if inner.type == cs.TS_IDENTIFIER and inner.text:
            return inner.text.decode(cs.ENCODING_UTF8)
    return None


def _cascade_call_name(call_node: Node) -> str | None:
    # (H) `obj..m()` holds the argument_part inside the cascade_section; every
    # (H) section shares the ONE base receiver, so skip earlier sibling
    # (H) sections to reach it.
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
    if (
        base is not None
        and base.type == cs.TS_IDENTIFIER
        and base.text
        and base.prev_named_sibling is None
    ):
        parts.insert(0, base.text.decode(cs.ENCODING_UTF8))
    return cs.SEPARATOR_DOT.join(parts)


_CHAIN_STOP = ""


def _chain_part(node: Node) -> str | None:
    # (H) One backward step over the selector chain: a member selector or the
    # (H) base identifier contributes a name segment; `this`/`super` yield the
    # (H) empty STOP marker (their member resolves against the caller's
    # (H) class); anything else (index selector, a call result, an arbitrary
    # (H) expression base) has no static name.
    match node.type:
        case cs.TS_DART_SELECTOR:
            return _selector_member_name(node)
        case cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR:
            # (H) a super call attaches the member selector directly, without
            # (H) the `selector` wrapper
            return _first_identifier_text(node)
        case cs.TS_DART_IDENTIFIER:
            if node.text is None:
                return None
            return node.text.decode(cs.ENCODING_UTF8)
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
    rebuild the dotted target; a chain broken by an index (`xs[0].f()`) or a
    call result (`f().g()`) has no static name and returns None. A `this`/
    `super` base is dropped so the bare member name resolves against the
    caller's class.
    """
    if call_node.type == cs.TS_DART_CASCADE_SECTION:
        return _cascade_call_name(call_node)
    parts_rev: list[str] = []
    node = call_node.prev_named_sibling
    while node is not None:
        part = _chain_part(node)
        if part is None:
            return None
        if part == _CHAIN_STOP or node.type == cs.TS_DART_IDENTIFIER:
            if part != _CHAIN_STOP:
                parts_rev.append(part)
            break
        parts_rev.append(part)
        node = node.prev_named_sibling
    if not parts_rev:
        return None
    return cs.SEPARATOR_DOT.join(reversed(parts_rev))


def dart_extract_uri(node: Node) -> str | None:
    """The unquoted URI of an import/export/part directive, or None."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_DART_URI and current.text:
            return current.text.decode(cs.ENCODING_UTF8).strip(cs.DART_QUOTE_CHARS)
        stack.extend(current.children)
    return None


def dart_local_name(uri: str) -> str:
    """A short local key for an import URI (its last path segment, no `.dart`)."""
    segment = uri.rstrip(cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)[-1]
    if segment.endswith(cs.DART_EXT):
        segment = segment[: -len(cs.DART_EXT)]
    return segment or uri


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
