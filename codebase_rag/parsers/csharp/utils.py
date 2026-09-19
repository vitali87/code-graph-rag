from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def _first_attribute_list(node: Node) -> Node | None:
    # First attribute_list in document order anywhere under `node` (pre-order
    # DFS), so an attribute nested in an inner `#if` (a conditional block
    # inside another) is still found, not only an immediate grandchild.
    if node.type == cs.TS_CSHARP_ATTRIBUTE_LIST:
        return node
    for child in node.children:
        if (found := _first_attribute_list(child)) is not None:
            return found
    return None


def definition_start_point(node: Node) -> tuple[int, int]:
    """The 1-based line and 0-based column a declaration truly starts at.

    The line alone is not enough for a caller that pairs the two: taking the
    line from a nested attribute and the column from the outer node names a
    point that is nowhere in the source (issue #1071).
    """
    for child in node.children:
        if child.type == cs.TS_CSHARP_PREPROC_IF_IN_ATTR_LIST:
            if (attr_list := _first_attribute_list(child)) is not None:
                return attr_list.start_point[0] + 1, attr_list.start_point[1]
            continue
        return child.start_point[0] + 1, child.start_point[1]
    return node.start_point[0] + 1, node.start_point[1]


def definition_start_line(node: Node) -> int:
    # The 1-based line a declaration truly starts on. When its attributes are
    # wrapped in a conditional-compilation block (`#if SYMBOL [Attr] #endif`),
    # tree-sitter nests a leading preproc_if_in_attribute_list child, so the
    # declaration's own start_point is the `#if` directive line. Roslyn treats
    # the directives as trivia and starts the span at the conditional
    # attribute, so return that attribute's line (else the first non-directive
    # child's line). Falls back to the node's own start for the common case
    # with no leading directive.
    for child in node.children:
        if child.type == cs.TS_CSHARP_PREPROC_IF_IN_ATTR_LIST:
            if (attr_list := _first_attribute_list(child)) is not None:
                return attr_list.start_point[0] + 1
            continue
        return child.start_point[0] + 1
    return node.start_point[0] + 1


def _normalize_type_name(text: str) -> str:
    # Strip generic arguments (`List<int>` -> `List`), a nullable suffix
    # (`Widget?`/`int?` -> the underlying type, so a nullable receiver still
    # binds), and whitespace, so a parameter signature is stable and matches
    # the registered, generic-free type names. Array brackets are kept (they
    # distinguish overloads).
    return text.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip().rstrip(cs.CHAR_QUESTION_MARK)


def generic_arity_of_type_text(text: str) -> int:
    # Number of top-level type arguments in a type reference:
    # `Builder` -> 0, `Builder<T>` -> 1, `Map<K, List<V>>` -> 2. Used to
    # disambiguate same-simple-name generic/non-generic type declarations.
    open_idx = text.find(cs.CHAR_ANGLE_OPEN)
    if open_idx < 0:
        return 0
    depth = 0
    count = 1
    for ch in text[open_idx + 1 :]:
        if ch in "<([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == cs.CHAR_ANGLE_CLOSE:
            if depth == 0:
                break
            depth -= 1
        elif ch == cs.CHAR_COMMA and depth == 0:
            count += 1
    return count


GENERIC_ARITY_MARKER = "`"


def annotate_type_ref(text: str) -> str:
    # Normalized type reference carrying its WRITTEN generic arity in CLR
    # style (`Builder<T>` -> "Builder`1", `Builder` -> "Builder"): a plain
    # name always means arity 0, so simple-name twins stay distinguishable
    # through every stored type map without touching method signatures.
    base = _normalize_type_name(text)
    arity = generic_arity_of_type_text(text)
    return f"{base}{GENERIC_ARITY_MARKER}{arity}" if arity else base


def split_type_ref(name: str) -> tuple[str, int]:
    if GENERIC_ARITY_MARKER in name:
        base, _, tail = name.rpartition(GENERIC_ARITY_MARKER)
        if tail.isdigit():
            return base, int(tail)
    return name, 0


def normalize_csharp_type_name(type_node: Node) -> str | None:
    # A type node's normalized name (generic-free, nullable-stripped) or
    # None for unnameable types (`void` callers never chain off it, but a
    # void return is recorded harmlessly and simply never resolves).
    text = safe_decode_text(type_node)
    return _normalize_type_name(text) if text else None


def extract_parameter_type_names(method_node: Node) -> list[str]:
    # The declared type of each parameter, in order, for the method-qn
    # signature that keeps C# overloads distinct. A `params object[]` tail is
    # not wrapped in a `parameter` node (grammar quirk); its `array_type`
    # sits directly under the parameter_list, so capture that too.
    param_list = method_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if param_list is None:
        return []
    types: list[str] = []
    for child in param_list.children:
        type_node: Node | None = None
        if child.type == cs.TS_CSHARP_PARAMETER:
            type_node = child.child_by_field_name(cs.FIELD_TYPE)
        elif child.type == cs.TS_CSHARP_ARRAY_TYPE:
            type_node = child
        if type_node is not None and type_node.text:
            if name := safe_decode_text(type_node):
                types.append(_normalize_type_name(name))
    return types


