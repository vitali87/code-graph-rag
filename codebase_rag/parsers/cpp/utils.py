from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text, safe_decode_with_fallback


def convert_operator_symbol_to_name(symbol: str) -> str:
    return cs.CPP_OPERATOR_SYMBOL_MAP.get(
        symbol,
        f"{cs.OPERATOR_PREFIX}{cs.CHAR_UNDERSCORE}{symbol.replace(cs.CHAR_SPACE, cs.CHAR_UNDERSCORE)}",
    )


def build_qualified_name(node: Node, module_qn: str, name: str) -> str:
    module_parts = module_qn.split(cs.SEPARATOR_DOT)

    is_module_file = len(module_parts) >= 3 and (
        bool(cs.CPP_MODULE_PATH_MARKERS & set(module_parts))
        or any(part.endswith(cs.CPP_MODULE_EXTENSIONS) for part in module_parts)
    )

    if is_module_file:
        project_name = module_parts[0]
        filename = module_parts[-1]

        return cs.SEPARATOR_DOT.join([project_name, filename, name])

    path_parts = extract_namespace_path(node)

    if path_parts:
        return cs.SEPARATOR_DOT.join([module_qn, *path_parts, name])
    return cs.SEPARATOR_DOT.join([module_qn, name])


def extract_namespace_path(node: Node) -> list[str]:
    """Names of the namespace blocks enclosing *node*, outermost first.

    C++17 nested syntax (`namespace a::b {`) parses as ONE namespace node
    named `a::b`; split it so both spellings of the same namespaces yield
    identical qn segments (`a.b`), matching classic nesting and the libclang
    frontend's nested cursors.
    """
    path_parts: list[str] = []
    current = node.parent

    while current and current.type != cs.CppNodeType.TRANSLATION_UNIT:
        if current.type == cs.CppNodeType.NAMESPACE_DEFINITION:
            namespace_name = None
            name_node = current.child_by_field_name(cs.KEY_NAME)
            if name_node and name_node.text:
                namespace_name = safe_decode_text(name_node)
            else:
                for child in current.children:
                    if (
                        child.type
                        in (
                            cs.CppNodeType.NAMESPACE_IDENTIFIER,
                            cs.CppNodeType.IDENTIFIER,
                        )
                        and child.text
                    ):
                        namespace_name = safe_decode_text(child)
                        break
            if namespace_name:
                path_parts.extend(
                    reversed(namespace_name.split(cs.SEPARATOR_DOUBLE_COLON))
                )
        current = current.parent

    path_parts.reverse()
    return path_parts


_EXPORT_CANDIDATE_TYPES = frozenset(
    {
        cs.CppNodeType.EXPORT,
        cs.CppNodeType.EXPORT_KEYWORD,
        cs.CppNodeType.IDENTIFIER,
        cs.CppNodeType.PRIMITIVE_TYPE,
    }
)

_EXPORT_STOP_TYPES = frozenset(
    {
        cs.CppNodeType.DECLARATION,
        cs.CppNodeType.FUNCTION_DEFINITION,
        cs.CppNodeType.TEMPLATE_DECLARATION,
        cs.CppNodeType.CLASS_SPECIFIER,
        cs.CppNodeType.TRANSLATION_UNIT,
    }
)


def is_exported(node: Node) -> bool:
    current = node
    export_text = cs.CppNodeType.EXPORT
    while current and current.parent:
        parent = current.parent

        for child in parent.children:
            if child == current:
                break
            if (
                child.type in _EXPORT_CANDIDATE_TYPES
                and child.text
                and safe_decode_text(child) == export_text
            ):
                return True

        if current.type in _EXPORT_STOP_TYPES:
            break
        current = current.parent

    return False


def extract_exported_class_name(class_node: Node) -> str | None:
    return next(
        (
            safe_decode_text(child)
            for child in class_node.children
            if child.type == cs.CppNodeType.IDENTIFIER and child.text
        ),
        None,
    )


