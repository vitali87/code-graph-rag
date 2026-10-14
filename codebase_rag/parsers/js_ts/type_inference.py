from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache, lru_cache, partial
from types import MappingProxyType
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, NodeType
from ..utils import get_cached_query, safe_decode_text, unparenthesized
from . import utils as ut

# Callable node types whose bodies own their locals and returns; the shared
# JS_TS_FUNCTION_NODES tuple lacks the generator EXPRESSION form.
_JS_NESTED_CALLABLE_TYPES = frozenset(cs.JS_TS_FUNCTION_NODES) | {
    cs.TS_GENERATOR_FUNCTION
}

# Ancestor node types that bound a `let`/`const` declaration's scope: the
# nearest one governs the binding (a for-header declaration scopes to the for
# statement, a case-level one to the whole switch body).
_JS_SCOPE_CONTAINER_TYPES = frozenset(
    {
        cs.TS_STATEMENT_BLOCK,
        cs.TS_JS_SWITCH_BODY,
        cs.TS_JS_FOR_STATEMENT,
        cs.TS_JS_FOR_IN_STATEMENT,
    }
)

# Bindings a callable's body can introduce besides declarators, loops and
# catches: a function declaration at the body's top level is hoisted to the
# whole callable (one inside a block is block-scoped in strict code), a class
# declaration is scoped like `let`, and a named function or class EXPRESSION
# binds its name only inside itself.
_JS_HOISTED_DECLARATIONS = frozenset(
    {cs.TS_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION_DECLARATION}
)
_JS_BLOCK_SCOPED_DECLARATIONS = frozenset(
    {cs.TS_CLASS_DECLARATION, cs.TS_ABSTRACT_CLASS_DECLARATION}
)
_JS_SELF_NAMED_EXPRESSIONS = frozenset(
    {cs.TS_FUNCTION_EXPRESSION, cs.TS_GENERATOR_FUNCTION, cs.TS_CLASS_EXPRESSION}
)
_JS_CLASS_NODE_TYPES = frozenset(cs.JS_TS_CLASS_NODES) | _JS_BLOCK_SCOPED_DECLARATIONS
# TypeScript is compiled as strict code; a JS file is a module (always
# strict) when it has a top-level import or export.
_JS_STRICT_LANGUAGES = frozenset({cs.SupportedLanguage.TS, cs.SupportedLanguage.TSX})
_JS_MODULE_STATEMENTS = frozenset({cs.TS_IMPORT_STATEMENT, cs.TS_EXPORT_STATEMENT})

# Per callable: each name its own parameters, locals, loop and catch bindings
# and nested declarations introduce, with the byte spans where they bind it.
_JsBindingIndex = dict[str, list[tuple[int, int]]]

# The signature node an overload of each implementation kind is written as.
_OVERLOAD_SIGNATURE_TYPES = {
    cs.TS_METHOD_DEFINITION: cs.TS_METHOD_SIGNATURE,
    cs.TS_FUNCTION_DECLARATION: cs.TS_FUNCTION_SIGNATURE,
}

# Declarations of a name: (scope start_byte, scope end_byte, `new` value node
# when the declarator's own initialiser constructs, else None). Assignments:
# (site byte, `new` value node).
_CtorDecls = dict[str, list[tuple[int, int, "ASTNode | None"]]]
_CtorAssigns = dict[str, list[tuple[int, "ASTNode"]]]
_CtorBindingIndex = tuple[_CtorDecls, _CtorAssigns]

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries
    from ..import_processor import ImportProcessor

_JS_DECLARATOR_QUERY = "(variable_declarator) @declarator"


def _container_element(type_str: str | None) -> str | None:
    """The element inside a canonical ``list[<element>]`` marker, else ``None``."""
    if (
        type_str
        and type_str.startswith(cs.JS_LIST_TYPE_PREFIX)
        and type_str.endswith("]")
    ):
        return type_str[len(cs.JS_LIST_TYPE_PREFIX) : -1] or None
    return None


def _tree_root(node: ASTNode) -> ASTNode:
    while node.parent is not None:
        node = node.parent
    return node


def _find_function_declaration(root: ASTNode, name: str) -> ASTNode | None:
    """The single same-file free function by name, else ``None``.

    Matches a declaration or an arrow bound by a declarator (`const f =
    (): T => ...`, whose annotation rides the arrow node). Without lexical
    scope resolution, two same-named functions (a local shadowing a
    top-level one) cannot be attributed to a call site, so more than one
    match yields nothing rather than a possibly wrong binding.
    """
    matches = _function_declarations(root).get(name, ())
    return matches[0] if len(matches) == 1 else None


@lru_cache(maxsize=1)
def _function_declarations(root: ASTNode) -> Mapping[str, tuple[ASTNode, ...]]:
    """Every free function of one tree by name, in document order.

    Each `for (x of f())` looked `f` up by walking the whole module tree, and
    the loop is typed once per enclosing function, so a file of N such loops
    cost O(N^2): 830 s for 2,000 functions (issue #2453). The walk now runs
    once per tree. One slot, because every lookup names the tree of the file
    being processed; keyed on the root node, which tree-sitter compares by
    tree, so a re-parsed file builds its own.
    """
    found: dict[str, list[ASTNode]] = {}
    stack: list[ASTNode] = [root]
    while stack:
        current = stack.pop()
        if declared := _declared_function(current):
            name, function = declared
            found.setdefault(name, []).append(function)
        stack.extend(reversed(current.children))
    return MappingProxyType({name: tuple(nodes) for name, nodes in found.items()})


def _declared_function(node: ASTNode) -> tuple[str, ASTNode] | None:
    """The name and callable `node` declares as a free function, if any."""
    if node.type in (
        cs.TS_FUNCTION_DECLARATION,
        cs.TS_GENERATOR_FUNCTION_DECLARATION,
    ):
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        name = safe_decode_text(name_node) if name_node is not None else None
        return (name, node) if name else None
    if node.type != cs.TS_VARIABLE_DECLARATOR:
        return None
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    value_node = node.child_by_field_name(cs.FIELD_VALUE)
    if (
        name_node is None
        or value_node is None
        or value_node.type != cs.TS_ARROW_FUNCTION
    ):
        return None
    name = safe_decode_text(name_node)
    return (name, value_node) if name else None


def _own_return_values(callable_node: ASTNode) -> list[ASTNode | None]:
    """What each of the callable's own returns hands back, parentheses
    dropped; ``None`` for a bare `return;`. An arrow's expression body is
    its one return value."""
    body = callable_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return []
    if body.type != cs.TS_STATEMENT_BLOCK:
        return [unparenthesized(body)]
    returns: list[ASTNode] = []
    ut.find_return_statements(body, returns)
    return [
        unparenthesized(next(iter(node.named_children), None))
        for node in returns
        if JsTypeInferenceEngine._return_belongs_to(node, callable_node)
    ]


