# Recovery for annotation macros written AFTER a C++ declarator: Clang
# thread-safety annotations (`bool Check() const LOCKS_REQUIRED(mu) {`,
# `int count_ GUARDED_BY(mu);`) and attribute macros (`void f()
# NOEXCEPT_MACRO;`), as used across Abseil, gRPC, Chromium, LLVM, RocksDB
# and googletest/gmock (issue #2552). tree-sitter never sees the #define, so
# the macro reads as a second declarator and the declaration is split around
# it. The split takes several shapes depending on what surrounds the macro:
#
# - The declaration closes at a zero-width MISSING `;` and the macro plus
#   the body becomes a type-less function_definition named after the macro
#   (`LOCKS_REQUIRED(mu) { ... }`). A method keeps its name through the
#   prototype, but its body calls are credited to `Box.LOCKS_REQUIRED`, a
#   node that does not exist; a free function keeps only its prototype and
#   none of its calls.
# - The real function_declarator is wrapped in an ERROR and the macro takes
#   the `declarator` field; a declaration of this shape registers a Method
#   named after the macro.
# - A data member's name is wrapped in an ERROR and the macro becomes the
#   declarator: `int count_ GUARDED_BY(mu);` registers a Method GUARDED_BY.
# - With no return type or qualifier to disambiguate, the part before the
#   macro reads as an expression: `void C::f()` as a variable initialised
#   with `()`, an out-of-class `C::~C()` as a call statement.
#
# Patching each consumer (names, spans, parameters, receiver types, caller
# attribution) would chase every shape through every pass. Instead the
# macro is blanked in place (space-filled, so byte offsets and line numbers
# survive) and the file is re-parsed, which is what a compiler sees once the
# macro has expanded to nothing. The tree picks WHERE to look: only a
# declarator next to a parse error anchors a scan, so a well-formed file is
# never touched. The text after the anchor decides WHAT is blanked: ALL_CAPS
# words, each with its balanced argument list, followed by the token that
# ends a declaration. The re-parse is kept only when it is strictly less
# damaged, as for the macro-marker pass.
import re
from collections.abc import Iterator

from tree_sitter import Node, Parser, Tree

from ... import constants as cs
from ..utils import safe_decode_text

_IDENTIFIER = re.compile(cs.CPP_IDENTIFIER_PATTERN)
_ANNOTATION_MACRO = re.compile(cs.CPP_ANNOTATION_MACRO_PATTERN)
_TRIVIA = re.compile(cs.CPP_TRIVIA_PATTERN, re.DOTALL)
_ARGUMENT_TOKEN = re.compile(cs.CPP_ARGUMENT_TOKEN_PATTERN, re.DOTALL)
_LITERAL = re.compile(cs.CPP_LITERAL_PATTERN, re.DOTALL)
_DECLARED_NAME = re.compile(cs.CPP_DECLARED_NAME_PATTERN)

# A declarator reaches its declaration through these wrappers (`int* f()`,
# `T& f()`), and recovery can wrap it in an ERROR as well.
_DECLARATOR_WRAPPERS = frozenset(
    {
        cs.TS_ERROR,
        cs.CppNodeType.POINTER_DECLARATOR,
        cs.CppNodeType.REFERENCE_DECLARATOR,
    }
)
_TYPED_OWNERS = frozenset(
    {
        cs.CppNodeType.DECLARATION,
        cs.CppNodeType.FIELD_DECLARATION,
        cs.CppNodeType.FUNCTION_DEFINITION,
    }
)
# Names that identify a function without a return type: an out-of-class
# definition (`void C::f()`), a destructor, an operator.
_SELF_EVIDENT_FUNCTION_NAMES = frozenset(
    {
        cs.CppNodeType.QUALIFIED_IDENTIFIER,
        cs.CppNodeType.DESTRUCTOR_NAME,
        cs.CppNodeType.OPERATOR_NAME,
    }
)
_PLAIN_NAMES = frozenset({cs.CppNodeType.IDENTIFIER, cs.CppNodeType.FIELD_IDENTIFIER})
# Scopes where no statement can stand, so a call statement there is a
# declaration tree-sitter misread.
_DECLARATION_SCOPES = frozenset(
    {cs.TS_CPP_TRANSLATION_UNIT, cs.TS_CPP_DECLARATION_LIST}
)
_STATEMENT_WRAPPERS = frozenset({cs.TS_EXPRESSION_STATEMENT, cs.TS_ERROR})


