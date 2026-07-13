from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def _normalize_type_name(text: str) -> str:
    # (H) Strip generic arguments (`List<int>` -> `List`), a nullable suffix
    # (H) (`Widget?`/`int?` -> the underlying type, so a nullable receiver still
    # (H) binds), and whitespace, so a parameter signature is stable and matches
    # (H) the registered, generic-free type names. Array brackets are kept (they
    # (H) distinguish overloads).
    return text.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip().rstrip(cs.CHAR_QUESTION_MARK)


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
    return _normalize_type_name(name) if name else None


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
                fields[name] = _normalize_type_name(type_text)
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
                    fields[name] = _normalize_type_name(type_text)
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
    return name


def extract_method_signature(method_node: Node) -> tuple[str | None, list[str]]:
    # (H) (method name, parameter type names). The name matches the leaf
    # (H) ingest_method registers (synthesized for operators/destructors), so the
    # (H) signatured qn stays consistent. Overloaded operators (`operator +` on
    # (H) two operand types) still get distinct qns via the parameter signature.
    return synthesize_method_name(method_node), extract_parameter_type_names(
        method_node
    )
