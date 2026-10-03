from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import CSharpGenericBase, CSharpGenericShape
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
    normalized: list[str] = []
    generic_depth = 0
    for char in text:
        if char == cs.CHAR_ANGLE_OPEN:
            generic_depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE and generic_depth:
            generic_depth -= 1
        elif generic_depth == 0:
            normalized.append(char)
    return "".join(normalized).strip().rstrip(cs.CHAR_QUESTION_MARK)


def strip_generic_arguments(text: str) -> str:
    """A type path with every segment's generic arguments removed.

    `Lib.Util<int>.Helper<T>` -> `Lib.Util.Helper`: unlike a cut at the
    first `<`, a generic NON-LEAF segment keeps the segments after it.
    """
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif ch == cs.CHAR_ANGLE_CLOSE:
            depth = max(depth - 1, 0)
        elif depth == 0:
            out.append(ch)
    return "".join(out).replace(" ", "")


def leaf_type_segment(text: str) -> str:
    """The last segment of a type path, split at the last dot OUTSIDE generic
    arguments: `Lib.Helper<System.String>` -> `Helper<System.String>`, where a
    plain rsplit would hand back `String>`.
    """
    depth = 0
    cut = -1
    for index, ch in enumerate(text):
        if ch == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif ch == cs.CHAR_ANGLE_CLOSE:
            depth = max(depth - 1, 0)
        elif ch == cs.SEPARATOR_DOT and depth == 0:
            cut = index
    return text[cut + 1 :]


def generic_arity_of_type_text(text: str) -> int:
    # Number of top-level type arguments in a type reference:
    # `Builder` -> 0, `Builder<T>` -> 1, `Map<K, List<V>>` -> 2. Used to
    # disambiguate same-simple-name generic/non-generic type declarations.
    open_idx = text.find(cs.CHAR_ANGLE_OPEN, _type_leaf_start(text))
    if open_idx < 0:
        return 0
    return _count_top_level_type_args(text[open_idx + 1 :])


def _type_leaf_start(text: str) -> int:
    # Index where the last top-level `.`/`::`-separated segment begins, so
    # `Outer<A>.Inner<B, C>` counts Inner's arguments, not Outer's.
    leaf_start = 0
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE and depth:
            depth -= 1
        elif depth == 0:
            if text.startswith(cs.SEPARATOR_DOUBLE_COLON, index):
                leaf_start = index + len(cs.SEPARATOR_DOUBLE_COLON)
                index += len(cs.SEPARATOR_DOUBLE_COLON) - 1
            elif char == cs.SEPARATOR_DOT:
                leaf_start = index + 1
        index += 1
    return leaf_start


def _count_top_level_type_args(args_text: str) -> int:
    # Comma-separated arguments up to the `>` closing the list `args_text`
    # opens after; nested `<>`, `()` and `[]` commas are not counted.
    depth = 0
    count = 1
    for ch in args_text:
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
    return type_ref(_normalize_type_name(text), generic_arity_of_type_text(text))


def type_ref(name: str, arity: int) -> str:
    return f"{name}{GENERIC_ARITY_MARKER}{arity}" if arity else name


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


def signature_type_name(text: str) -> str:
    """A type as a method signature spells it: `Ctx< T >?` -> `Ctx<T>`.

    Generic arguments are kept, because `Validate(Ctx<T>)` and
    `Validate(Ctx)` are two overloads and erasing them made the second a
    line-suffixed duplicate of the first (issue #2619). They get one
    canonical spacing so two spellings of a type name one overload, and a
    trailing nullable `?` is dropped as the type maps drop it.
    """
    collapsed = cs.CHAR_SPACE.join(text.split())
    out: list[str] = []
    depth = 0
    for index, char in enumerate(collapsed):
        if char == cs.CHAR_SPACE:
            following = collapsed[index + 1 : index + 2]
            if following == cs.CHAR_ANGLE_OPEN or (
                depth
                and (
                    (out and out[-1] in cs.CSHARP_SIGNATURE_TIGHT_CHARS + char)
                    or following in cs.CSHARP_SIGNATURE_TIGHT_CHARS
                )
            ):
                continue
        elif char == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE and depth:
            depth -= 1
        out.append(char)
        if char == cs.CHAR_COMMA and depth:
            out.append(cs.CHAR_SPACE)
    return "".join(out).strip().rstrip(cs.CHAR_QUESTION_MARK)


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
                types.append(signature_type_name(name))
    return types


