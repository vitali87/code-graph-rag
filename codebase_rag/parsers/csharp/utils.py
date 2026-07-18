from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def _first_attribute_list(node: Node) -> Node | None:
    # (H) First attribute_list in document order anywhere under `node` (pre-order
    # (H) DFS), so an attribute nested in an inner `#if` (a conditional block
    # (H) inside another) is still found, not only an immediate grandchild.
    if node.type == cs.TS_CSHARP_ATTRIBUTE_LIST:
        return node
    for child in node.children:
        if (found := _first_attribute_list(child)) is not None:
            return found
    return None


def definition_start_line(node: Node) -> int:
    # (H) The 1-based line a declaration truly starts on. When its attributes are
    # (H) wrapped in a conditional-compilation block (`#if SYMBOL [Attr] #endif`),
    # (H) tree-sitter nests a leading preproc_if_in_attribute_list child, so the
    # (H) declaration's own start_point is the `#if` directive line. Roslyn treats
    # (H) the directives as trivia and starts the span at the conditional
    # (H) attribute, so return that attribute's line (else the first non-directive
    # (H) child's line). Falls back to the node's own start for the common case
    # (H) with no leading directive.
    for child in node.children:
        if child.type == cs.TS_CSHARP_PREPROC_IF_IN_ATTR_LIST:
            if (attr_list := _first_attribute_list(child)) is not None:
                return attr_list.start_point[0] + 1
            continue
        return child.start_point[0] + 1
    return node.start_point[0] + 1


def _normalize_type_name(text: str) -> str:
    # (H) Strip generic arguments (`List<int>` -> `List`), a nullable suffix
    # (H) (`Widget?`/`int?` -> the underlying type, so a nullable receiver still
    # (H) binds), and whitespace, so a parameter signature is stable and matches
    # (H) the registered, generic-free type names. Array brackets are kept (they
    # (H) distinguish overloads).
    return text.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip().rstrip(cs.CHAR_QUESTION_MARK)


def generic_arity_of_type_text(text: str) -> int:
    # (H) Number of top-level type arguments in a type reference:
    # (H) `Builder` -> 0, `Builder<T>` -> 1, `Map<K, List<V>>` -> 2. Used to
    # (H) disambiguate same-simple-name generic/non-generic type declarations.
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
        elif ch == cs.SEPARATOR_COMMA and depth == 0:
            count += 1
    return count


GENERIC_ARITY_MARKER = "`"


def annotate_type_ref(text: str) -> str:
    # (H) Normalized type reference carrying its WRITTEN generic arity in CLR
    # (H) style (`Builder<T>` -> "Builder`1", `Builder` -> "Builder"): a plain
    # (H) name always means arity 0, so simple-name twins stay distinguishable
    # (H) through every stored type map without touching method signatures.
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
    # (H) A type node's normalized name (generic-free, nullable-stripped) or
    # (H) None for unnameable types (`void` callers never chain off it, but a
    # (H) void return is recorded harmlessly and simply never resolves).
    text = safe_decode_text(type_node)
    return _normalize_type_name(text) if text else None


def extract_parameter_type_names(method_node: Node) -> list[str]:
    # (H) The declared type of each parameter, in order, for the method-qn
    # (H) signature that keeps C# overloads distinct. A `params object[]` tail is
    # (H) not wrapped in a `parameter` node (grammar quirk); its `array_type`
    # (H) sits directly under the parameter_list, so capture that too.
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


def extension_receiver_type(method_node: Node) -> str | None:
    # (H) For an extension method, the normalized type of its receiver: the first
    # (H) parameter, whose first modifier is `this` (`static int WordCount(this
    # (H) string s)` -> "string"). Only extension methods carry `this` on a
    # (H) parameter, so its presence both identifies the method and names the
    # (H) receiver type a call binds against (`s.WordCount()`). Returns None for a
    # (H) non-extension method.
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
    # (H) Index an extension method by simple name + receiver type + declaring
    # (H) namespace so a `recv.Ext()` call binds to the static method even though it
    # (H) lives on an unrelated static class (not in recv's hierarchy). Shared by the
    # (H) class-member pass and the `#if`-truncation recovery so both stay in sync.
    # (H) No-op for a non-extension method (no `this` receiver).
    receiver_type = extension_receiver_type(method_node)
    if not receiver_type:
        return
    # (H) The receiver's WRITTEN generic arity (`this Builder<TResult>` -> 1),
    # (H) so a call receiver of known arity never binds an extension declared
    # (H) for the other twin.
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
    # (H) Strip the parameter signature BEFORE taking the leaf: a qualified param
    # (H) type (`Poke(N2.Widget)`) contains dots, so an rsplit-then-strip would key
    # (H) on `Widget)` instead of the method name `Poke` and never match.
    leaf = ingested_qn.split(cs.CHAR_PAREN_OPEN, 1)[0].rsplit(cs.SEPARATOR_DOT, 1)[-1]
    # (H) The extension's declaring namespace (its class's namespace-qualified name
    # (H) minus the class leaf) so an unqualified `this Widget` can resolve to
    # (H) `<namespace>.Widget` against a qualified call receiver. Empty for a
    # (H) top-level (namespace-less) class.
    ns_qualified_class = (
        class_qn[len(module_qn) + 1 :]
        if module_qn is not None
        and class_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
        else class_qn
    )
    ext_namespace = (
        ns_qualified_class.rsplit(cs.SEPARATOR_DOT, 1)[0]
        if cs.SEPARATOR_DOT in ns_qualified_class
        else ""
    )
    store.setdefault(leaf, []).append(
        (ingested_qn, receiver_type, ext_namespace, receiver_arity)
    )