_CSHARP_TYPE_DECLARATIONS = frozenset(
    {
        cs.TS_CSHARP_CLASS_DECLARATION,
        cs.TS_CSHARP_STRUCT_DECLARATION,
        cs.TS_CSHARP_RECORD_DECLARATION,
        cs.TS_CSHARP_INTERFACE_DECLARATION,
        cs.TS_CSHARP_ENUM_DECLARATION,
    }
)


def _declared_name(node: Node) -> str | None:
    name_node = node.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
    if name_node is None or not name_node.text:
        return None
    return safe_decode_text(name_node)


def _enclosing_scopes(node: Node) -> tuple[list[str], list[str]]:
    # (namespace segments, enclosing type names) of `node`, outermost first.
    # Block namespaces are ancestors and nest; a file-scoped `namespace N;`
    # is a sibling under the compilation unit, so it is read from there.
    namespaces: list[str] = []
    types: list[str] = []
    current = node.parent
    while current is not None:
        if current.type == cs.TS_CSHARP_NAMESPACE_DECLARATION:
            if name := _declared_name(current):
                namespaces.append(name)
        elif current.type in _CSHARP_TYPE_DECLARATIONS:
            if name := _declared_name(current):
                types.append(name)
        elif current.type == cs.TS_CSHARP_COMPILATION_UNIT:
            for child in current.children:
                if child.type == cs.TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION:
                    if name := _declared_name(child):
                        namespaces.append(name)
                    break
        current = current.parent
    namespaces.reverse()
    types.reverse()
    return namespaces, types


def declared_namespace(node: Node) -> str | None:
    """The dotted namespace `node` is declared in, or None at the top level."""
    namespaces, _types = _enclosing_scopes(node)
    return cs.SEPARATOR_DOT.join(namespaces) if namespaces else None


def namespace_qualified_name(type_node: Node) -> str:
    """`N1.Outer.Widget` for a type declaration: namespace, enclosing types,
    own name. Read from the declaration rather than the qualified name,
    because a namespace the module's directory already spells is not in the
    qn (issue #1629)."""
    namespaces, types = _enclosing_scopes(type_node)
    own = _declared_name(type_node)
    return cs.SEPARATOR_DOT.join([*namespaces, *types, *([own] if own else [])])


def extension_receiver_type(method_node: Node) -> str | None:
    # For an extension method, the normalized type of its receiver: the first
    # parameter, whose first modifier is `this` (`static int WordCount(this
    # string s)` -> "string"). Only extension methods carry `this` on a
    # parameter, so its presence both identifies the method and names the
    # receiver type a call binds against (`s.WordCount()`). Returns None for a
    # non-extension method.
    param_list = method_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if param_list is None:
        return None
    first = next(
        (c for c in param_list.children if c.type == cs.TS_CSHARP_PARAMETER), None
    )
    if first is None:
        return None
    has_this = any(
        c.type == cs.TS_CSHARP_MODIFIER and safe_decode_text(c) == cs.TS_CSHARP_THIS
        for c in first.children
    )
    if not has_this:
        return None
    type_node = first.child_by_field_name(cs.FIELD_TYPE)
    name = safe_decode_text(type_node) if type_node and type_node.text else None
    return annotate_type_ref(name) if name else None


def index_extension_method(
    store: dict[str, list[tuple[str, str, str, int]]],
    ingested_qn: str,
    method_node: Node,
    class_qn: str,
    module_qn: str | None,
) -> None:
    # Index an extension method by simple name + receiver type + declaring
    # namespace so a `recv.Ext()` call binds to the static method even though it
    # lives on an unrelated static class (not in recv's hierarchy). Shared by the
    # class-member pass and the `#if`-truncation recovery so both stay in sync.
    # No-op for a non-extension method (no `this` receiver).
    receiver_type = extension_receiver_type(method_node)
    if not receiver_type:
        return
    # The receiver's WRITTEN generic arity (`this Builder<TResult>` -> 1),
    # so a call receiver of known arity never binds an extension declared
    # for the other twin.
    receiver_arity = 0
    param_list = method_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if param_list is not None:
        first = next(
            (c for c in param_list.children if c.type == cs.TS_CSHARP_PARAMETER), None
        )
        if first is not None:
            type_node = first.child_by_field_name(cs.FIELD_TYPE)
            raw = safe_decode_text(type_node) if type_node is not None else None
            if raw:
                receiver_arity = generic_arity_of_type_text(raw)
    # Strip the parameter signature BEFORE taking the leaf: a qualified param
    # type (`Poke(N2.Widget)`) contains dots, so an rsplit-then-strip would key
    # on `Widget)` instead of the method name `Poke` and never match.
    leaf = ingested_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rsplit(cs.SEPARATOR_DOT, 1)[-1]
    # The extension's declaring namespace (its class's namespace-qualified name
    # minus the class leaf) so an unqualified `this Widget` can resolve to
    # `<namespace>.Widget` against a qualified call receiver. Empty for a
    # top-level (namespace-less) class. Read from the declaration: the qn no
    # longer carries a namespace the directory spells (issue #1629).
    namespaces, enclosing_types = _enclosing_scopes(method_node)
    ext_namespace = cs.SEPARATOR_DOT.join([*namespaces, *enclosing_types[:-1]])
    store.setdefault(leaf, []).append(
        (ingested_qn, receiver_type, ext_namespace, receiver_arity)
    )