def retry_without_trailing_annotations(
    parser: Parser, tree: Tree, source_bytes: bytes
) -> tuple[Tree, bytes]:
    if not tree.root_node.has_error:
        return tree, source_bytes
    spans = sorted(
        {
            span
            for anchor in _anchors(tree.root_node)
            for span in _annotation_spans(source_bytes, anchor.end_byte)
        }
    )
    if not spans:
        return tree, source_bytes
    blanked = _blank_spans(source_bytes, spans)
    retry = parser.parse(blanked)
    if _damage(retry.root_node) < _damage(tree.root_node):
        return retry, blanked
    return tree, source_bytes


def _anchors(root: Node) -> Iterator[Node]:
    # Every shape leaves a MISSING node or an ERROR next to the real
    # declarator, so only error-bearing subtrees (and the declarator
    # wrappers inside them) are walked.
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.children:
            if child.has_error or child.type in _DECLARATOR_WRAPPERS:
                stack.append(child)
            if _is_anchor(child):
                yield child


def _is_anchor(node: Node) -> bool:
    if node.type == cs.CppNodeType.FUNCTION_DECLARATOR:
        return _declares_function(node)
    if node.type == cs.CppNodeType.INIT_DECLARATOR:
        # Cut off before the macro, `void ns::C::f()` is also a variable
        # initialised with `()`, and tree-sitter picks that reading.
        value = node.child_by_field_name(cs.FIELD_VALUE)
        return (
            value is not None
            and value.type == cs.TS_ARGUMENT_LIST
            and _has_typed_owner(node)
        )
    if node.type == cs.TS_CPP_CALL_EXPRESSION:
        return _is_misread_special_member(node)
    # A data member whose name recovery wrapped in an ERROR of its own.
    parent = node.parent
    return (
        node.type in _PLAIN_NAMES
        and parent is not None
        and parent.type == cs.TS_ERROR
        and parent.named_child_count == 1
        and _has_typed_owner(parent)
    )


def _declares_function(declarator: Node) -> bool:
    # A macro invocation also parses as a function_declarator (`Q_DISABLE_COPY
    # (X)` on its own line); only one with a return type, a self-evident
    # function name, or a constructor's name anchors a scan.
    name = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
    if name is None:
        return False
    if name.type in _SELF_EVIDENT_FUNCTION_NAMES:
        return True
    if name.type not in _PLAIN_NAMES:
        return False
    return _has_typed_owner(declarator) or safe_decode_text(name) == _class_name(
        declarator
    )


def _is_misread_special_member(call: Node) -> bool:
    # An out-of-class constructor or destructor has no return type, so cut
    # off before the macro `C::~C()` reads as a call statement at file or
    # namespace scope.
    function = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
    wrapper = call.parent
    scope = wrapper.parent if wrapper is not None else None
    return (
        function is not None
        and function.type == cs.CppNodeType.QUALIFIED_IDENTIFIER
        and wrapper is not None
        and wrapper.type in _STATEMENT_WRAPPERS
        and scope is not None
        and scope.type in _DECLARATION_SCOPES
    )