def extract_operator_name(operator_node: Node) -> str:
    if not operator_node.text:
        return cs.CPP_FALLBACK_OPERATOR

    operator_text = safe_decode_with_fallback(operator_node).strip()

    if operator_text.startswith(cs.CPP_OPERATOR_TEXT_PREFIX):
        symbol = operator_text[len(cs.CPP_OPERATOR_TEXT_PREFIX) :].strip()
        return convert_operator_symbol_to_name(symbol)

    return cs.CPP_FALLBACK_OPERATOR


def extract_destructor_name(destructor_node: Node) -> str:
    for child in destructor_node.children:
        if child.type == cs.CppNodeType.IDENTIFIER and child.text:
            class_name = safe_decode_text(child)
            return f"{cs.CPP_DESTRUCTOR_PREFIX}{class_name}"
    return cs.CPP_FALLBACK_DESTRUCTOR


def _extract_name_from_function_definition(func_node: Node) -> str | None:
    def find_function_declarator(node: Node) -> str | None:
        if node.type == cs.CppNodeType.FUNCTION_DECLARATOR:
            return extract_function_name(node)

        for child in node.children:
            if child.type in (
                cs.CppNodeType.POINTER_DECLARATOR,
                cs.CppNodeType.REFERENCE_DECLARATOR,
                cs.CppNodeType.FUNCTION_DECLARATOR,
                cs.CppNodeType.PARENTHESIZED_DECLARATOR,
                # A macro-attributed ctor buries its REAL declarator inside
                # the ERROR while the base-initializer (`: exception(...)`)
                # survives as a sibling declarator; depth-first source order
                # must enter the ERROR so the ctor's own name wins.
                cs.TS_ERROR,
            ):
                result = find_function_declarator(child)
                if result:
                    return result
        return None

    return find_function_declarator(func_node)


def _extract_name_from_declaration(func_node: Node) -> str | None:
    return next(
        (
            extract_function_name(child)
            for child in func_node.children
            if child.type == cs.CppNodeType.FUNCTION_DECLARATOR
        ),
        None,
    )


def _extract_name_from_field_declaration(func_node: Node) -> str | None:
    has_function_declarator = any(
        child.type == cs.CppNodeType.FUNCTION_DECLARATOR for child in func_node.children
    )
    if not has_function_declarator:
        return None

    for child in func_node.children:
        if child.type == cs.CppNodeType.FUNCTION_DECLARATOR:
            declarator = child.child_by_field_name(cs.FIELD_DECLARATOR)
            if (
                declarator
                and declarator.type == cs.CppNodeType.FIELD_IDENTIFIER
                and declarator.text
            ):
                return safe_decode_text(declarator)

            for grandchild in child.children:
                if (
                    grandchild.type == cs.CppNodeType.FIELD_IDENTIFIER
                    and grandchild.text
                ):
                    return safe_decode_text(grandchild)
    return None


def _extract_name_from_function_declarator(func_node: Node) -> str | None:
    for child in func_node.children:
        if (
            child.type
            in (
                cs.CppNodeType.IDENTIFIER,
                cs.CppNodeType.FIELD_IDENTIFIER,
            )
            and child.text
        ):
            return safe_decode_text(child)
        if child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
            return _find_rightmost_name(child)
        if child.type == cs.CppNodeType.OPERATOR_NAME:
            return extract_operator_name(child)
        if child.type == cs.CppNodeType.DESTRUCTOR_NAME:
            return extract_destructor_name(child)
    return None


def _find_rightmost_name(node: Node) -> str | None:
    # Handle out-of-class method definitions like Calculator::add
    # or deeply nested like Outer::Inner::MyClass::method
    last_name = None
    for qchild in node.children:
        match qchild.type:
            case cs.CppNodeType.IDENTIFIER | cs.CppNodeType.FIELD_IDENTIFIER:
                last_name = safe_decode_text(qchild)
            case cs.CppNodeType.OPERATOR_NAME:
                last_name = extract_operator_name(qchild)
            case cs.CppNodeType.DESTRUCTOR_NAME:
                last_name = extract_destructor_name(qchild)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                if nested := _find_rightmost_name(qchild):
                    last_name = nested
    return last_name