def _property_field(member: Node) -> tuple[str, str] | None:
    """(name, annotated type) for a property declaration, or None if either is absent."""
    name = safe_decode_text(member.child_by_field_name(cs.FIELD_NAME))
    type_text = safe_decode_text(member.child_by_field_name(cs.FIELD_TYPE))
    if not name or not type_text:
        return None
    return name, annotate_type_ref(type_text)


def _declared_fields(member: Node) -> list[tuple[str, str]]:
    """(name, annotated type) for every declarator of one field declaration.

    `private Widget _a, _b;` declares two names sharing one type.
    """
    var_decl = next(
        (c for c in member.children if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION),
        None,
    )
    if var_decl is None:
        return []
    type_text = safe_decode_text(var_decl.child_by_field_name(cs.FIELD_TYPE))
    if not type_text:
        return []
    annotated = annotate_type_ref(type_text)
    declared: list[tuple[str, str]] = []
    for declarator in var_decl.children:
        if declarator.type != cs.TS_CSHARP_VARIABLE_DECLARATOR:
            continue
        name = safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
        if name:
            declared.append((name, annotated))
    return declared


def build_field_type_map(class_node: Node) -> dict[str, str]:
    # {field-or-property name: type name} for members declared directly on
    # this class body, recorded at ingestion so a receiver typed to a field
    # (`_w.M()`) resolves, including a field inherited from a base class in
    # another file, reached by walking class_inheritance over these maps.
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return {}
    fields: dict[str, str] = {}
    for member in body.children:
        if member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
            if (prop := _property_field(member)) is not None:
                fields[prop[0]] = prop[1]
        elif member.type == cs.TS_CSHARP_FIELD_DECLARATION:
            fields.update(_declared_fields(member))
    return fields


def _operator_name(method_node: Node) -> str | None:
    """`operator_<symbol>` / `operator_<target-type>`, or None for a non-operator.

    Operators expose no `name` field, so the leaf is synthesized from the
    `operator` field (binary and unary) or the `type` field (conversion).
    """
    if method_node.type == cs.TS_CSHARP_OPERATOR_DECLARATION:
        field = cs.TS_CSHARP_FIELD_OPERATOR
    elif method_node.type == cs.TS_CSHARP_CONVERSION_OPERATOR_DECLARATION:
        field = cs.TS_CSHARP_FIELD_TYPE
    else:
        return None
    node = method_node.child_by_field_name(field)
    symbol = safe_decode_text(node) if node is not None and node.text else None
    return cs.TS_CSHARP_OPERATOR_NAME_PREFIX + symbol if symbol else None


def synthesize_method_name(method_node: Node) -> str | None:
    # The registered leaf name for a C# member. Operators are synthesized (see
    # _operator_name). A destructor HAS a `name` field equal to the type name,
    # which would collide with the constructor, so prefix `~`. Everything else
    # uses the plain `name` leaf. Kept identical to _csharp_get_name so the FQN
    # scope walk and the node qn agree.
    if method_node.type in (
        cs.TS_CSHARP_OPERATOR_DECLARATION,
        cs.TS_CSHARP_CONVERSION_OPERATOR_DECLARATION,
    ):
        return _operator_name(method_node)
    name_node = method_node.child_by_field_name(cs.FIELD_NAME)
    name = safe_decode_text(name_node) if name_node and name_node.text else None
    if name and method_node.type == cs.TS_CSHARP_DESTRUCTOR_DECLARATION:
        return cs.TS_CSHARP_DESTRUCTOR_NAME_PREFIX + name
    # A reserved keyword as the name means tree-sitter parse-recovered a broken
    # construct (e.g. a `#if`-split `else if` chain -> local_function named
    # `if`); it is never a real member, so drop it rather than pollute the graph.
    if name in cs.CSHARP_RESERVED_KEYWORDS:
        return None
    return name


def extract_method_signature(method_node: Node) -> tuple[str | None, list[str]]:
    # (method name, parameter type names). The name matches the leaf
    # ingest_method registers (synthesized for operators/destructors), so the
    # signatured qn stays consistent. Overloaded operators (`operator +` on
    # two operand types) still get distinct qns via the parameter signature.
    return synthesize_method_name(method_node), extract_parameter_type_names(
        method_node
    )