def _has_typed_owner(node: Node) -> bool:
    # The declarator must be the first thing after its declaration's type.
    # Two macro lines glued together (`Q_DISABLE_COPY(X)` then
    # `Q_DECLARE_PRIVATE(X)`) read as type `Q_DISABLE_COPY`, then `(X)`, then
    # the second macro as a declarator: the "type" is a macro, and so is the
    # declarator.
    child, owner = node, node.parent
    while owner is not None and owner.type in _DECLARATOR_WRAPPERS:
        if owner.type == cs.TS_ERROR and child.prev_named_sibling is not None:
            return False
        child, owner = owner, owner.parent
    if owner is None or owner.type not in _TYPED_OWNERS:
        return False
    type_node = owner.child_by_field_name(cs.FIELD_TYPE)
    if type_node is None:
        return False
    sibling = child.prev_named_sibling
    while sibling is not None and sibling != type_node:
        if sibling.type == cs.TS_ERROR:
            return False
        sibling = sibling.prev_named_sibling
    return sibling is not None


def _class_name(node: Node) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES:
            name = current.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name) if name is not None else None
        current = current.parent
    return None


def _annotation_spans(source: bytes, start: int) -> list[tuple[int, int]]:
    # The annotations between a declarator and the token that ends its
    # declaration; empty unless every word in between is an annotation or a
    # declarator suffix the grammar already knows.
    spans: list[tuple[int, int]] = []
    pos = _skip_trivia(source, start)
    while (word := _IDENTIFIER.match(source, pos)) is not None:
        name = word.group()
        if name in cs.CPP_DECLARATOR_END_KEYWORDS:
            break
        end = _word_end(source, word)
        if end is None:
            return []
        if name not in cs.CPP_DECLARATOR_SUFFIX_KEYWORDS:
            if not _ANNOTATION_MACRO.fullmatch(name):
                return []
            spans.append((word.start(), end))
        pos = _skip_trivia(source, end)
    if not spans or not _ends_declaration(source, pos):
        return []
    return spans


def _word_end(source: bytes, word: re.Match[bytes]) -> int | None:
    # Where `word` ends, past its argument list when it has one; None when
    # that list never closes or declares parameters (a function, no macro).
    end = word.end()
    after = _skip_trivia(source, end)
    if not source.startswith(cs.CPP_OPEN_PAREN, after):
        return end
    closed = _argument_list_end(source, after)
    if closed is None or _reads_as_parameters(source[after:closed]):
        return None
    return closed


def _ends_declaration(source: bytes, pos: int) -> bool:
    if pos < len(source) and source[pos] in cs.CPP_DECLARATOR_END_BYTES:
        return True
    word = _IDENTIFIER.match(source, pos)
    return word is not None and word.group() in cs.CPP_DECLARATOR_END_KEYWORDS


def _reads_as_parameters(arguments: bytes) -> bool:
    # `(int a)` declares a parameter; a macro's arguments are expressions.
    # This keeps `int ATTR(x) NAME(int a)`, a macro BEFORE an ALL_CAPS name,
    # from losing that name.
    return any(
        _DECLARED_NAME.search(code) is not None for code in _LITERAL.split(arguments)
    )


def _skip_trivia(source: bytes, pos: int) -> int:
    match = _TRIVIA.match(source, pos)
    return match.end() if match is not None else pos


def _argument_list_end(source: bytes, open_pos: int) -> int | None:
    depth = 0
    limit = min(len(source), open_pos + cs.CPP_ANNOTATION_MAX_ARGUMENT_BYTES)
    for token in _ARGUMENT_TOKEN.finditer(source, open_pos, limit):
        text = token.group()
        if text == cs.CPP_OPEN_PAREN:
            depth += 1
        elif text == cs.CPP_CLOSE_PAREN:
            depth -= 1
            if depth == 0:
                return token.end()
    return None


def _blank_spans(source: bytes, spans: list[tuple[int, int]]) -> bytes:
    out = bytearray(source)
    for start, end in spans:
        for i in range(start, end):
            if out[i] not in cs.CPP_LINE_BREAK_BYTES:
                out[i] = cs.CPP_BLANK_BYTE
    return bytes(out)


def _damage(root: Node) -> int:
    # ERROR nodes AND zero-width MISSING tokens: the commonest split leaves
    # only a MISSING `;` behind, which an ERROR count cannot see.
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_ERROR or node.is_missing:
            count += 1
        if node.has_error:
            stack.extend(node.children)
    return count