def _extract_name_from_template_declaration(func_node: Node) -> str | None:
    return next(
        (
            extract_function_name(child)
            for child in func_node.children
            if child.type
            in (
                cs.CppNodeType.FUNCTION_DEFINITION,
                cs.CppNodeType.FUNCTION_DECLARATOR,
                cs.CppNodeType.DECLARATION,
            )
        ),
        None,
    )


def _enclosing_class_name(node: Node) -> str | None:
    current = node.parent
    while current is not None:
        if current.type in cs.CPP_TYPE_SPECIFIER_NODE_TYPES:
            name = current.child_by_field_name(cs.FIELD_NAME)
            if name is None:
                return None
            # A specialization's name (`formatter<T, char>`) is a
            # template_type; the ctor identifier repeats only the bare name.
            if name.type == cs.CppNodeType.TEMPLATE_TYPE:
                inner = name.child_by_field_name(cs.FIELD_NAME)
                return safe_decode_text(inner) if inner is not None else None
            return safe_decode_text(name)
        current = current.parent
    return None


def _has_named_parameter(declarator: Node) -> bool:
    # A macro invocation's "parameters" are expressions (`(...)`, bare
    # identifiers parsed as type-only declarations, or call shapes), so none
    # carries a NAMED declarator. A real definition's `int fd` / `const S& s`
    # does. One named parameter is proof of a genuine declaration even when
    # recovery orphaned it from its class.
    params = declarator.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return False

    def declares_identifier(node: Node) -> bool:
        # Follow only the declarator-field spine (plus the two wrapper nodes
        # holding their declarator as a bare child): identifiers reachable
        # ONLY off that path are array bounds (`int[MAX_SIZE]`) or an inner
        # fn-ptr's parameter names (`void (*)(int x)`), not names of THIS
        # parameter.
        if node.type in (cs.CppNodeType.IDENTIFIER, cs.CppNodeType.FIELD_IDENTIFIER):
            return True
        inner = node.child_by_field_name(cs.FIELD_DECLARATOR)
        if inner is not None:
            return declares_identifier(inner)
        if node.type in (
            cs.CppNodeType.REFERENCE_DECLARATOR,
            cs.CppNodeType.PARENTHESIZED_DECLARATOR,
        ):
            return any(
                declares_identifier(child) for child in node.children if child.is_named
            )
        return False

    for param in params.children:
        if param.type not in (
            cs.CppNodeType.PARAMETER_DECLARATION,
            cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
        ):
            continue
        inner = param.child_by_field_name(cs.FIELD_DECLARATOR)
        if inner is not None and declares_identifier(inner):
            return True
    return False


def is_recovery_artifact_shape(func_node: Node) -> bool:
    # `FMT_CATCH(...) {}` (a macro invocation followed by a block) parses as a
    # TYPE-LESS function_definition (or declaration, when member-init recovery
    # sweeps it into a class body) named after the macro. Valid C++ only omits
    # the return type on a constructor, whose plain-identifier declarator
    # repeats the enclosing class name; every OTHER type-less plain-identifier
    # definition shares one shape with two meanings: a macro invocation, or a
    # real definition recovery orphaned from its class or stripped of its type.
    # The registered-class tiebreak and named-parameter evidence (see
    # is_macro_invocation_artifact) decide.
    if func_node.type not in (
        cs.CppNodeType.FUNCTION_DEFINITION,
        cs.CppNodeType.DECLARATION,
    ):
        return False
    if func_node.child_by_field_name(cs.FIELD_TYPE) is not None:
        return False
    declarator = func_node.child_by_field_name(cs.FIELD_DECLARATOR)
    if declarator is None or declarator.type != cs.CppNodeType.FUNCTION_DECLARATOR:
        return False
    inner = declarator.child_by_field_name(cs.FIELD_DECLARATOR)
    if inner is None or inner.type != cs.CppNodeType.IDENTIFIER or not inner.text:
        return False
    return safe_decode_text(inner) != _enclosing_class_name(func_node)