def build_field_type_map(class_node: Node) -> dict[str, str]:
    # (H) {field-or-property name: type name} for members declared directly on
    # (H) this class body, recorded at ingestion so a receiver typed to a field
    # (H) (`_w.M()`) resolves -- including a field inherited from a base class in
    # (H) another file, reached by walking class_inheritance over these maps.
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    if body is None:
        return {}
    fields: dict[str, str] = {}
    for member in body.children:
        if member.type == cs.TS_CSHARP_PROPERTY_DECLARATION:
            name = safe_decode_text(member.child_by_field_name(cs.FIELD_NAME))
            type_text = safe_decode_text(member.child_by_field_name(cs.FIELD_TYPE))
            if name and type_text:
                fields[name] = annotate_type_ref(type_text)
        elif member.type == cs.TS_CSHARP_FIELD_DECLARATION:
            var_decl = next(
                (
                    c
                    for c in member.children
                    if c.type == cs.TS_CSHARP_VARIABLE_DECLARATION
                ),
                None,
            )
            if var_decl is None:
                continue
            type_text = safe_decode_text(var_decl.child_by_field_name(cs.FIELD_TYPE))
            if not type_text:
                continue
            for declarator in var_decl.children:
                if declarator.type != cs.TS_CSHARP_VARIABLE_DECLARATOR:
                    continue
                name = safe_decode_text(declarator.child_by_field_name(cs.FIELD_NAME))
                if name:
                    fields[name] = annotate_type_ref(type_text)
    return fields


def synthesize_method_name(method_node: Node) -> str | None:
    # (H) The registered leaf name for a C# member. Operators expose no `name`
    # (H) field, so synthesize `operator_<symbol>` (binary/unary operators) or
    # (H) `operator_<target-type>` (conversion operators). A destructor HAS a
    # (H) `name` field equal to the type name, which would collide with the
    # (H) constructor, so prefix `~`. Everything else uses the plain `name` leaf.
    # (H) Kept identical to _csharp_get_name so the FQN scope walk and the node
    # (H) qn agree.
    if method_node.type == cs.TS_CSHARP_OPERATOR_DECLARATION:
        op_node = method_node.child_by_field_name(cs.TS_CSHARP_FIELD_OPERATOR)
        symbol = safe_decode_text(op_node) if op_node and op_node.text else None
        return cs.TS_CSHARP_OPERATOR_NAME_PREFIX + symbol if symbol else None
    if method_node.type == cs.TS_CSHARP_CONVERSION_OPERATOR_DECLARATION:
        type_node = method_node.child_by_field_name(cs.TS_CSHARP_FIELD_TYPE)
        target = safe_decode_text(type_node) if type_node and type_node.text else None
        return cs.TS_CSHARP_OPERATOR_NAME_PREFIX + target if target else None
    name_node = method_node.child_by_field_name(cs.FIELD_NAME)
    name = safe_decode_text(name_node) if name_node and name_node.text else None
    if name and method_node.type == cs.TS_CSHARP_DESTRUCTOR_DECLARATION:
        return cs.TS_CSHARP_DESTRUCTOR_NAME_PREFIX + name
    # (H) A reserved keyword as the name means tree-sitter parse-recovered a broken
    # (H) construct (e.g. a `#if`-split `else if` chain -> local_function named
    # (H) `if`); it is never a real member, so drop it rather than pollute the graph.
    if name in cs.CSHARP_RESERVED_KEYWORDS:
        return None
    return name


def extract_method_signature(method_node: Node) -> tuple[str | None, list[str]]:
    # (H) (method name, parameter type names). The name matches the leaf
    # (H) ingest_method registers (synthesized for operators/destructors), so the
    # (H) signatured qn stays consistent. Overloaded operators (`operator +` on
    # (H) two operand types) still get distinct qns via the parameter signature.
    return synthesize_method_name(method_node), extract_parameter_type_names(
        method_node
    )