def _overload_signatures(callable_node: ASTNode) -> list[ASTNode]:
    """The overload signatures written for this implementation beside it:
    in its class body, or in its module or block (each may be exported).

    A static and an instance method of one name are two members, each with
    its own overloads, so only signatures of the same staticness count.
    """
    signature_type = _OVERLOAD_SIGNATURE_TYPES.get(callable_node.type)
    static = ut.is_static_member(callable_node)
    name = safe_decode_text(callable_node.child_by_field_name(cs.FIELD_NAME))
    scope = callable_node.parent
    if scope is not None and scope.type == cs.TS_EXPORT_STATEMENT:
        scope = scope.parent
    if signature_type is None or not name or scope is None:
        return []
    return [
        member
        for child in scope.named_children
        if (member := _exported_declaration(child)).type == signature_type
        and safe_decode_text(member.child_by_field_name(cs.FIELD_NAME)) == name
        and ut.is_static_member(member) == static
    ]


def _method_in_body(
    class_body: ASTNode, method_name: str, static: bool
) -> ASTNode | None:
    """The class's own `method_name` body with the wanted staticness: a
    static and an instance method may share the name."""
    return next(
        (
            member
            for member in class_body.named_children
            if member.type == cs.TS_METHOD_DEFINITION
            and safe_decode_text(member.child_by_field_name(cs.FIELD_NAME))
            == method_name
            and ut.is_static_member(member) == static
        ),
        None,
    )


def _member_name(member: ASTNode) -> str | None:
    # A TS class field keeps its key in `name`, a JS one in `property`.
    key = member.child_by_field_name(cs.FIELD_NAME) or member.child_by_field_name(
        cs.FIELD_PROPERTY
    )
    return safe_decode_text(key) if key is not None else None


def _single_non_nullish_member(union: ASTNode) -> ASTNode | None:
    members = [
        child
        for child in union.named_children
        if (safe_decode_text(child) or "") not in cs.TS_NULLISH_TYPE_TEXTS
    ]
    return members[0] if len(members) == 1 else None


def _exported_declaration(node: ASTNode) -> ASTNode:
    if node.type == cs.TS_EXPORT_STATEMENT and (
        declaration := node.child_by_field_name(cs.FIELD_DECLARATION)
    ):
        return declaration
    return node


def _this_value_of(callable_node: ASTNode) -> str | None:
    # `this` in a class's instance method is an instance of the receiver's
    # class; in a static one it is the receiver class itself (a subclass's
    # when called through it). Only a class member's `this` is either.
    parent = callable_node.parent
    if (
        callable_node.type != cs.TS_METHOD_DEFINITION
        or parent is None
        or parent.type != cs.TS_CLASS_BODY
    ):
        return None
    if ut.is_static_member(callable_node):
        return cs.JS_RECEIVER_CLASS
    return cs.JS_RECEIVER_INSTANCE


def _js_await_call(arguments: ASTNode) -> ASTNode | None:
    # The call whose argument list this is, when that call is the grammar's
    # parse of `await (X)()`: a call to an identifier spelled `await`.
    call = arguments.parent
    if call is None or call.type != cs.TS_CALL_EXPRESSION:
        return None
    func = call.child_by_field_name(cs.FIELD_FUNCTION)
    if (
        func is not None
        and func.type == cs.TS_IDENTIFIER
        and safe_decode_text(func) == cs.JS_AWAIT_IDENTIFIER
    ):
        return call
    return None


class JsTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "project_name",
        "_find_method_ast_node",
        "_queries",
        "_binding_root",
        "_binding_module_strict",
        "_binding_indexes",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
        find_method_ast_node_func: Callable[[str], ASTNode | None],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries] | None = None,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name
        self._find_method_ast_node = find_method_ast_node_func
        self._queries = queries
        # Binding indexes of the callables in ONE parsed file. Holding that
        # file's root keeps its tree alive, so a node id can never be
        # recycled into a newer parse that the stale indexes would answer.
        self._binding_root: ASTNode | None = None
        self._binding_module_strict = False
        self._binding_indexes: dict[tuple[int, int], _JsBindingIndex] = {}

    def _get_declarators_via_query(
        self, caller_node: ASTNode, language: cs.SupportedLanguage | None = None
    ) -> list[Node] | None:
        if self._queries is None:
            return None
        # sorted: frozenset order varies across runs (str-hash randomization) and
        # the first language with queries wins, so keep it deterministic.
        langs = [language] if language is not None else sorted(cs.JS_TS_LANGUAGES)
        for lang in langs:
            lang_queries = self._queries.get(lang)
            if lang_queries and "language" in lang_queries:
                try:
                    q = get_cached_query(lang_queries["language"], _JS_DECLARATOR_QUERY)
                    cursor = QueryCursor(q)
                    captures = cursor.captures(caller_node)
                    return captures.get("declarator", [])
                except Exception:  # noqa: S112 - a failed query falls through to the next language
                    continue
        return None

    def build_local_variable_type_map(
        self,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}
        declarator_count = 0
        # Seeded first so a declarator that rebinds the name wins.
        self._seed_interface_parameters(caller_node, local_var_types, module_qn)

        declarator_nodes = self._get_declarators_via_query(caller_node, language)
        if declarator_nodes is not None:
            for current in declarator_nodes:
                declarator_count += 1
                self._record_declarator(current, local_var_types, module_qn, language)
        else:
            stack: list[ASTNode] = [caller_node]
            while stack:
                current = stack.pop()
                if current.type == cs.TS_VARIABLE_DECLARATOR:
                    declarator_count += 1
                    self._record_declarator(
                        current, local_var_types, module_qn, language
                    )
                stack.extend(reversed(current.children))

        # After declarators so a loop over an already-typed variable can read
        # its container marker.
        self._seed_for_of_variables(caller_node, local_var_types, module_qn)

        logger.debug(
            ls.JS_VAR_TYPE_MAP_BUILT,
            count=len(local_var_types),
            declarator_count=declarator_count,
        )
        return local_var_types

    def _seed_interface_parameters(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # `use(r: Repo)` calling `r.find()` (issue #2524): the receiver's only
        # type is its annotation, and without it the call fell to the name-only
        # member gate, which cannot choose between the interface method and its
        # implementations. Only a first-party INTERFACE is seeded: any other
        # annotation keeps the untyped fallback it had, since a typed receiver
        # the resolver cannot place is treated as external and dropped.
        for name, type_name in ut.annotated_parameter_types(caller_node):
            if interface_qn := self._first_party_interface_qn(type_name, module_qn):
                local_var_types[name] = interface_qn

    def _first_party_interface_qn(self, type_name: str, module_qn: str) -> str | None:
        # An import names the symbol or its module, so both spellings are tried
        # before a same-file declaration.
        imported = self.import_processor.import_mapping.get(module_qn, {}).get(
            type_name
        )
        candidates = [f"{module_qn}{cs.SEPARATOR_DOT}{type_name}"]
        if imported:
            candidates = [
                imported,
                f"{imported}{cs.SEPARATOR_DOT}{type_name}",
                *candidates,
            ]
        return next(
            (
                qn
                for qn in candidates
                if self.function_registry.get(qn) == NodeType.INTERFACE
            ),
            None,
        )

    def _record_declarator(
        self,
        declarator: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> None:
        """One declarator's type: the annotation is declared truth and wins
        over value inference; an annotation-only ``let u: User;`` still types.
        """
        name_node = declarator.child_by_field_name("name")
        if name_node is None or not name_node.text:
            return
        var_name = safe_decode_text(name_node)
        if var_name is None:
            return
        logger.debug(ls.JS_VAR_DECLARATOR_FOUND, var_name=var_name, module_qn=module_qn)
        # Precedence: a `new` expression is exact runtime truth and beats the
        # declared annotation (`const s: IService = new UserService()` types as
        # the concrete class); the annotation beats the remaining value
        # heuristics; an annotation-only `let u: User;` still types.
        value_node = declarator.child_by_field_name("value")
        var_type = None
        if value_node is not None and value_node.type == cs.TS_NEW_EXPRESSION:
            var_type = self._infer_js_variable_type_from_value(
                value_node, module_qn, language
            )
        if var_type is None and (type_node := declarator.child_by_field_name("type")):
            var_type = self._annotation_type(type_node, module_qn)
        if var_type is None and value_node is not None:
            var_type = self._infer_js_variable_type_from_value(
                value_node, module_qn, language
            )
        if var_type:
            local_var_types[var_name] = var_type
            logger.debug(ls.JS_VAR_INFERRED, var_name=var_name, var_type=var_type)
        else:
            logger.debug(ls.JS_VAR_INFER_FAILED, var_name=var_name)

    def _annotation_type(self, annotation_node: ASTNode, module_qn: str) -> str | None:
        """The type of a ``type_annotation`` node (`: User`, `: User[]`, ...)."""
        inner = next(iter(annotation_node.named_children), None)
        return self._type_from_type_node(inner, module_qn) if inner else None

    def _type_from_type_node(self, node: ASTNode, module_qn: str) -> str | None:
        if node.type == cs.TS_UNION_TYPE:
            return self._union_member_type(node, module_qn)
        if node.type == cs.TS_ARRAY_TYPE:
            inner = next(iter(node.named_children), None)
            element = self._type_from_type_node(inner, module_qn) if inner else None
            return self._element_marker(element)
        if node.type == cs.TS_GENERIC_TYPE:
            return self._generic_type(node, module_qn)
        if node.type == cs.TS_TYPE_IDENTIFIER:
            name = safe_decode_text(node)
            if not name:
                return None
            return self._resolve_js_class_name(name, module_qn) or name
        if node.type == cs.TS_NESTED_TYPE_IDENTIFIER:
            return safe_decode_text(node)
        return None

    def _union_member_type(self, node: ASTNode, module_qn: str) -> str | None:
        # `User | null` names a single concrete member; wider unions are
        # not a receiver type.
        members = [
            child
            for child in node.named_children
            if (safe_decode_text(child) or "") not in cs.TS_NULLISH_TYPE_TEXTS
        ]
        if len(members) == 1:
            return self._type_from_type_node(members[0], module_qn)
        return None

    def _generic_type(self, node: ASTNode, module_qn: str) -> str | None:
        """`Array<User>` and friends carry an element; other generics do not."""
        name_node = node.child_by_field_name("name")
        if (
            name_node is None
            or (safe_decode_text(name_node) or "") not in cs.TS_ARRAY_GENERIC_NAMES
        ):
            return None
        arguments = next(
            (child for child in node.named_children if child != name_node), None
        )
        if arguments is None:
            return None
        argument_types = list(arguments.named_children)
        if len(argument_types) != 1:
            return None
        return self._element_marker(
            self._type_from_type_node(argument_types[0], module_qn)
        )

    @staticmethod
    def _element_marker(element: str | None) -> str | None:
        # Nested containers (`User[][]`) have no scalar element to iterate to.
        if element is None or element.startswith(cs.JS_LIST_TYPE_PREFIX):
            return None
        return cs.JS_LIST_TYPE_FORMAT.format(element=element)

    def _seed_for_of_variables(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        stack: list[ASTNode] = [caller_node]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_JS_FOR_IN_STATEMENT:
                self._seed_one_for_of(node, local_var_types, module_qn)
            stack.extend(reversed(node.children))

    def _seed_one_for_of(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        operator = node.child_by_field_name("operator")
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if (
            operator is None
            or left is None
            or right is None
            or safe_decode_text(operator) != cs.TS_JS_OPERATOR_OF
            or left.type != cs.TS_IDENTIFIER
        ):
            return
        loop_var = safe_decode_text(left)
        if not loop_var:
            return
        element = self._for_of_element_type(right, local_var_types, module_qn)
        # The map is function-wide while the loop binding is block-scoped: the
        # header REBINDS the name, so an entry that disagrees with the loop's
        # element (or an element the engine cannot type) would emit wrong
        # edges on one side of the loop. Dropping it beats guessing.
        existing = local_var_types.get(loop_var)
        if existing is not None and existing != element:
            del local_var_types[loop_var]
            return
        if element:
            local_var_types[loop_var] = element
            logger.debug(ls.JS_VAR_INFERRED, var_name=loop_var, var_type=element)

    def _for_of_element_type(
        self, right: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if right.type == cs.TS_IDENTIFIER:
            name = safe_decode_text(right)
            return _container_element(local_var_types.get(name)) if name else None
        if right.type == cs.TS_CALL_EXPRESSION:
            # A scalar return is not an element type; only the marker unwraps.
            return _container_element(self._call_return_type(right, module_qn))
        if right.type == cs.TS_ARRAY:
            return self._array_literal_element_type(right, module_qn)
        return None

    def _array_literal_element_type(
        self, array_node: ASTNode, module_qn: str
    ) -> str | None:
        """`[new Widget(), new Widget()]` names its element; a literal whose
        elements construct different classes, or mix constructions with other
        expressions, guarantees nothing and yields ``None``."""
        element: str | None = None
        for child in array_node.named_children:
            if child.type != cs.TS_NEW_EXPRESSION:
                return None
            class_name = ut.extract_constructor_name(child)
            if not class_name:
                return None
            resolved = self._resolve_js_class_name(class_name, module_qn) or class_name
            if element is None:
                element = resolved
            elif element != resolved:
                return None
        return element

    def _call_return_type(self, call_node: ASTNode, module_qn: str) -> str | None:
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            return None
        if func_node.type == cs.TS_IDENTIFIER:
            # The shared method lookup only knows `Class.method` shapes, so a
            # free function is located in its own file's tree (imported
            # functions stay out of reach without AST access; honest miss).
            callee = safe_decode_text(func_node)
            if not callee:
                return None
            fn_node = _find_function_declaration(_tree_root(call_node), callee)
            if fn_node is None:
                return None
            fn_qn = f"{module_qn}{cs.SEPARATOR_DOT}{callee}"
            return self._annotated_return_type_of(
                fn_node, module_qn
            ) or self._analyze_return_statements(fn_node, fn_qn)
        if func_node.type == cs.TS_MEMBER_EXPRESSION and (
            method_call := ut.extract_method_call(func_node)
        ):
            return self._infer_js_method_return_type(method_call, module_qn)
        return None

    def _annotated_return_type_of(
        self, callable_node: ASTNode, module_qn: str
    ) -> str | None:
        annotation = callable_node.child_by_field_name("return_type")
        return self._annotation_type(annotation, module_qn) if annotation else None

    @staticmethod
    def static_member(class_body: ASTNode, member_name: str) -> tuple[bool, int | None]:
        """Whether the class declares a static `member_name` of its own, and
        the line of its body when that member is a plain method.

        Any own static of the name (a field, a getter or setter too) hides
        a parent's, so a caller walking up `extends` stops at it; one with no
        method body to read answers no line.
        """
        declared = False
        for member in class_body.named_children:
            if not ut.is_static_member(member) or _member_name(member) != member_name:
                continue
            declared = True
            if member.type == cs.TS_METHOD_SIGNATURE:
                continue
            if member.type == cs.TS_METHOD_DEFINITION and not any(
                child.type in cs.TS_ACCESSOR_KEYWORDS for child in member.children
            ):
                return True, member.start_point[0] + 1
            return True, None
        return declared, None

    def visible_class(
        self,
        site: ASTNode,
        name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str | None:
        """The class `name` written at `site` refers to: the module's own or
        an imported one, unless a parameter, local or nested declaration
        around `site` rebinds it. Nothing outside the module's own scope is
        searched, so a name it does not bind names no class."""
        if self._js_bound_by_enclosing_callable(site, name, language):
            return None
        return self._resolve_js_class_name(name, module_qn)

    def callable_return_type(
        self,
        root: ASTNode,
        path: list[str],
        module_qn: str,
        language: cs.SupportedLanguage,
        static: bool,
    ) -> str | None:
        """`path`: the callable's qn segments below its module, a method's
        last one following its class's path, a function's alone. `static`:
        the call reaches the class's static member of that name, not its
        instance one."""
        if len(path) == 1:
            callable_node = _find_function_declaration(root, path[0])
        else:
            body = ut.find_class_body_at(root, path[:-1])
            callable_node = _method_in_body(body, path[-1], static) if body else None
        if callable_node is None:
            return None
        return self._chain_return_type(callable_node, module_qn, language)

    def _chain_return_type(
        self,
        callable_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str | None:
        """The class a call to this callable evaluates to, read in its own
        module, so the next hop of a fluent chain can bind (issue #2468).

        A declared return is the answer when it names one class; with none,
        every return must hand back `this`, or every one construct the same
        class. A `this` return (or `: this`) is answered with a receiver
        marker the caller binds to the class it called through. Anything else
        names no class, and the chain ends there rather than guess.
        """
        if any(
            child.type in cs.TS_CHAIN_OPAQUE_CALLABLE_KEYWORDS
            for child in callable_node.children
        ):
            return None
        this_value = _this_value_of(callable_node)
        if signatures := _overload_signatures(callable_node):
            # A caller sees only the overloads, never the implementation's
            # own signature; `pick(1)` may be a B where the body builds an A.
            declared = {
                self._declared_chain_type(annotation, this_value, module_qn)
                if (annotation := sig.child_by_field_name(cs.FIELD_RETURN_TYPE))
                else None
                for sig in signatures
            }
            return declared.pop() if len(declared) == 1 else None
        annotation = callable_node.child_by_field_name(cs.FIELD_RETURN_TYPE)
        if annotation is not None:
            return self._declared_chain_type(annotation, this_value, module_qn)
        return self._evident_return_class(
            callable_node, this_value, module_qn, language
        )

    def _declared_chain_type(
        self, annotation: ASTNode, this_value: str | None, module_qn: str
    ) -> str | None:
        declared = next(iter(annotation.named_children), None)
        return (
            self._chain_type_of(declared, this_value, module_qn)
            if declared is not None
            else None
        )

    def _chain_type_of(
        self, declared: ASTNode, this_value: str | None, module_qn: str
    ) -> str | None:
        if declared.type == cs.TS_THIS_TYPE:
            # The `this` type names an instance; TS allows it in no static.
            return this_value if this_value == cs.JS_RECEIVER_INSTANCE else None
        if declared.type == cs.TS_UNION_TYPE:
            # `Product | undefined` names its one class.
            member = _single_non_nullish_member(declared)
            return (
                self._chain_type_of(member, this_value, module_qn)
                if member is not None
                else None
            )
        name_node = (
            declared.child_by_field_name(cs.FIELD_NAME)
            if declared.type == cs.TS_GENERIC_TYPE
            else declared
        )
        if name_node is None or name_node.type != cs.TS_TYPE_IDENTIFIER:
            return self._type_from_type_node(declared, module_qn)
        # `Builder<T>` is a Builder; an array-shaped generic is a container,
        # and `Promise<T>` names no first-party class.
        name = safe_decode_text(name_node)
        if not name or name in cs.TS_ARRAY_GENERIC_NAMES:
            return None
        return self._scoped_class(name_node, name, module_qn)

    def _scoped_class(self, site: ASTNode, name: str, module_qn: str) -> str | None:
        """The class `name` read at `site` is, innermost scope first: a class
        declared in an enclosing function, method or namespace (by its own
        qn), else the module's own or an imported one. A nearer binding that
        is no class, or one a qn cannot name, answers nothing rather than
        letting the module-level class of that name stand in."""
        declaration = ut.enclosing_type_declaration(site, name)
        if declaration is None:
            return self._resolve_js_class_name(name, module_qn)
        return self._declared_class_qn(declaration, module_qn)

    @staticmethod
    def _declared_class_qn(declaration: ASTNode, module_qn: str) -> str | None:
        path = ut.class_qn_path(declaration)
        return cs.SEPARATOR_DOT.join([module_qn, *path]) if path else None

    def _constructed_class(
        self, value: ASTNode, module_qn: str, language: cs.SupportedLanguage
    ) -> str | None:
        constructor = value.child_by_field_name(cs.FIELD_CONSTRUCTOR)
        name = (
            safe_decode_text(constructor)
            if constructor is not None and constructor.type == cs.TS_IDENTIFIER
            else None
        )
        if constructor is None or not name:
            return self._infer_js_variable_type_from_value(value, module_qn, language)
        # A value position: the nearest value binding of the name wins. The
        # block walk finds the nearest declaration; a parameter, hoisted
        # `var`, catch or loop binding in a callable between the call and
        # that declaration is nearer still, and is no class.
        declaration = ut.enclosing_value_declaration(constructor, name)
        limit = ut.declaring_block(declaration) if declaration is not None else None
        if self._js_bound_by_enclosing_callable(value, name, language, limit):
            return None
        if declaration is None:
            return self._resolve_js_class_name(name, module_qn)
        return self._declared_class_qn(declaration, module_qn)

    def _evident_return_class(
        self,
        callable_node: ASTNode,
        this_value: str | None,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str | None:
        values = _own_return_values(callable_node)
        if not values or None in values:
            return None
        if all(value.type == cs.TS_THIS for value in values if value is not None):
            return this_value
        constructed = {
            self._constructed_class(value, module_qn, language)
            if value.type == cs.TS_NEW_EXPRESSION
            else None
            for value in values
            if value is not None
        }
        return constructed.pop() if len(constructed) == 1 else None

    def _infer_js_variable_type_from_value(
        self,
        value_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        logger.debug(ls.JS_INFER_VALUE_NODE, node_type=value_node.type)

        if value_node.type == cs.TS_NEW_EXPRESSION:
            if class_name := ut.extract_constructor_name(value_node):
                if self._js_bound_by_enclosing_callable(
                    value_node, class_name, language
                ):
                    logger.debug(ls.JS_CTOR_LOCALLY_BOUND, class_name=class_name)
                    return None
                class_qn = self._resolve_js_class_name(class_name, module_qn)
                return class_qn or class_name

        elif value_node.type == cs.TS_CALL_EXPRESSION and (
            call_type := self._infer_js_call_value_type(value_node, module_qn)
        ):
            return call_type

        logger.debug(ls.JS_NO_PATTERN_MATCHED, node_type=value_node.type)
        return None

    def _infer_js_call_value_type(
        self, value_node: ASTNode, module_qn: str
    ) -> str | None:
        # `obj.method()` takes the method's inferred return type; a bare
        # `factory()` is typed by the callee's own name.
        func_node = value_node.child_by_field_name("function")
        func_type = func_node.type if func_node else cs.STR_NONE
        logger.debug(ls.JS_CALL_EXPR_FUNC_NODE, func_type=func_type)
        if func_node is None:
            return None
        if func_node.type == cs.TS_MEMBER_EXPRESSION:
            return self._infer_js_member_call_type(func_node, module_qn)
        if func_node.type == cs.TS_IDENTIFIER and func_node.text:
            return safe_decode_text(func_node)
        return None

    def _infer_js_member_call_type(
        self, func_node: ASTNode, module_qn: str
    ) -> str | None:
        method_call_text = ut.extract_method_call(func_node)
        logger.debug(ls.JS_EXTRACTED_METHOD_CALL, method_call=method_call_text)
        if not method_call_text:
            return None
        if inferred_type := self._infer_js_method_return_type(
            method_call_text, module_qn
        ):
            logger.debug(
                ls.JS_TYPE_INFERRED,
                method_call=method_call_text,
                inferred_type=inferred_type,
            )
            return inferred_type
        logger.debug(ls.JS_RETURN_TYPE_INFER_FAILED, method_call=method_call_text)
        return None

    def _infer_js_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            logger.debug(ls.JS_METHOD_CALL_INVALID, method_call=method_call)
            return None

        class_name, method_name = parts

        class_qn = self._resolve_js_class_name(class_name, module_qn)
        if not class_qn:
            logger.debug(
                ls.JS_CLASS_RESOLVE_FAILED, class_name=class_name, module_qn=module_qn
            )
            return None

        logger.debug(ls.JS_CLASS_RESOLVED, class_name=class_name, class_qn=class_qn)

        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        logger.debug(ls.JS_LOOKING_FOR_METHOD, method_qn=method_qn)

        method_node = self._find_method_ast_node(method_qn)
        if not method_node:
            logger.debug(ls.JS_METHOD_AST_NOT_FOUND, method_qn=method_qn)
            return None

        # A declared return annotation is the cheapest, most reliable source;
        # body analysis is the fallback (issue #1303).
        return_type = self._annotated_return_type_of(
            method_node, module_qn
        ) or self._analyze_return_statements(method_node, method_qn)
        logger.debug(
            ls.JS_RETURN_ANALYZED, method_qn=method_qn, return_type=return_type
        )
        return return_type

    def _resolve_js_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]

                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                if (
                    full_class_qn in self.function_registry
                    and self.function_registry[full_class_qn] == NodeType.CLASS
                ):
                    return full_class_qn

                return imported_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == NodeType.CLASS
        ):
            return local_class_qn

        return None

    def _get_language_obj(self) -> object | None:
        if self._queries is None:
            return None
        for lang in sorted(cs.JS_TS_LANGUAGES):
            lang_queries = self._queries.get(lang)
            if lang_queries and "language" in lang_queries:
                return lang_queries["language"]
        return None

    def _analyze_return_statements(
        self, method_node: ASTNode, method_qn: str
    ) -> str | None:
        return_nodes: list[ASTNode] = []
        ut.find_return_statements(method_node, return_nodes, self._get_language_obj())

        # One O(body) scan shared by every `return <identifier>` in this
        # method, built on first use; deliberately NOT stored on the engine: a
        # tree-sitter node id is a recycled heap address across parses, and
        # holding Node values would pin whole trees against the bounded AST
        # cache.
        ctor_index = cache(partial(self._js_ctor_binding_index, method_node))

        for return_node in return_nodes:
            # Nested callables own their returns: a callback's `return new
            # Foo()` (or `return x`) is the callback's value, never the
            # method's. The ONE exception is an IIFE whose call value the
            # method itself returns (`return (function () { return new C()
            # })()`): its direct-expression returns ARE the method's value.
            owner_is_method = self._return_belongs_to(return_node, method_node)
            if not owner_is_method and not self._iife_return_of(
                return_node, method_node
            ):
                continue
            if inferred := self._return_node_type(
                return_node, method_qn, owner_is_method, ctor_index
            ):
                return inferred

        return None

    def _return_node_type(
        self,
        return_node: ASTNode,
        method_qn: str,
        owner_is_method: bool,
        ctor_index: Callable[[], _CtorBindingIndex],
    ) -> str | None:
        for child in return_node.children:
            if child.type == cs.TS_RETURN:
                continue

            if inferred_type := ut.analyze_return_expression(child, method_qn):
                return inferred_type

            if not owner_is_method:
                # An IIFE's `return x` reads the IIFE's OWN locals; the
                # method-body binding index says nothing about them.
                continue

            # `return x` where the METHOD'S OWN body binds `x = new C(...)`
            # (the cache-then-construct factory, fastify's ContentType.from,
            # issue #992): the CONSTRUCTED class types the return when every
            # construction reaching THIS return's variable agrees. Bindings
            # resolve by SCOPE SPAN: the innermost declarator whose scope
            # encloses the return is the variable, so a nested-block shadow
            # neither erases an outer construction nor inherits it.
            # Unknown-value assignments (the cache hit) do not veto; a second
            # DIFFERENT class does.
            if child.type != cs.TS_IDENTIFIER or not (name := safe_decode_text(child)):
                continue
            if resolved := self._constructed_return_type(
                name, child.start_byte, ctor_index(), method_qn
            ):
                return resolved
        return None

    def _constructed_return_type(
        self,
        name: str,
        start_byte: int,
        ctor_index: _CtorBindingIndex,
        method_qn: str,
    ) -> str | None:
        constructed = self._js_constructed_for(name, start_byte, ctor_index)
        if constructed is None:
            return None
        ctor = ut.extract_constructor_name(constructed)
        if not ctor:
            return None
        own_qn = ut.analyze_return_expression(constructed, method_qn)
        own_leaf = own_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] if own_qn else None
        # analyze_return_expression resolves a NEW to the method's OWN class;
        # keep that qn precision only when the constructed class IS the own
        # class. Otherwise resolve the constructed class in the FACTORY'S
        # module (where the construction names it), falling back to the bare
        # name.
        if ctor == own_leaf:
            return own_qn
        if own_qn and cs.SEPARATOR_DOT in own_qn:
            factory_module = own_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if resolved := self._resolve_js_class_name(ctor, factory_module):
                return resolved
        return ctor

    @staticmethod
    def _return_belongs_to(return_node: ASTNode, method_node: ASTNode) -> bool:
        current = return_node.parent
        while current is not None:
            if current.type in _JS_NESTED_CALLABLE_TYPES:
                return current.id == method_node.id
            current = current.parent
        return False

    def _iife_return_of(self, return_node: ASTNode, method_node: ASTNode) -> bool:
        # True when the return's owning callable is immediately invoked and
        # the call's value is returned by the method itself, so the inner
        # return IS the method's return value. A callback ARGUMENT never
        # qualifies: its owner is not the callee of any enclosing call.
        owner = self._js_owning_callable(return_node)
        if owner is None or owner.id == method_node.id:
            return False
        call = self._js_enclosing_invocation(owner)
        if call is None:
            return False
        consumer = self._js_value_consumer_return(call)
        return consumer is not None and self._return_belongs_to(consumer, method_node)

    @staticmethod
    def _js_owning_callable(return_node: ASTNode) -> ASTNode | None:
        current = return_node.parent
        while current is not None:
            if current.type in _JS_NESTED_CALLABLE_TYPES:
                return current
            current = current.parent
        return None

    @staticmethod
    def _js_enclosing_invocation(owner: ASTNode) -> ASTNode | None:
        # Climb value-transparent wrappers; at a call, require the wrapped
        # owner to BE the callee (not an argument, not one operand of many).
        # The one argument-position exception: the JS grammar parses
        # `await (X)()` as a call to an identifier spelled `await` with X as
        # its sole argument, so that call IS a transparent await of X.
        node = owner
        current = owner.parent
        while current is not None:
            if (
                current.type == cs.TS_PARENTHESIZED_EXPRESSION
                or current.type in cs.TS_CAST_WRAPPER_TYPES
            ):
                node = current
                current = current.parent
                continue
            if current.type == cs.TS_ARGUMENTS:
                call = _js_await_call(current)
                if call is None:
                    return None
                node = call
                current = call.parent
                continue
            if current.type == cs.TS_CALL_EXPRESSION:
                func = current.child_by_field_name(cs.FIELD_FUNCTION)
                if func is not None and func.id == node.id:
                    return current
                return None
            return None
        return None

    @staticmethod
    def _js_value_consumer_return(call: ASTNode) -> ASTNode | None:
        current = call.parent
        while current is not None:
            if (
                current.type == cs.TS_PARENTHESIZED_EXPRESSION
                or current.type in cs.TS_CAST_WRAPPER_TYPES
                or current.type == cs.TS_AWAIT_EXPRESSION
            ):
                current = current.parent
                continue
            if current.type == cs.TS_RETURN_STATEMENT:
                return current
            return None
        return None

    def _js_ctor_binding_index(self, method_node: ASTNode) -> _CtorBindingIndex:
        # One scan of the method body (nested callables excluded: their
        # locals are their own) collecting every DECLARATION of every name
        # with the byte span of the scope it governs, plus every
        # `x = new C(...)` assignment site. Scope spans, not name equality,
        # decide which construction reaches which return.
        decls: _CtorDecls = {}
        assigns: _CtorAssigns = {}
        stack: list[ASTNode] = list(method_node.children)
        while stack:
            node = stack.pop()
            if node.type in _JS_NESTED_CALLABLE_TYPES:
                continue
            if node.type == cs.TS_VARIABLE_DECLARATOR:
                self._index_ctor_declarator(node, method_node, decls)
            elif node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                self._index_ctor_assignment(node, assigns)
            elif node.type == cs.TS_JS_FOR_IN_STATEMENT:
                self._index_ctor_loop_binding(node, method_node, decls)
            elif node.type == cs.TS_JS_CATCH_CLAUSE:
                self._index_ctor_catch_binding(node, decls)
            stack.extend(node.children)
        return decls, assigns

    def _index_ctor_declarator(
        self, node: ASTNode, method_node: ASTNode, decls: _CtorDecls
    ) -> None:
        target = node.child_by_field_name(cs.FIELD_NAME)
        if target is None:
            return
        value = node.child_by_field_name(cs.FIELD_VALUE)
        ctor_value = (
            value if value is not None and value.type == cs.TS_NEW_EXPRESSION else None
        )
        is_lexical = (
            node.parent is not None and node.parent.type == cs.TS_LEXICAL_DECLARATION
        )
        span = (
            self._js_scope_span(node, method_node)
            if is_lexical
            else (method_node.start_byte, method_node.end_byte)
        )
        # A destructuring pattern introduces its names with an unknowable
        # value (the construction cannot be attributed to one of them).
        single = target.type == cs.TS_IDENTIFIER
        for bound_name in self._js_binding_names(target):
            decls.setdefault(bound_name, []).append(
                (span[0], span[1], ctor_value if single else None)
            )

    @staticmethod
    def _index_ctor_assignment(node: ASTNode, assigns: _CtorAssigns) -> None:
        target = node.child_by_field_name(cs.FIELD_LEFT)
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if (
            target is not None
            and value is not None
            and target.type == cs.TS_IDENTIFIER
            and value.type == cs.TS_NEW_EXPRESSION
            and (bound_name := safe_decode_text(target))
        ):
            assigns.setdefault(bound_name, []).append((node.start_byte, value))

    def _index_ctor_loop_binding(
        self, node: ASTNode, method_node: ASTNode, decls: _CtorDecls
    ) -> None:
        # `for (const x of xs)` binds x over the loop; `for (var x of xs)`
        # hoists it; `for (x of xs)` (no kind) assigns an existing binding
        # and introduces nothing.
        kind = node.child_by_field_name(cs.FIELD_KIND)
        if kind is None:
            return
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        span = (
            (method_node.start_byte, method_node.end_byte)
            if kind.type == cs.TS_JS_VAR_KIND
            else (node.start_byte, node.end_byte)
        )
        for bound_name in self._js_binding_names(left):
            decls.setdefault(bound_name, []).append((span[0], span[1], None))

    def _index_ctor_catch_binding(self, node: ASTNode, decls: _CtorDecls) -> None:
        param = node.child_by_field_name(cs.FIELD_PARAMETER)
        if param is None:
            return
        for bound_name in self._js_binding_names(param):
            decls.setdefault(bound_name, []).append(
                (node.start_byte, node.end_byte, None)
            )

    @staticmethod
    def _js_binding_names(target: ASTNode) -> list[str]:
        # Only BINDING positions introduce names: a pattern default's right
        # side, a computed key's expression, and a pair's key are READS of
        # the enclosing scope and must not shadow it.
        names: list[str] = []
        stack: list[ASTNode] = [target]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in (
                cs.TS_IDENTIFIER,
                cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
            ):
                if name := safe_decode_text(node):
                    names.append(name)
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
            ):
                stack.extend(node.named_children)
        return names

    def _js_bound_by_enclosing_callable(
        self,
        site: ASTNode,
        name: str,
        language: cs.SupportedLanguage | None,
        limit: ASTNode | None = None,
    ) -> bool:
        # `new Box()` constructs whatever `Box` is in scope at the site. A
        # parameter, local, loop or catch binding, or a function or class
        # declared inside an enclosing callable is a different value from
        # the module's class, often a constructor the caller is handed, and
        # typing the instance as the module class gave that class's methods
        # callers that never reach them (#2465). Only the module's own
        # binding, which the class lookup resolves, may type it. `limit`: a
        # block that declares the name itself; only callables inside it can
        # bind it nearer.
        callables: list[ASTNode] = []
        root = site
        current = site.parent
        inside = True
        while current is not None:
            inside = inside and (limit is None or current.id != limit.id)
            if (
                inside
                and current.type in _JS_SELF_NAMED_EXPRESSIONS
                and self._js_declared_name(current) == name
            ):
                # A named function or class expression binds its own name
                # inside itself only.
                return True
            if inside and current.type in _JS_NESTED_CALLABLE_TYPES:
                callables.append(current)
            root = current
            current = current.parent
        if not callables:
            return False
        pos = site.start_byte
        return any(
            start <= pos < end
            for callable_node in callables
            for start, end in self._js_binding_index(callable_node, root, language).get(
                name, ()
            )
        )

    def _js_binding_index(
        self,
        callable_node: ASTNode,
        root: ASTNode,
        language: cs.SupportedLanguage | None,
    ) -> _JsBindingIndex:
        # Scanning the callable at every construction made a function with n
        # constructions cost O(n^2); each callable is indexed once per file.
        if self._binding_root is None or self._binding_root.id != root.id:
            self._binding_root = root
            self._binding_module_strict = self._js_module_is_strict(root)
            self._binding_indexes = {}
        key = (callable_node.start_byte, callable_node.end_byte)
        index = self._binding_indexes.get(key)
        if index is None:
            strict = (
                language in _JS_STRICT_LANGUAGES
                or self._binding_module_strict
                or self._js_callable_is_strict(callable_node)
            )
            index = self._js_build_binding_index(callable_node, strict)
            self._binding_indexes[key] = index
        return index

    def _js_build_binding_index(
        self, callable_node: ASTNode, strict: bool
    ) -> _JsBindingIndex:
        index: _JsBindingIndex = {}
        whole = (callable_node.start_byte, callable_node.end_byte)
        for name in self._js_parameter_names(callable_node):
            index.setdefault(name, []).append(whole)
        stack: list[ASTNode] = list(callable_node.children)
        while stack:
            node = stack.pop()
            names, span = self._js_node_bindings(node, callable_node, strict)
            if span is not None:
                for name in names:
                    index.setdefault(name, []).append(span)
            # Nested callables and classes own what they declare inside.
            if (
                node.type not in _JS_NESTED_CALLABLE_TYPES
                and node.type not in _JS_CLASS_NODE_TYPES
            ):
                stack.extend(node.children)
        return index

    def _js_node_bindings(
        self, node: ASTNode, callable_node: ASTNode, strict: bool
    ) -> tuple[list[str], tuple[int, int] | None]:
        # The names one node declares and the byte span they are visible in;
        # the node types tested below are disjoint.
        node_type = node.type
        if node_type in _JS_HOISTED_DECLARATIONS and (
            declared := self._js_declared_name(node)
        ):
            return [declared], self._js_function_declaration_span(
                node, callable_node, strict
            )
        if node_type in _JS_BLOCK_SCOPED_DECLARATIONS and (
            declared := self._js_declared_name(node)
        ):
            return [declared], self._js_scope_span(node, callable_node)
        if node_type == cs.TS_VARIABLE_DECLARATOR and (
            (target := node.child_by_field_name(cs.FIELD_NAME)) is not None
        ):
            return self._js_binding_names(target), self._js_declarator_span(
                node, callable_node
            )
        if node_type == cs.TS_JS_FOR_IN_STATEMENT and (
            (left := node.child_by_field_name(cs.FIELD_LEFT)) is not None
        ):
            return self._js_binding_names(left), self._js_loop_span(node, callable_node)
        if node_type == cs.TS_JS_CATCH_CLAUSE and (
            (param := node.child_by_field_name(cs.FIELD_PARAMETER)) is not None
        ):
            return self._js_binding_names(param), (node.start_byte, node.end_byte)
        return [], None

    def _js_function_declaration_span(
        self, node: ASTNode, callable_node: ASTNode, strict: bool
    ) -> tuple[int, int]:
        # At the body's top level the declaration is hoisted to the whole
        # callable. Inside a block, strict code (modules, TypeScript, class
        # bodies, "use strict") scopes it to that block like `let`; a sloppy
        # script hoists it to the callable too (Annex B), where after the
        # block the name reads the function or undefined, never the class.
        body = node.parent
        top_level = (
            body is not None
            and body.parent is not None
            and body.parent.id == callable_node.id
        )
        if top_level or not strict:
            return (callable_node.start_byte, callable_node.end_byte)
        return self._js_scope_span(node, callable_node)

    def _js_module_is_strict(self, root: ASTNode) -> bool:
        return any(
            child.type in _JS_MODULE_STATEMENTS for child in root.named_children
        ) or self._js_has_use_strict(root)

    def _js_callable_is_strict(self, callable_node: ASTNode) -> bool:
        # Class bodies are always strict; a "use strict" prologue makes its
        # function and everything nested in it strict.
        current: ASTNode | None = callable_node
        while current is not None:
            if current.type == cs.TS_CLASS_BODY:
                return True
            if current.type in _JS_NESTED_CALLABLE_TYPES and (
                (body := current.child_by_field_name(cs.FIELD_BODY)) is not None
                and self._js_has_use_strict(body)
            ):
                return True
            current = current.parent
        return False

    @staticmethod
    def _js_has_use_strict(scope: ASTNode) -> bool:
        # The directive prologue: the string-literal statements that open
        # a script or function body.
        for statement in scope.named_children:
            if statement.type == cs.TS_COMMENT:
                continue
            literal = (
                statement.named_children[0]
                if statement.type == cs.TS_EXPRESSION_STATEMENT
                and statement.named_child_count == 1
                else None
            )
            if literal is None or literal.type != cs.TS_STRING:
                return False
            text = safe_decode_text(literal)
            if text is not None and text[1:-1] == cs.JS_USE_STRICT_DIRECTIVE:
                return True
        return False

    def _js_parameter_names(self, callable_node: ASTNode) -> list[str]:
        # A TS parameter wraps its pattern (`Box: T = d`); only the pattern
        # binds, the default value is a read of the enclosing scope.
        names: list[str] = []
        for field in (cs.FIELD_PARAMETERS, cs.FIELD_PARAMETER):
            params = callable_node.child_by_field_name(field)
            if params is None:
                continue
            entries = (
                params.named_children
                if params.type == cs.TS_JS_FORMAL_PARAMETERS
                else [params]
            )
            for entry in entries:
                pattern: ASTNode | None = entry
                if entry.type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
                    pattern = entry.child_by_field_name(cs.TS_FIELD_PATTERN)
                if pattern is not None:
                    names.extend(self._js_binding_names(pattern))
        return names

    def _js_declarator_span(
        self, node: ASTNode, callable_node: ASTNode
    ) -> tuple[int, int]:
        if node.parent is not None and node.parent.type == cs.TS_LEXICAL_DECLARATION:
            return self._js_scope_span(node, callable_node)
        return (callable_node.start_byte, callable_node.end_byte)

    def _js_loop_span(
        self, node: ASTNode, callable_node: ASTNode
    ) -> tuple[int, int] | None:
        # Same rule as the ctor index: no kind assigns an existing binding,
        # `var` hoists to the callable, `let`/`const` cover the loop.
        kind = node.child_by_field_name(cs.FIELD_KIND)
        if kind is None:
            return None
        if kind.type == cs.TS_JS_VAR_KIND:
            return (callable_node.start_byte, callable_node.end_byte)
        return (node.start_byte, node.end_byte)

    @staticmethod
    def _js_declared_name(node: ASTNode) -> str | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        return safe_decode_text(name_node) if name_node is not None else None

    @staticmethod
    def _js_scope_span(node: ASTNode, method_node: ASTNode) -> tuple[int, int]:
        # The scope a `let`/`const` governs: the nearest enclosing block-like
        # ancestor (a for-header declaration governs the for statement, a
        # case-level one the switch body), or the whole method.
        current = node.parent
        while current is not None and current.id != method_node.id:
            if current.type in _JS_SCOPE_CONTAINER_TYPES:
                return (current.start_byte, current.end_byte)
            current = current.parent
        return (method_node.start_byte, method_node.end_byte)

    @staticmethod
    def _js_innermost_scope(
        entries: list[tuple[int, int, ASTNode | None]], pos: int
    ) -> tuple[int, int] | None:
        enclosing = [(s, e) for s, e, _v in entries if s <= pos < e]
        if not enclosing:
            return None
        # Nested spans: the innermost starts latest; ties are the same span.
        return max(enclosing, key=lambda span: (span[0], -span[1]))

    def _js_constructed_for(
        self, name: str, pos: int, index: _CtorBindingIndex
    ) -> ASTNode | None:
        # The variable `return x` reads is the innermost declaration of x
        # whose scope encloses the return (no declaration: the name is a
        # parameter, function-scoped). The constructions reaching it are its
        # own initialiser plus every assignment site resolving to the SAME
        # declaration; they must all agree on one class.
        decls, assigns = index
        entries = decls.get(name, [])
        scope = self._js_innermost_scope(entries, pos)
        constructions: list[ASTNode] = []
        if scope is not None:
            constructions.extend(
                value
                for s, e, value in entries
                if (s, e) == scope and value is not None
            )
        for site, value in assigns.get(name, []):
            if self._js_innermost_scope(entries, site) == scope:
                constructions.append(value)
        # A construction whose class name cannot be extracted (`new
        # registry.Cached(v)`) is an UNKNOWN class: it vetoes exactly like a
        # different named class, never silently loses to one.
        ctor_names = {ut.extract_constructor_name(value) for value in constructions}
        if len(ctor_names) != 1:
            return None
        chosen = ctor_names.pop()
        if chosen is None:
            return None
        return next(
            value
            for value in constructions
            if ut.extract_constructor_name(value) == chosen
        )