def has_named_parameter(func_node: Node) -> bool:
    declarator = func_node.child_by_field_name(cs.FIELD_DECLARATOR)
    return declarator is not None and _has_named_parameter(declarator)


def is_macro_invocation_artifact(func_node: Node) -> bool:
    # A macro invocation's "parameters" are expressions, so the artifact shape
    # WITH a named parameter is proof of a genuine (recovery-mangled)
    # definition; without one, only a registered class bearing the name (an
    # orphaned zero-param ctor) saves the node from being dropped.
    return is_recovery_artifact_shape(func_node) and not has_named_parameter(func_node)


def extract_function_name(func_node: Node) -> str | None:
    name = _extract_function_name_by_type(func_node)
    # A reserved keyword in declarator position is an error-recovery
    # artifact (macro access-label + `const decltype(MACRO_)` members), not
    # a definition; registering it mints a phantom Method (reader.decltype).
    if name in cs.CPP_RESERVED_DEF_NAMES:
        return None
    return name


def _extract_function_name_by_type(func_node: Node) -> str | None:
    match func_node.type:
        case (
            cs.CppNodeType.FUNCTION_DEFINITION
            | cs.CppNodeType.CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION
            | cs.CppNodeType.INLINE_METHOD_DEFINITION
            | cs.CppNodeType.OPERATOR_CAST_DEFINITION
        ):
            return _extract_name_from_function_definition(func_node)
        case (
            cs.CppNodeType.DECLARATION
            | cs.CppNodeType.CONSTRUCTOR_OR_DESTRUCTOR_DECLARATION
        ):
            return _extract_name_from_declaration(func_node)
        case cs.CppNodeType.FIELD_DECLARATION:
            name = _extract_name_from_declaration(func_node)
            return name or _extract_name_from_field_declaration(func_node)
        case cs.CppNodeType.FUNCTION_DECLARATOR:
            return _extract_name_from_function_declarator(func_node)
        case cs.CppNodeType.TEMPLATE_DECLARATION:
            return _extract_name_from_template_declaration(func_node)
        case _:
            return None


def _get_inner_function_node(node: Node) -> Node:
    if node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        for child in node.children:
            if child.type == cs.CppNodeType.FUNCTION_DEFINITION:
                return child
    return node


def _scope_segment_name(scope: Node) -> str | None:
    # The name of one scope segment of a qualified return type. A namespace or
    # plain type reads directly, but a TEMPLATE_TYPE scope (`Outer<T>::Inner`)
    # must reduce to its `type_identifier`: the raw text carries `<T>` template
    # arguments that no registry class QN holds, so it would never suffix-match.
    if scope.type == cs.CppNodeType.TEMPLATE_TYPE:
        name = scope.child_by_field_name(cs.FIELD_NAME)
        return safe_decode_text(name) if name is not None else None
    return safe_decode_text(scope)


