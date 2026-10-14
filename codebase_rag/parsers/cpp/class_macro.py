# Recovery for a macro written between `class` (or `struct`, `union`) and
# the class name: the export/visibility idiom `class SPDLOG_API Logger {...}`
# of spdlog, Qt (`Q_CORE_EXPORT`), googletest (`GTEST_API_`), abseil
# (`ABSL_DLL`) and most shared libraries (issue #2840). tree-sitter never
# sees the #define, so it reads a function named `Logger` whose return type
# is `class SPDLOG_API`: the class is named after the macro, its members are
# lost or turn into free functions, and every class so marked in one file
# collapses into one node.
#
# Whether as that definition or, on one line, inside an ERROR, the shape is
# a body-less class specifier, then a bare identifier, then the `{` or `:`
# that opens a class. Valid C++ never has it: `class Foo foo;` ends at the
# `;`, and a real function needs a function declarator. So the shape alone
# marks the macro, which is blanked in place (space-filled, so byte offsets
# and line numbers survive) before a re-parse, as a compiler sees the file
# once the macro has expanded to nothing. The re-parse is kept unless it is
# more damaged, as for the other recovery passes.
from tree_sitter import Node, Parser, QueryCursor, Tree

from ... import constants as cs
from ..utils import get_cached_query
from .trailing_annotation import _blank_spans, _damage


def retry_without_class_name_macros(
    parser: Parser, tree: Tree, source_bytes: bytes
) -> tuple[Tree, bytes]:
    if parser.language is None:
        return tree, source_bytes
    cursor = QueryCursor(get_cached_query(parser.language, cs.CPP_MACRO_CLASS_QUERY))
    spans = sorted(
        {
            (macro.start_byte, macro.end_byte)
            for specifier in cursor.captures(tree.root_node).get(
                cs.CAPTURE_CPP_MACRO_CLASS, []
            )
            if (macro := _macro_in_place_of_name(specifier)) is not None
        }
    )
    if not spans:
        return tree, source_bytes
    blanked = _blank_spans(source_bytes, spans)
    retry = parser.parse(blanked)
    if _damage(retry.root_node) <= _damage(tree.root_node):
        return retry, blanked
    return tree, source_bytes


def _macro_in_place_of_name(specifier: Node) -> Node | None:
    # A body-less class specifier followed by a bare identifier and the
    # class's opening `{` or `:`: its `name` is the macro.
    if specifier.child_by_field_name(cs.FIELD_BODY) is not None:
        return None
    name = specifier.next_sibling
    if name is None or name.type != cs.TS_IDENTIFIER:
        return None
    opener = name.next_sibling
    if opener is None or (opener.text or b"")[:1] not in cs.CPP_CLASS_OPENERS:
        return None
    return specifier.child_by_field_name(cs.FIELD_NAME)