def explicit_interface_name(member_node: Node) -> str | None:
    """The interface an explicit implementation names, as written:
    `int IValidator.Validate(Ctx c)` -> `IValidator`; None for any other
    member."""
    for child in member_node.children:
        if child.type == cs.TS_CSHARP_EXPLICIT_INTERFACE_SPECIFIER:
            named = child.named_children
            text = safe_decode_text(named[0]) if named else None
            return signature_type_name(text) if text else None
    return None


def member_qn_leaf(member_node: Node) -> str | None:
    """The leaf a C# member registers under when its bare name is not enough.

    Parameters give `Validate(Ctx<T>)`, keeping overloads apart. An explicit
    interface implementation is prefixed with its interface,
    `IValidator#Validate(Ctx)`: C# reaches it only through that interface,
    so it is not an overload of the class's own `Validate` and must not
    share their name (issue #2619). None for a parameterless member that
    implements nothing explicitly, whose bare name is its leaf.
    """
    name, params = extract_method_signature(member_node)
    if not name:
        return None
    if interface := explicit_interface_name(member_node):
        separator = cs.CSHARP_EXPLICIT_IMPL_SEPARATOR
        name = f"{interface.replace(cs.SEPARATOR_DOT, separator)}{separator}{name}"
    elif not params:
        return None
    signature = (
        f"{cs.CHAR_PAREN_OPEN}{cs.SEPARATOR_COMMA_SPACE.join(params)}{cs.CHAR_PAREN_CLOSE}"
        if params
        else ""
    )
    return f"{name}{signature}"


def _generic_base(node: Node) -> CSharpGenericBase | None:
    # A base list entry that passes type arguments, else None: a plain base
    # binds no parameter. A qualified base carries them on its last segment,
    # a record's positional base on its type.
    if node.type in (
        cs.TS_CSHARP_QUALIFIED_NAME,
        cs.TS_CSHARP_PRIMARY_CONSTRUCTOR_BASE_TYPE,
    ):
        inner = (
            node.child_by_field_name(cs.FIELD_NAME)
            if node.type == cs.TS_CSHARP_QUALIFIED_NAME
            else next(iter(node.named_children), None)
        )
        return _generic_base(inner) if inner is not None else None
    if node.type != cs.TS_CSHARP_GENERIC_NAME:
        return None
    name_node = next(
        (c for c in node.children if c.type == cs.TS_CSHARP_IDENTIFIER), None
    )
    arg_list = next(
        (c for c in node.children if c.type == cs.TS_CSHARP_TYPE_ARGUMENT_LIST), None
    )
    name = safe_decode_text(name_node) if name_node is not None else None
    if not name or arg_list is None:
        return None
    arguments = tuple(
        signature_type_name(text)
        for arg in arg_list.named_children
        if (text := safe_decode_text(arg))
    )
    return CSharpGenericBase(name, arguments) if arguments else None


def type_parameter_names(parameter_list: Node | None) -> tuple[str, ...]:
    """The names a `type_parameter_list` declares: `<[A] T, out U>` ->
    ("T", "U"); () for no list."""
    if parameter_list is None:
        return ()
    return tuple(
        name
        for param in parameter_list.named_children
        if (name := safe_decode_text(param.child_by_field_name(cs.FIELD_NAME)))
    )


def generic_shape(type_node: Node) -> CSharpGenericShape | None:
    """`class PersonValidator : Inline<Person>` -> no parameters and the
    base `Inline<Person>`; `class Inline<T> : Validator<T>` -> `T` and
    `Validator<T>`. None for a type that takes and passes no type argument,
    which is most of them."""
    parameters: list[str] = []
    bases: list[CSharpGenericBase] = []
    for child in type_node.children:
        if child.type == cs.TS_CSHARP_TYPE_PARAMETER_LIST:
            parameters.extend(type_parameter_names(child))
        elif child.type == cs.TS_CSHARP_BASE_LIST:
            bases.extend(
                base
                for entry in child.named_children
                if (base := _generic_base(entry)) is not None
            )
    if not (parameters or bases):
        return None
    return CSharpGenericShape(tuple(parameters), tuple(bases))


