from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..node_text import safe_decode_text, sorted_captures
from ..types_defs import (
    ASTNode,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
    PropertyDict,
)

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..types_defs import FunctionRegistryTrieProtocol


def shared_or_new[T](value: T | None, empty: Callable[[], T]) -> T:
    """The caller's container, or a fresh empty one when none was given.

    Passes share their index maps by reference and fill them after one
    another is built, so the given container must be kept even when it is
    empty: `value or {}` would swap an empty-but-shared map for a new one and
    lose every later write.
    """
    return value if value is not None else empty()


def follow_reexports(
    qn: str,
    import_mapping: dict[str, dict[str, str]],
    function_registry: FunctionRegistryTrieProtocol,
) -> str:
    # `from .pkg import sym` records the importer's name against the re-export
    # module (pkg.sym), not the real definition (pkg.mod.sym), so an unregistered
    # qn may be a re-export. Follow the module's import map one hop at a time
    # until a registered symbol is reached, guarding against cycles.
    if qn in function_registry:
        return qn
    return _ImportMapWalk(import_mapping, function_registry).follow(qn)[0]


class _ImportMapWalk:
    """One `follow_reexports` lookup.

    A module two star paths reach is walked on each: the first path can end
    ambiguous where the second does not, so marking the module visited for
    the whole lookup lost the export the second path provides. Only a qn on
    the current path stops a walk, which still ends every cycle; a qn settled
    without meeting one is remembered, so a module shared by many paths is
    still walked once.
    """

    __slots__ = ("_imports", "_registry", "_path", "_settled")

    def __init__(
        self,
        import_mapping: Mapping[str, Mapping[str, str]],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> None:
        self._imports = import_mapping
        self._registry = function_registry
        self._path: set[str] = set()
        self._settled: dict[str, str] = {}

    def follow(self, qn: str) -> tuple[str, bool]:
        # Where `qn` leads, and whether a cycle cut the walk short. A cut walk
        # depends on the path that reached it, so it is never settled.
        current = qn
        entered: list[str] = []
        cut = False
        while current and current not in self._registry and cs.SEPARATOR_DOT in current:
            if current in self._settled:
                current = self._settled[current]
                break
            if current in self._path:
                cut = True
                break
            self._path.add(current)
            entered.append(current)
            following, star_cut = self._step(current)
            cut = cut or star_cut
            if not following or following == current:
                break
            current = following
        self._path.difference_update(entered)
        if not cut:
            self._settled.update(dict.fromkeys(entered, current))
        return current, cut

    def _step(self, current: str) -> tuple[str | None, bool]:
        module_qn, _, name = current.rpartition(cs.SEPARATOR_DOT)
        if following := self._imports.get(module_qn, {}).get(name):
            return following, False
        return self._star_target(module_qn, name)

    def _star_target(self, module_qn: str, name: str) -> tuple[str | None, bool]:
        # A star re-export (`export * from './router'`, `from .mod import *`)
        # binds no name of its own, so a barrel's member is whichever star
        # source exports it, possibly through further stars. Only a source
        # that reaches a registered definition its module exports counts: a
        # private one is never re-exported, so it neither stands in for the
        # name nor makes an exported one ambiguous. Two exported ones are
        # ambiguous (TypeScript exports neither), so nothing is guessed; one
        # reached along two paths is one binding, not two.
        reached: set[str] = set()
        cut = False
        for key, source in self._imports.get(module_qn, {}).items():
            if not key.startswith(cs.GLOB_ALL):
                continue
            target, branch_cut = self.follow(f"{source}{cs.SEPARATOR_DOT}{name}")
            cut = cut or branch_cut
            if target in self._registry and not self._registry.is_module_private(
                target
            ):
                reached.add(target)
        return (reached.pop() if len(reached) == 1 else None), cut


def function_span_key(module_qn: str, node: Node) -> FunctionSpanKey:
    # tree-sitter points are 0-based; recorded lines are 1-based.
    return (module_qn, node.start_point[0] + 1, node.start_point[1])


# The traversal queries whose failure has been reported; reported once each,
# since every later call falls back the same way.
_FALLBACK_REPORTED: set[str] = set()


def note_query_fallback(query_text: str, error: Exception) -> None:
    """Report, once per query, that a traversal query failed and its tree
    walk, which gives the same answer more slowly, ran instead."""
    if query_text in _FALLBACK_REPORTED:
        return
    _FALLBACK_REPORTED.add(query_text)
    logger.warning(
        ls.TRAVERSAL_QUERY_FALLBACK.format(
            query=query_text.strip().splitlines()[0], error=error
        )
    )


class FunctionCapturesResult(NamedTuple):
    lang_config: LanguageSpec
    captures: dict[str, list[ASTNode]]


def get_function_captures(
    root_node: ASTNode,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> FunctionCapturesResult | None:
    lang_queries = queries[language]
    lang_config = lang_queries[cs.QUERY_CONFIG]

    if not (query := lang_queries[cs.QUERY_FUNCTIONS]):
        return None

    cursor = QueryCursor(query)
    captures = sorted_captures(cursor, root_node)
    return FunctionCapturesResult(lang_config, captures)


def extract_modifiers_and_decorators(
    node: ASTNode, lang_queries: LanguageQueries
) -> tuple[list[str], list[str]]:
    query = lang_queries.get(cs.QUERY_HIGHLIGHTS)
    if not query:
        return [], []

    cursor = QueryCursor(query)

    body_node = node.child_by_field_name(cs.FIELD_BODY)
    header_end_byte = body_node.start_byte if body_node else node.end_byte

    query_nodes = _annotated_nodes(node)
    target_node = query_nodes[-1]

    modifiers: list[str] = []
    decorators: list[str] = []

    for q_node in query_nodes:
        end_byte = header_end_byte if q_node == target_node else q_node.end_byte
        cursor.set_byte_range(q_node.start_byte, end_byte)
        _collect_modifier_captures(
            sorted_captures(cursor, q_node), modifiers, decorators
        )

    return modifiers, decorators


def _annotated_nodes(node: ASTNode) -> list[ASTNode]:
    """The nodes a definition's modifiers and decorators are written on, in
    source order: its leading annotation siblings, then the definition's own
    node or the wrapper that owns them."""
    target_node = node
    if node.parent and node.parent.type in (
        cs.TS_PY_DECORATED_DEFINITION,
        cs.TS_EXPORT_STATEMENT,
        # Dart wraps a class member's function_signature in a
        # method_signature that owns the `static` token.
        cs.TS_DART_METHOD_SIGNATURE,
    ):
        target_node = node.parent

    nodes = [target_node]
    curr_sibling = target_node.prev_named_sibling
    while curr_sibling and _is_leading_annotation(target_node, curr_sibling):
        nodes.insert(0, curr_sibling)
        curr_sibling = curr_sibling.prev_named_sibling
    return nodes


def decorated_start_props(node: ASTNode) -> PropertyDict:
    """`decorated_start_line` when decorators or attributes sit above `node`
    but outside its own span, else nothing (issue #2428).

    Read from the same nodes the `decorators` list is, so the decorated
    source of a definition starts at the first of them; a wrapper that starts
    on the definition's own line (`export class`) widens nothing. The node's
    `start_line` stays where it is: everything line-based keys on it.
    """
    first_row = _annotated_nodes(node)[0].start_point[0]
    if first_row >= node.start_point[0]:
        return {}
    return {cs.KEY_DECORATED_START_LINE: first_row + 1}


def _is_leading_annotation(target_node: ASTNode, sibling: ASTNode) -> bool:
    # Rust attributes, TS method decorators and Dart metadata precede the
    # definition as siblings rather than children.
    if sibling.type == cs.TS_RS_ATTRIBUTE_ITEM:
        return True
    if target_node.type == cs.TS_METHOD_DEFINITION:
        return sibling.type == cs.TS_DECORATOR
    return (
        target_node.type in (cs.TS_DART_METHOD_SIGNATURE, cs.TS_DART_FUNCTION_SIGNATURE)
        and sibling.type == cs.TS_DART_ANNOTATION
    )


def _collect_modifier_captures(
    captures: dict[str, list[ASTNode]],
    modifiers: list[str],
    decorators: list[str],
) -> None:
    for name, nodes in captures.items():
        if name.startswith(cs.CAPTURE_KEYWORD_MODIFIER) or name == cs.CAPTURE_KEYWORD:
            _append_unique_texts(nodes, modifiers, cs.EXCLUDED_KEYWORDS)
        elif name.startswith(cs.CAPTURE_ATTRIBUTE) or name.startswith(
            cs.CAPTURE_FUNCTION_DECORATOR
        ):
            _append_unique_texts(nodes, decorators, frozenset())


def _append_unique_texts(
    nodes: list[ASTNode], out: list[str], excluded: Collection[str]
) -> None:
    for n in nodes:
        text = safe_decode_text(n)
        if text and text not in out and text not in excluded:
            out.append(text)


# The member-name fields a call's callee carries: JS/TS `property`, Python
# `attribute`, Rust/Go/C++/Scala `field`.
_MEMBER_NAME_FIELDS = (cs.FIELD_PROPERTY, cs.TS_PY_FIELD_ATTRIBUTE, cs.FIELD_FIELD)


def member_call_name(node: Node) -> Node | None:
    """The method name a member call invokes, or None for any other call."""
    # Java `method_invocation` / PHP `member_call_expression`: the call
    # itself holds the receiver and the name.
    if node.child_by_field_name(cs.FIELD_OBJECT) is not None and (
        name := node.child_by_field_name(cs.FIELD_NAME)
    ):
        return name
    func = node.child_by_field_name(cs.FIELD_FUNCTION)
    if func is not None and func.type == cs.TS_GENERIC_FUNCTION:
        # Rust turbofish `x.parse::<u64>()`.
        func = func.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None:
        return None
    if func.type == cs.TS_CSHARP_MEMBER_ACCESS_EXPRESSION:
        return func.child_by_field_name(cs.FIELD_NAME)
    if func.type == cs.TS_CSHARP_CONDITIONAL_ACCESS_EXPRESSION:
        binding = next(
            (
                c
                for c in func.children
                if c.type == cs.TS_CSHARP_MEMBER_BINDING_EXPRESSION
            ),
            None,
        )
        return binding.child_by_field_name(cs.FIELD_NAME) if binding else None
    for field in _MEMBER_NAME_FIELDS:
        if (member := func.child_by_field_name(field)) is not None:
            return member
    return None


def call_site_start(node: Node) -> tuple[int, int]:
    """Where a call's edge site starts: a member call's name, else the node.

    Shared by indexing and by the edit operations that find a call again
    from its recorded (line, col) (issue #3166).
    """
    name = member_call_name(node)
    if name is not None and name.start_byte > node.start_byte:
        return name.start_point
    return node.start_point


def node_site_properties(node: Node) -> PropertyDict:
    """Edge-site span of a tree-sitter node (issue #1522).

    1-based lines, 0-based columns, end exclusive, matching the node
    `start_line`/`start_col` convention so a site can be sliced straight
    out of the file.
    """
    return {
        cs.KEY_LINE: node.start_point[0] + 1,
        cs.KEY_COL: node.start_point[1],
        cs.KEY_END_LINE: node.end_point[0] + 1,
        cs.KEY_END_COL: node.end_point[1],
    }


def import_site_props(
    node: Node, local_name: str, imported_name: str | None
) -> PropertyDict:
    # Sentinel keys (`*pkg` wildcards, Go `.pkg` dot-imports) bind no name,
    # so they carry no alias.
    site = node_site_properties(node)
    if not local_name.startswith((cs.IMPORTED_NAME_WILDCARD, cs.SEPARATOR_DOT)):
        site[cs.KEY_ALIAS] = local_name
    if imported_name:
        site[cs.KEY_IMPORTED_NAME] = imported_name
    return site


def is_project_qn(qn: str, project_name: str) -> bool:
    return qn == project_name or qn.startswith(f"{project_name}{cs.SEPARATOR_DOT}")


def contains_node(parent: ASTNode, target: ASTNode) -> bool:
    return parent == target or any(
        contains_node(child, target) for child in parent.children
    )


def unparenthesized(node: Node | None) -> Node | None:
    """The expression under any parentheses: `(fn)()` still calls `fn`.

    Without the unwrap a stored computed lookup invoked as `(fn)()` was not
    recognised as a call, and `(registry[name])()` was not recognised as a
    computed dispatch, so an unrelated invoked literal in the same body could
    be recorded as the dynamic-trace site (#1543).
    """
    while node is not None and node.type == cs.TS_PARENTHESIZED_EXPRESSION:
        node = next(iter(node.named_children), None)
    return node


def decorator_tail_names(decorators: list[str]) -> set[str]:
    return {
        decorator.lstrip("@#[]() ")
        .split("(")[0]
        .split(cs.SEPARATOR_DOT)[-1]
        .rstrip(")] ")
        for decorator in decorators
    }


def is_static_decorator(decorators: list[str]) -> bool:
    return bool(decorator_tail_names(decorators) & cs.STATIC_DECORATORS)


def _is_abstract_decorator(decorators: list[str]) -> bool:
    return bool(decorator_tail_names(decorators) & cs.ABSTRACT_DECORATORS)


def _is_bodiless_ts_member(
    method_node: ASTNode, language: cs.SupportedLanguage
) -> bool:
    # An interface or `abstract` TS member never runs itself, like a Python
    # @abstractmethod stub (issue #2524). Gated on language because Dart spells
    # its ordinary, bodied members `method_signature` too.
    return (
        language in cs.JS_TS_LANGUAGES
        and method_node.type in cs.TS_BODILESS_METHOD_TYPES
    )


def is_abstract_member(
    decorators: list[str], method_node: ASTNode, language: cs.SupportedLanguage
) -> bool:
    return _is_abstract_decorator(decorators) or _is_bodiless_ts_member(
        method_node, language
    )


def is_stored_abstract_member(
    decorators: list[str],
    modifiers: list[str],
    language: cs.SupportedLanguage | None,
) -> bool:
    """`is_abstract_member` read back from a stored definition's properties.

    An incremental run rehydrates unchanged members without their trees: a TS
    `abstract` member is told by its modifier. An interface member is told by
    its owner's label, which the call resolver reads itself.
    """
    return _is_abstract_decorator(decorators) or (
        language in cs.JS_TS_LANGUAGES and cs.TS_ABSTRACT_MODIFIER in modifiers
    )


def find_descendant(node: Node | None, node_type: str) -> Node | None:
    if node is None:
        return None
    stack: list[Node] = [node]
    while stack:
        current = stack.pop()
        if current.type == node_type:
            return current
        stack.extend(current.children)
    return None


def is_method_node(func_node: ASTNode, lang_config: LanguageSpec) -> bool:
    if lang_config.language == cs.SupportedLanguage.JULIA:
        return False

    current = func_node.parent
    if not isinstance(current, Node):
        return False

    class_types = lang_config.class_node_types
    func_types = lang_config.function_node_types
    module_types = lang_config.module_node_types
    body_field = cs.FIELD_BODY

    while current is not None:
        current_type = current.type
        if current_type in module_types:
            return False
        if current_type in class_types:
            return True
        if (
            current_type in func_types
            and current.child_by_field_name(body_field) is not None
        ):
            return False
        current = current.parent
    return False


def enclosing_class_node(
    func_node: ASTNode, lang_config: LanguageSpec
) -> ASTNode | None:
    """The nearest class node above func_node, or None if the module comes first.

    Unlike is_method_node this does not stop at an enclosing function, so a
    `def inner()` nested in a method still reports its class. The call
    processor uses it to hand everything scoped inside a class body to the
    class pass instead of walking it a second time (issue #1903).
    """
    current = func_node.parent
    if not isinstance(current, Node):
        return None
    class_types = lang_config.class_node_types
    module_types = lang_config.module_node_types
    while current is not None:
        if current.type in module_types:
            return None
        if current.type in class_types:
            return current
        current = current.parent
    return None


def module_qn_for_entity(
    entity_qn: str, module_paths: Mapping[str, object]
) -> str | None:
    """The indexed module a fully-qualified entity belongs to, or None.

    Entity qns extend their module's qn with class and member segments, so
    the module is the longest strict prefix that names an indexed module
    (issue #1347). Only key membership is read, so the mapping's value type
    does not matter.
    """
    candidate = entity_qn
    while cs.SEPARATOR_DOT in candidate:
        candidate = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
        if candidate in module_paths:
            return candidate
    return None


def warn_if_name_truncated(
    node: Node, name: str | None, file_path: Path | None = None
) -> None:
    """Log when a symbol name was truncated by an invalid byte.

    An invalid byte inside an identifier is a token boundary to tree-sitter,
    so the `name` node covers only the bytes on one side of it. The extractor
    then decodes that shortened node without error: `calculate_total` is
    indexed as `calculate_t`, with nothing raised and nothing logged. A wrong
    name is worse than a missing one -- callers in untouched files stop
    resolving to it, so it reads as dead code while a symbol nobody calls
    appears beside it.

    The discriminator is the byte IMMEDIATELY ADJACENT to the name's span:
    the one the grammar split on. Two narrower-sounding shapes are both wrong:

    * The name node's own bytes never contain the bad byte -- tree-sitter
      excludes it -- so they decode cleanly in the corrupt and the clean case
      alike. A check on them detects nothing at all.
    * The enclosing DEFINITION's span does contain it, but also spans the
      whole body, so it fires on a bad byte in any body comment, docstring or
      string literal while the symbol is indexed under its correct name, and
      once per enclosing definition -- one bad byte in a nested class yields a
      warning per level.

    Measured across 105,352 real source files (Rust crates, Go modules, the
    macOS SDK, Homebrew, site-packages): 165 are not valid UTF-8 and NONE
    corrupts a symbol -- every one is latin-1 punctuation in a copyright
    header or an author's name. All 165 are exactly the shape the second
    bullet misfires on, which is why the adjacency test earns its place.
    """
    if not name:
        return
    # The replacement character can only be in a NAME because a decode put it
    # there: #1797's fix decodes with errors="replace", so a bad byte the
    # grammar keeps INSIDE the name expression (Lua's `Greeter.gr\ufffdeet`,
    # where the byte lands in an ERROR node between two identifiers) survives
    # as U+FFFD rather than truncating the token. No legitimate identifier
    # contains it, in any language, so this needs no adjacency test.
    if cs.UNICODE_REPLACEMENT_CHAR in name:
        logger.warning(
            ls.TRUNCATED_SYMBOL_NAME.format(
                path=file_path if file_path is not None else "<unknown>",
                name=name,
            )
        )
        return
    source = _node_source_bytes(node)
    if source is None:
        return
    # Callers pass the DEFINITION node, which is what they have. Narrow to the
    # span the extracted name actually came from: `child_by_field_name` alone
    # is not enough, because grammars nest the identifier differently (C puts
    # it under `function_declarator`, so the definition has no `name` field at
    # all and the search below is what finds it).
    span = _name_span(node, name)
    if span is None:
        return
    start, end = span
    # A WINDOW either side, not a single byte. A legitimate multi-byte
    # character next to the name (`def alpha\u00a9()` under error recovery, where
    # `\u00a9` is the two bytes c2 a9) has neither half decodable ALONE, so a
    # one-byte probe calls valid source corrupt. Four bytes is the longest
    # UTF-8 sequence, so a window that size decodes cleanly whenever the
    # neighbouring text is well-formed.
    before = source[max(0, start - cs.UTF8_MAX_SEQUENCE_BYTES) : start]
    after = source[end : end + cs.UTF8_MAX_SEQUENCE_BYTES]
    for probe, at_end in ((before, False), (after, True)):
        if not probe:
            continue
        if _has_invalid_byte(probe, at_end=at_end):
            logger.warning(
                ls.TRUNCATED_SYMBOL_NAME.format(
                    path=file_path if file_path is not None else "<unknown>",
                    name=name,
                )
            )
            return


# A definition's own name is at most a step or two above it (the JS/TS
# field-definition wrapper is one). Bounded so a deeply nested node cannot walk
# to the root and match an unrelated enclosing definition of the same name.
_NAME_SPAN_ANCESTOR_LIMIT = 3


def _name_span(node: Node, name: str) -> tuple[int, int] | None:
    """Byte span of the identifier `name` was decoded from, or None.

    Prefers the `name` field, then the shallowest descendant whose own text
    equals the extracted name. The search is bounded to identifier-ish leaves
    and stops at the first match, so it stays cheap on large definitions.
    """
    # A synthesized name need not appear in the source at all: C# destructor
    # ingestion builds `~Greeter` while the source leaf is just `Greeter`, and
    # a leading sigil is the general shape of that. Strip non-identifier
    # prefixes so the span lookup matches what the grammar actually holds.
    name = name.lstrip(cs.SYNTHETIC_NAME_PREFIXES)
    if not name:
        return None
    encoded = name.encode(cs.ENCODING_UTF8)
    direct = node.child_by_field_name(cs.FIELD_NAME)
    if direct is not None and direct.text == encoded:
        return direct.start_byte, direct.end_byte

    stack = [node]
    while stack:
        current = stack.pop(0)
        if current is not node and current.text == encoded and not current.children:
            return current.start_byte, current.end_byte
        stack.extend(current.children)

    # Not below the node: a JS/TS class-field arrow or function expression is
    # named by its ENCLOSING field definition (`greet = (n) => n`), and
    # `_js_ts_field_member_name` reads the binding from there, so the
    # identifier sits above the node the caller passed. Walk out a bounded
    # distance rather than threading a node through every extraction branch --
    # a name that belongs to this definition is always within a step or two.
    ancestor = node.parent
    for _ in range(_NAME_SPAN_ANCESTOR_LIMIT):
        if ancestor is None:
            break
        field = ancestor.child_by_field_name(cs.FIELD_NAME)
        if field is not None and field.text == encoded:
            return field.start_byte, field.end_byte
        ancestor = ancestor.parent
    return None


def _has_invalid_byte(window: bytes, *, at_end: bool) -> bool:
    """Whether `window` contains a byte that no valid UTF-8 sequence explains.

    The window is sliced at an arbitrary offset, so it can begin or end
    mid-character even when the source is well-formed: `\u00a9` is the two bytes
    c2 a9, and either half alone fails to decode. Only a genuinely invalid
    byte should count, so the partial sequence at the cut edge is trimmed
    before decoding -- `at_end` says which edge is the cut one (the window
    BEFORE the name is cut at its start, the one AFTER at its end).
    """
    for trim in range(len(window)):
        candidate = window[trim:] if not at_end else window[: len(window) - trim]
        if not candidate:
            return False
        try:
            candidate.decode(cs.ENCODING_UTF8)
        except UnicodeDecodeError:
            continue
        return False
    return True


def _node_source_bytes(node: Node) -> bytes | None:
    """The whole source buffer the node's byte offsets index into.

    `start_byte`/`end_byte` are offsets into the FILE, so the adjacency probe
    needs the file's bytes rather than the node's own slice.
    """
    root = node
    while (parent := getattr(root, "parent", None)) is not None:
        root = parent
    raw = getattr(root, "text", None)
    return raw if isinstance(raw, bytes) else None


def written_simple_name(name: str) -> str:
    """The last segment of a written name, whatever the separator: `geo::Shape`
    and `pkg.Base` both name what a defining file's own simple name does
    (issue #1568). One spelling for every pass that records an unresolved
    reference, so a waiter and its provider agree on the key."""
    return name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT).rsplit(
        cs.SEPARATOR_DOT, 1
    )[-1]


def method_signature_arity(method_name: str) -> int | None:
    # Number of top-level parameters in a signatured method name
    # (`readField(A,JsonReader,BoundField)` -> 3, `create()` -> 0); None when the
    # name carries no signature (Python/JS methods). Commas inside generics
    # (`Map<K, V>`) are nested, so only depth-0 commas separate parameters.
    open_idx = method_name.find(cs.CHAR_PAREN_OPEN)
    if open_idx < 0:
        return None
    inner = method_name[open_idx + 1 : method_name.rfind(cs.CHAR_PAREN_CLOSE)]
    if not inner.strip():
        return 0
    depth = 0
    count = 1
    for ch in inner:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        elif ch == "," and depth == 0:
            count += 1
    return count