def _return_type_path(type_node: Node) -> str | None:
    # Reduce a return-type node to a dotted namespace-qualified class path:
    # `::nlohmann::detail::parser<...>` -> "nlohmann.detail.parser", a bare
    # `Widget` -> "Widget". Descend a qualified_identifier's `name` field,
    # collecting each `scope` namespace, and unwrap a template_type to its
    # `type_identifier`. A primitive/auto/other return type has no class name
    # and yields None so a chained hop off it stays unresolved. The qualified
    # path disambiguates a factory-returned class from a same-named factory
    # method (nlohmann's basic_json has both).
    parts: list[str] = []
    current: Node | None = type_node
    while current is not None:
        match current.type:
            case cs.CppNodeType.TYPE_IDENTIFIER:
                if name := safe_decode_text(current):
                    parts.append(name)
                break
            case cs.CppNodeType.TEMPLATE_TYPE:
                current = current.child_by_field_name(cs.FIELD_NAME)
            case cs.CppNodeType.QUALIFIED_IDENTIFIER:
                scope = current.child_by_field_name(cs.FIELD_SCOPE)
                if scope is not None and (scope_name := _scope_segment_name(scope)):
                    parts.append(scope_name)
                current = current.child_by_field_name(cs.FIELD_NAME)
            case _:
                return None
    return cs.SEPARATOR_DOT.join(parts) if parts else None


def extract_return_type_name(func_node: Node) -> str | None:
    # The qualified class path a C++ function/method returns, for chained-call
    # typing (`parser(...).parse(...)`). Unwraps a template_declaration to the
    # inner function_definition, then reduces its `type` field to a class path.
    inner = _get_inner_function_node(func_node)
    type_node = inner.child_by_field_name(cs.FIELD_TYPE)
    if type_node is None:
        return None
    return _return_type_path(type_node)


def _find_qualified_identifier_in_declarator(func_node: Node) -> Node | None:
    inner_node = _get_inner_function_node(func_node)

    declarator = inner_node.child_by_field_name(cs.FIELD_DECLARATOR)
    if not declarator:
        return None

    if declarator.type == cs.CppNodeType.FUNCTION_DECLARATOR:
        for child in declarator.children:
            if child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
                return child
    return None


def is_out_of_class_method_definition(func_node: Node) -> bool:
    if func_node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        inner = _get_inner_function_node(func_node)
        if inner.type != cs.CppNodeType.FUNCTION_DEFINITION:
            return False
    elif func_node.type not in (
        cs.CppNodeType.FUNCTION_DEFINITION,
        cs.CppNodeType.CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION,
    ):
        return False

    return _find_qualified_identifier_in_declarator(func_node) is not None


def _extract_class_name_from_template_type(template_type_node: Node) -> str | None:
    for child in template_type_node.children:
        if child.type == cs.TS_TYPE_IDENTIFIER and child.text:
            return safe_decode_text(child)
    return None


def extract_class_name_from_out_of_class_method(func_node: Node) -> str | None:
    qualified_id = _find_qualified_identifier_in_declarator(func_node)
    if not qualified_id:
        return None

    has_nested_qualified = any(
        child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER
        for child in qualified_id.children
    )

    if has_nested_qualified:
        return extract_class_name_from_out_of_class_method_qualified(qualified_id)

    for child in qualified_id.children:
        if child.type == cs.TS_TEMPLATE_TYPE:
            return _extract_class_name_from_template_type(child)
        if child.type in (
            cs.CppNodeType.NAMESPACE_IDENTIFIER,
            cs.CppNodeType.IDENTIFIER,
            cs.TS_TYPE_IDENTIFIER,
        ):
            if child.text:
                return safe_decode_text(child)

    return None


def _collect_all_names_from_qualified_id(node: Node) -> list[str]:
    names: list[str] = []
    for child in node.children:
        if child.type in (
            cs.CppNodeType.NAMESPACE_IDENTIFIER,
            cs.CppNodeType.IDENTIFIER,
            cs.TS_TYPE_IDENTIFIER,
        ):
            if name := safe_decode_text(child):
                names.append(name)
        elif child.type == cs.CppNodeType.QUALIFIED_IDENTIFIER:
            names.extend(_collect_all_names_from_qualified_id(child))
    return names


def extract_class_name_from_out_of_class_method_qualified(
    qualified_id: Node,
) -> str | None:
    names = _collect_all_names_from_qualified_id(qualified_id)
    if len(names) >= 2:
        return cs.SEPARATOR_DOUBLE_COLON.join(names[:-1])
    return None