def split_explicit_member(leaf: str) -> tuple[str, str] | None:
    """(`IValidator`, `Validate(Ctx)`) for the leaf `IValidator#Validate(Ctx)`.

    The interface comes back with its dots restored; None for a leaf that
    is not an explicit interface implementation.
    """
    head, paren, params = leaf.partition(cs.CHAR_PAREN_OPEN)
    interface, separator, name = head.rpartition(cs.CSHARP_EXPLICIT_IMPL_SEPARATOR)
    if not (separator and interface and name):
        return None
    return (
        interface.replace(separator, cs.SEPARATOR_DOT),
        f"{name}{paren}{params}",
    )


def names_interface(written: str, interface_path: str, interface_arity: int) -> bool:
    """Whether the interface an explicit implementation spells (`IRun`,
    `B.IRun`, `IRun<T>`) can be the one whose namespace-qualified name is
    `interface_path` (`B.IRun`).

    Every segment the spelling writes must agree, so `A.IRun.Go` and
    `B.IRun.Go` on one class stay with their own interfaces (issue #2619),
    and so must the generic arity.
    """
    path = written.rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
    segments = strip_generic_arguments(path).split(cs.SEPARATOR_DOT)
    target = strip_generic_arguments(interface_path).split(cs.SEPARATOR_DOT)
    return (
        target[-len(segments) :] == segments
        and generic_arity_of_type_text(path) == interface_arity
    )


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


def _file_scoped_namespace(unit: Node) -> str | None:
    # A file-scoped `namespace N;` is a SIBLING of the declarations it
    # governs under the compilation unit, not their ancestor.
    for child in unit.children:
        if child.type == cs.TS_CSHARP_FILE_SCOPED_NAMESPACE_DECLARATION:
            return _declared_name(child)
    return None


def _scope_of(node: Node) -> tuple[bool, str] | None:
    # (is namespace, name) when `node` is a scope the qualified name walks.
    if node.type == cs.TS_CSHARP_NAMESPACE_DECLARATION:
        name = _declared_name(node)
        return (True, name) if name else None
    if node.type in _CSHARP_TYPE_DECLARATIONS:
        name = _declared_name(node)
        return (False, name) if name else None
    if node.type == cs.TS_CSHARP_COMPILATION_UNIT:
        name = _file_scoped_namespace(node)
        return (True, name) if name else None
    return None


def _enclosing_scopes(node: Node) -> tuple[list[str], list[str]]:
    # (namespace segments, enclosing type names) of `node`, outermost first.
    # Block namespaces are ancestors and nest; the file-scoped one is read
    # from the compilation unit.
    namespaces: list[str] = []
    types: list[str] = []
    current = node.parent
    while current is not None:
        scope = _scope_of(current)
        if scope is not None:
            (namespaces if scope[0] else types).append(scope[1])
        current = current.parent
    namespaces.reverse()
    types.reverse()
    return namespaces, types


def declared_namespace(node: Node) -> str | None:
    """The dotted namespace `node` is declared in, or None at the top level."""
    namespaces, _types = _enclosing_scopes(node)
    return cs.SEPARATOR_DOT.join(namespaces) if namespaces else None


def namespace_qualified_name(type_node: Node) -> str | None:
    """`N1.Outer.Widget` for a type declaration: namespace, enclosing types,
    own name. Read from the declaration rather than the qualified name,
    because a namespace the module's directory already spells is not in the
    qn (issue #1629). None for a declaration with no name: its enclosing
    path alone would be the key of the type that encloses it (bot review)."""
    own = _declared_name(type_node)
    if not own:
        return None
    namespaces, types = _enclosing_scopes(type_node)
    return cs.SEPARATOR_DOT.join([*namespaces, *types, own])


def unique_carrier(
    carriers: set[str] | None, partial_groups: dict[str, list[str]]
) -> str | None:
    """The one class a declared name (`N.Widget`) names, or None.

    Several carriers are either the parts of ONE partial type, any of which
    spans the group, or two independent projects that each declare the name
    in their own directory; the latter must not be merged across assembly
    boundaries, so it is left unresolved like every other ambiguity here
    (bot review on #1999).
    """
    if not carriers:
        return None
    if len(carriers) == 1:
        return next(iter(carriers))
    ordered = sorted(carriers)
    group = partial_groups.get(ordered[0])
    if group is not None and all(partial_groups.get(qn) is group for qn in ordered):
        return ordered[0]
    return None


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
