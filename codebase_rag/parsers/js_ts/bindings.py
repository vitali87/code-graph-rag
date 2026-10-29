"""Where a JavaScript/TypeScript name is bound, read without the type engine.

The call pass resolves a callee or receiver by the declaration that is in
scope at the call: a `const`/`let`/`var` declarator, a parameter, a loop or
catch binding, or a hoisted function declaration. This module indexes those
introductions per scope (`scope_introductions`), finds the innermost
enclosing scope that decides a name (`innermost_enclosing`), finds a tree's
named functions (`named_functions`) and builds the whole-file binding index
the receiver resolution of #988 reads (`JsFileBindingCollector`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from functools import lru_cache
from types import MappingProxyType

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_text

# Declarator kinds for the symbol-index value chase (issue #989). OPAQUE
# marks an introduction whose value cannot be known statically (a loop or
# catch binding, an uninitialised declarator): it wins scoping and refuses
# typing. PARAM is a parameter, carried with its callable so a declared type
# (annotation or JSDoc) can be read; the value chase refuses it like OPAQUE.
DECL_PLAIN = "plain"
DECL_DESTRUCTURED = "destructured"
_DECL_OPAQUE = "opaque"
DECL_PARAM = "param"


# Ancestors bounding a `let`/`const` declaration's scope (mirrors the
# type-inference engine's set).
_SCOPE_CONTAINER_TYPES = frozenset(
    {
        "statement_block",
        "switch_body",
        "for_statement",
        "for_in_statement",
    }
)


# Inline function values in an object literal (`{ onSuccess: () => {} }`): the
# JS/TS definition pass registers these as their own nodes named by the key
# (scope.onSuccess), so a passed object of callbacks must reference each or
# every TanStack-style callback reports as dead.
# Generator expressions are inline function values like the other two
# (issue #994): a registered node with no reference path is an orphan.
INLINE_FUNC_VALUE_TYPES = frozenset(
    {cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION}
)


# JS/TS scopes that bind their own parameters (and, for a named function
# expression, their own name) for the lexical receiver resolution of #988.
_LEXICAL_CALLABLE_SCOPES = frozenset(
    {
        cs.TS_ARROW_FUNCTION,
        cs.TS_FUNCTION_EXPRESSION,
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
        cs.TS_METHOD_DEFINITION,
    }
)


# Declarations hoisted to function/module scope.
_LEXICAL_HOISTED_DECLARATIONS = frozenset(
    {cs.TS_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION_DECLARATION}
)


# Inline values that ARE callables when held by a declarator; generators
# included (a `const gen = function* () {...}` receiver is invocable).
_INLINE_CALLABLE_VALUE_TYPES = INLINE_FUNC_VALUE_TYPES | {cs.TS_GENERATOR_FUNCTION}


# Declarations that bind a VALUE name for the receiver index: classes, TS
# enums (compiled to vars) and namespaces/modules (compiled to objects).
_VALUE_TYPE_DECLARATIONS = frozenset(
    {
        cs.TS_CLASS_DECLARATION,
        cs.TS_CLASS_EXPRESSION,
        cs.TS_ENUM_DECLARATION,
        cs.TS_INTERNAL_MODULE,
        cs.TS_MODULE,
    }
)


class JsFileBindingCollector:
    """Whole-file binding index for the #988 receiver resolution.

    One pass over a JS/TS file: name -> introductions as (function node or
    None, container node or None for file-wide). Non-function introductions
    are OVER-collected on purpose (destructuring, import aliases,
    reassignment targets): each can only suppress. A `with` block makes every
    name dynamic, so the whole file collapses to an empty index and nothing
    ever resolves.

    TS declaration merging: every block of one namespace shares one scope for
    EXPORTED direct members, so those members gain one introduction per
    SIBLING block afterwards; that is exactly where merging makes them
    visible, and nowhere else. Merge groups key on (nearest NON-namespace
    container id, dotted namespace path): `A { N }` in either A block and
    `A.N` at top level all key to (program, "A.N") and merge, while a
    top-level `N` keys to (program, "N") and stays distinct. Node identity
    uses tree-sitter's stable `.id` (fresh Python wrappers over the same tree
    node compare unequal with `is`).
    """

    __slots__ = (
        "_bindings",
        "_namespace_blocks",
        "_namespace_group_of",
        "_ns_exported",
        "_poisoned",
        "_unwrap_ts_value",
    )

    def __init__(self, unwrap_ts_value: Callable[[Node], Node]) -> None:
        self._unwrap_ts_value = unwrap_ts_value
        self._bindings: dict[str, list[tuple[Node | None, Node | None]]] = {}
        self._namespace_blocks: dict[tuple[int, str], list[Node]] = {}
        self._namespace_group_of: dict[int, tuple[int, str]] = {}
        self._ns_exported: list[tuple[str, Node | None, Node]] = []
        self._poisoned = False

    def collect(self, root: Node) -> dict[str, list[tuple[Node | None, Node | None]]]:
        stack: list[tuple[Node, Node, int, tuple[str, ...]]] = [
            (root, root, root.id, ())
        ]
        while stack:
            node, fn_container, ns_anchor, ns_path = stack.pop()
            descend, frame = self._visit(node, fn_container, ns_anchor, ns_path)
            if self._poisoned:
                return {}
            if descend:
                for child in node.named_children:
                    stack.append((child, *frame))
        self._replicate_merged_members()
        return self._bindings

    def _visit(
        self, node: Node, fn_container: Node, ns_anchor: int, ns_path: tuple[str, ...]
    ) -> tuple[bool, tuple[Node, int, tuple[str, ...]]]:
        node_type = node.type
        frame = (fn_container, ns_anchor, ns_path)
        if node_type in _LEXICAL_CALLABLE_SCOPES:
            return True, self._visit_callable(node, fn_container)
        if node_type == cs.TS_CLASS_STATIC_BLOCK:
            # A static block is its own `var` scope.
            return True, (node, node.id, ())
        if node_type == cs.TS_JS_WITH_STATEMENT:
            # Dynamic scoping: no receiver in this file can be trusted.
            self._poisoned = True
            return False, frame
        if node_type == cs.TS_VARIABLE_DECLARATOR:
            self._visit_declarator(node, fn_container)
        elif node_type == cs.TS_JS_FOR_IN_STATEMENT:
            # Covers for-of and for-in, with or without var/let/const: the
            # left side binds (or rebinds) its identifiers. The enclosing
            # callable body over-approximates the lexical variants' scope,
            # which can only suppress.
            left = node.child_by_field_name(cs.FIELD_LEFT)
            if left is not None:
                self._add_pattern(left, fn_container)
        elif node_type == cs.TS_JS_CATCH_CLAUSE:
            param = node.child_by_field_name(cs.FIELD_PARAMETER)
            if param is not None:
                self._add_pattern(param, node)
        elif node_type in _VALUE_TYPE_DECLARATIONS:
            return True, self._visit_type_binding(node, frame)
        elif node_type in (cs.TS_IMPORT_ALIAS, cs.TS_IMPORT_STATEMENT):
            # Either import form binds every identifier it contains: the
            # default/named/namespace clauses, the TS `import x =
            # require(...)` clause, and the namespace alias `import x =
            # Other.thing` (whose aliased path identifiers over-collect,
            # which can only suppress). The module path is a string.
            self._add_pattern(node, None)
            return False, frame
        elif node_type in (
            cs.TS_JS_ASSIGNMENT_EXPRESSION,
            cs.TS_JS_AUGMENTED_ASSIGNMENT_EXPRESSION,
        ):
            self._visit_assignment(node)
        elif node_type == cs.TS_FUNCTION_SIGNATURE:
            self._visit_function_signature(node, fn_container)
        return True, frame

    def _visit_function_signature(self, node: Node, fn_container: Node) -> None:
        # An ambient namespace member (`declare namespace N { export function
        # helper(): number }`) is a function_signature, yet declaration
        # merging makes it a real introduction in every sibling block of the
        # same namespace: a call there that TS resolves to the merged member
        # must not fall through to a same-named file-level function (issue
        # #996). Count-only (no function node) so it can only suppress, and
        # replicated across the merge group like other exported members;
        # file-level overload signatures stay uncollected — they denote the
        # same function as their implementation. Ambient members are exported
        # whether or not they carry the keyword, so every namespace-level
        # signature replicates.
        if fn_container.type not in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE):
            return
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or not (sig_name := safe_decode_text(name_node)):
            return
        if self._sibling_implementation_exists(node, sig_name):
            # An overload signature beside its implementation in the same
            # block denotes THAT implementation, not a distinct introduction;
            # counting it would discard correct resolutions to the
            # implementation. Only declaration-only signatures participate.
            return
        self._add(sig_name, None, fn_container)
        self._ns_exported.append((sig_name, None, fn_container))

    @staticmethod
    def _sibling_implementation_exists(node: Node, sig_name: str) -> bool:
        block = node.parent
        if block is not None and block.type == cs.TS_EXPORT_STATEMENT:
            block = block.parent
        if block is None:
            return False
        for sibling in block.named_children:
            candidate: Node | None = sibling
            if sibling.type == cs.TS_EXPORT_STATEMENT:
                candidate = next(iter(sibling.named_children), None)
            if candidate is None or candidate.type not in (
                cs.TS_FUNCTION_DECLARATION,
                cs.TS_GENERATOR_FUNCTION_DECLARATION,
            ):
                continue
            impl_name = candidate.child_by_field_name(cs.FIELD_NAME)
            if impl_name is not None and safe_decode_text(impl_name) == sig_name:
                return True
        return False

    def _visit_callable(
        self, node: Node, fn_container: Node
    ) -> tuple[Node, int, tuple[str, ...]]:
        params = node.child_by_field_name(cs.FIELD_PARAMETERS)
        if params is not None:
            self._add_pattern(params, node)
        elif (single := node.child_by_field_name(cs.FIELD_PARAMETER)) is not None:
            self._add_pattern(single, node)
        node_type = node.type
        if node_type in (cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION):
            # A named function expression binds its own name in its body only.
            name_node = node.child_by_field_name(cs.FIELD_NAME)
            if name_node is not None:
                self._add(safe_decode_text(name_node), node, node)
        elif node_type in _LEXICAL_HOISTED_DECLARATIONS:
            self._visit_hoisted_declaration(node, fn_container)
        # A method_definition name is a property, never a binding.
        body = node.child_by_field_name(cs.FIELD_BODY) or node
        return body, body.id, ()

    def _visit_hoisted_declaration(self, node: Node, fn_container: Node) -> None:
        # Function declarations hoist; Annex B makes even a block-level one
        # function-scoped in sloppy mode, and one under a switch case is live
        # across the whole switch body, so the enclosing callable body (or
        # module) is the only container that never understates the binding's
        # reach. In strict-mode block cases this over-widens, which can only
        # suppress.
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None:
            return
        decl_name = safe_decode_text(name_node)
        self._add(decl_name, node, fn_container)
        if (
            decl_name
            and fn_container.type in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE)
            and node.parent is not None
            and node.parent.type == cs.TS_EXPORT_STATEMENT
        ):
            self._ns_exported.append((decl_name, node, fn_container))

    def _visit_declarator(self, node: Node, fn_container: Node) -> None:
        target = node.child_by_field_name(cs.FIELD_NAME)
        declaration = node.parent
        if target is None or declaration is None:
            return
        container = self._declarator_container(declaration, fn_container)
        if target.type != cs.TS_IDENTIFIER:
            self._add_pattern(target, container)
            return
        value = node.child_by_field_name(cs.FIELD_VALUE)
        fn_value: Node | None = None
        if value is not None:
            value = self._unwrap_ts_value(value)
            if value.type in _INLINE_CALLABLE_VALUE_TYPES:
                fn_value = value
        target_name = safe_decode_text(target)
        self._add(target_name, fn_value, container)
        wrapper = declaration.parent
        if (
            target_name
            and wrapper is not None
            and wrapper.type == cs.TS_EXPORT_STATEMENT
            and (body := wrapper.parent) is not None
            and (ns := body.parent) is not None
            and ns.type in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE)
        ):
            self._ns_exported.append((target_name, fn_value, ns))

    def _visit_type_binding(
        self, node: Node, frame: tuple[Node, int, tuple[str, ...]]
    ) -> tuple[Node, int, tuple[str, ...]]:
        # Classes, TS enums and namespaces all bind VALUE names (an enum
        # compiles to a var, a namespace to an object). A namespace/module
        # body is additionally its own scope: its members are invisible
        # outside it.
        _fn_container, ns_anchor, ns_path = frame
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None:
            self._add(safe_decode_text(name_node), None, None)
        if node.type not in (cs.TS_INTERNAL_MODULE, cs.TS_MODULE):
            return frame
        if name_node is not None and (ns_name := safe_decode_text(name_node)):
            ns_path = ns_path + tuple(ns_name.split(cs.SEPARATOR_DOT))
            group = (ns_anchor, cs.SEPARATOR_DOT.join(ns_path))
            self._namespace_blocks.setdefault(group, []).append(node)
            self._namespace_group_of[node.id] = group
        return node, ns_anchor, ns_path

    def _visit_assignment(self, node: Node) -> None:
        # A reassignment rebinds a name declared SOMEWHERE to an arbitrary
        # value; file-wide relevance is the safe reading.
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        if left.type == cs.TS_IDENTIFIER:
            self._add(safe_decode_text(left), None, None)
        elif left.type in (cs.TS_OBJECT_PATTERN, cs.TS_ARRAY_PATTERN):
            self._add_pattern(left, None)

    def _add(
        self, name: str | None, fn_node: Node | None, container: Node | None
    ) -> None:
        if name:
            self._bindings.setdefault(name, []).append((fn_node, container))

    def _add_pattern(self, pattern: Node, container: Node | None) -> None:
        # Binding-side only: a default value (assignment_pattern right, or
        # the TS grammar's wrapperless parameter default outside the pattern
        # field) or a type annotation READS names, it does not introduce them.
        stack = [pattern]
        while stack:
            part = stack.pop()
            if part.type == cs.TS_ASSIGNMENT_PATTERN:
                left = part.child_by_field_name(cs.FIELD_LEFT)
                if left is not None:
                    stack.append(left)
                continue
            if part.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
                ts_pattern = part.child_by_field_name(cs.TS_FIELD_PATTERN)
                if ts_pattern is not None:
                    stack.append(ts_pattern)
                continue
            if part.type == cs.TS_TYPE_ANNOTATION:
                continue
            if part.type in (
                cs.TS_IDENTIFIER,
                cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
            ):
                self._add(safe_decode_text(part), None, container)
            stack.extend(part.named_children)

    @staticmethod
    def _declarator_container(declaration: Node, fn_container: Node) -> Node | None:
        # `var` hoists to the enclosing callable body; `let`/`const` scope to
        # the declaration's parent, peeling an export_statement wrapper and
        # widening a switch-case slot to the shared switch body.
        if declaration.type != cs.TS_LEXICAL_DECLARATION:
            return fn_container
        parent = declaration.parent
        if parent is None:
            return fn_container
        if parent.type == cs.TS_EXPORT_STATEMENT:
            return parent.parent
        if parent.type in (cs.TS_JS_SWITCH_CASE, cs.TS_JS_SWITCH_DEFAULT):
            return parent.parent
        return parent

    def _replicate_merged_members(self) -> None:
        for member_name, fn_node, block in self._ns_exported:
            group = self._namespace_group_of.get(block.id)
            if group is None:
                continue
            blocks = self._namespace_blocks.get(group, [])
            if len(blocks) < 2:
                continue
            for sibling in blocks:
                if sibling.id != block.id:
                    self._add(member_name, fn_node, sibling)


# One introduction of a name: the byte span of the scope it governs, its kind,
# and the value it binds (the callable itself for a parameter).
JsIntroduction = tuple[int, int, str, Node | None]


# Scopes whose introductions stay indexed. A receiver lookup walks out from
# the call through every enclosing callable to the module, so the scopes in
# use at once are the current file's nesting chain plus the factory modules
# it reads; bounded because every entry keeps its parse tree alive.
_SCOPE_INDEX_CACHE_SIZE = 64


# Trees whose named functions stay indexed: the file being processed and the
# few modules its factories live in.
_FUNCTION_INDEX_CACHE_SIZE = 8


@lru_cache(maxsize=_SCOPE_INDEX_CACHE_SIZE)
def scope_introductions(
    scope: Node,
) -> Mapping[str, tuple[JsIntroduction, ...]]:
    """Every introduction owned by this callable (or module root), by name.

    Each with the byte span of the scope it governs; nested callables own
    their locals and are skipped. Built once per scope: answering one name
    per receiver rescanned the scope's whole body at every call site, and for
    a module-level receiver that is the module, or in a bundle the namespace
    IIFE around everything (issue #2453). Keyed on the scope node, which
    tree-sitter compares by tree, so a re-parsed file builds its own.
    """
    intros: defaultdict[str, list[JsIntroduction]] = defaultdict(list)
    if scope.type in cs.JS_TS_FUNCTION_NODES:
        param = (scope.start_byte, scope.end_byte, DECL_PARAM, scope)
        for name in _param_names(scope):
            intros[name].append(param)
    stack: list[Node] = list(scope.children)
    while stack:
        node = stack.pop()
        if node.type in cs.JS_TS_FUNCTION_NODES:
            continue
        if node.type == cs.TS_VARIABLE_DECLARATOR:
            _declarator_introduction(node, scope, intros)
        elif node.type == cs.TS_JS_FOR_IN_STATEMENT:
            _loop_introduction(node, scope, intros)
        elif node.type == cs.TS_JS_CATCH_CLAUSE:
            _catch_introduction(node, intros)
        stack.extend(node.children)
    return MappingProxyType({name: tuple(found) for name, found in intros.items()})


def innermost_enclosing(
    intros: tuple[JsIntroduction, ...], pos: int
) -> tuple[bool, tuple[str, Node | None] | None]:
    """Whether one scope's introductions decide the name read at `pos`, and how.

    Undecided when none of them encloses `pos`, so the next scope out is
    asked; decided with no answer when the innermost enclosing ones tie.
    """
    enclosing = [e for e in intros if e[0] <= pos < e[1]]
    if not enclosing:
        return False, None
    best_start = max(e[0] for e in enclosing)
    best = [e for e in enclosing if e[0] == best_start]
    if len(best) != 1:
        return True, None
    return True, (best[0][2], best[0][3])


def _loop_introduction(
    node: Node, scope: Node, intros: defaultdict[str, list[JsIntroduction]]
) -> None:
    left = node.child_by_field_name(cs.FIELD_LEFT)
    kind_node = node.child_by_field_name(cs.FIELD_KIND)
    if left is None or kind_node is None:
        return
    # `for (var x of xs)` hoists x to the FUNCTION; a read after the loop
    # still reads the loop variable.
    span_node = scope if kind_node.type == cs.TS_JS_VAR_KIND else node
    intro = (span_node.start_byte, span_node.end_byte, _DECL_OPAQUE, None)
    for name in _binding_names(left):
        intros[name].append(intro)


def _catch_introduction(
    node: Node, intros: defaultdict[str, list[JsIntroduction]]
) -> None:
    param = node.child_by_field_name(cs.FIELD_PARAMETER)
    if param is None:
        return
    intro = (node.start_byte, node.end_byte, _DECL_OPAQUE, None)
    for name in _binding_names(param):
        intros[name].append(intro)


def _declarator_introduction(
    node: Node, scope: Node, intros: defaultdict[str, list[JsIntroduction]]
) -> None:
    target = node.child_by_field_name(cs.FIELD_NAME)
    if target is None:
        return
    if target.type == cs.TS_IDENTIFIER:
        text = safe_decode_text(target)
        names = [text] if text else []
        kind = DECL_PLAIN
    else:
        names = _binding_names(target)
        kind = DECL_DESTRUCTURED
    if not names:
        return
    value = node.child_by_field_name(cs.FIELD_VALUE)
    if value is None:
        kind = _DECL_OPAQUE
    span_node = _declarator_span(node, scope)
    intro = (span_node.start_byte, span_node.end_byte, kind, value)
    for name in names:
        intros[name].append(intro)


def _declarator_span(node: Node, scope: Node) -> Node:
    # A `let`/`const` binds to the nearest block-like container below the
    # scope; a `var` to the whole scope.
    is_lexical = (
        node.parent is not None and node.parent.type == cs.TS_LEXICAL_DECLARATION
    )
    if not is_lexical:
        return scope
    current = node.parent
    while current is not None and current.id != scope.id:
        if current.type in _SCOPE_CONTAINER_TYPES:
            return current
        current = current.parent
    return scope


def _param_names(scope: Node) -> list[str]:
    names: dict[str, None] = {}
    for field in (cs.FIELD_PARAMETERS, cs.FIELD_PARAMETER):
        params = scope.child_by_field_name(field)
        if params is not None:
            names.update(dict.fromkeys(_binding_names(params)))
    return list(names)


def _binding_names(target: Node) -> list[str]:
    """Each name `target` binds, once, in the order the walk meets them.

    BINDING positions only: pattern defaults' right sides, computed keys and
    pair keys are reads of the enclosing scope.
    """
    names: dict[str, None] = {}
    stack: list[Node] = [target]
    while stack:
        node = stack.pop()
        node_type = node.type
        if node_type in (
            cs.TS_IDENTIFIER,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
        ):
            if name := safe_decode_text(node):
                names[name] = None
        elif node_type in (
            cs.TS_ASSIGNMENT_PATTERN,
            cs.TS_OBJECT_ASSIGNMENT_PATTERN,
        ):
            if (left := node.child_by_field_name(cs.FIELD_LEFT)) is not None:
                stack.append(left)
        elif node_type == cs.TS_PAIR_PATTERN:
            if (value := node.child_by_field_name(cs.FIELD_VALUE)) is not None:
                stack.append(value)
        elif node_type in (
            cs.TS_OBJECT_PATTERN,
            cs.TS_ARRAY_PATTERN,
            cs.TS_REST_PATTERN,
            cs.TS_JS_FORMAL_PARAMETERS,
            cs.TS_REQUIRED_PARAMETER,
            cs.TS_OPTIONAL_PARAMETER,
        ):
            stack.extend(node.named_children)
    return list(names)


@lru_cache(maxsize=_FUNCTION_INDEX_CACHE_SIZE)
def named_functions(root: Node) -> Mapping[str, Node]:
    """The first function each name declares in one tree, as the walk meets it.

    A function declaration, or the callable a declarator binds. Locating a
    factory by its qn walked the whole module tree at every call-result
    receiver it typed (issue #2453); the walk now runs once per tree, keyed
    on the root node, which tree-sitter compares by tree.
    """
    first: dict[str, Node] = {}
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if (named := _named_function(node)) is not None:
            first.setdefault(*named)
        stack.extend(node.children)
    return MappingProxyType(first)


def _named_function(node: Node) -> tuple[str, Node] | None:
    if node.type == cs.TS_FUNCTION_DECLARATION:
        name = node.child_by_field_name(cs.FIELD_NAME)
        text = safe_decode_text(name) if name is not None else None
        return (text, node) if text else None
    if node.type != cs.TS_VARIABLE_DECLARATOR:
        return None
    name = node.child_by_field_name(cs.FIELD_NAME)
    value = node.child_by_field_name(cs.FIELD_VALUE)
    if name is None or value is None or value.type not in cs.JS_TS_FUNCTION_NODES:
        return None
    text = safe_decode_text(name)
    return (text, value) if text else None
