from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs


def dart_get_name(node: Node) -> str | None:
    # (H) Mirror of language_spec._dart_get_name for the parsers-internal callers
    # (H) (ingest_method) that cannot import language_spec without a cycle. Most
    # (H) nodes expose a `name` field; constructors/factories/mixins take their
    # (H) LAST bare identifier child (`C.named` -> `named`, default `C(...)` -> `C`).
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
