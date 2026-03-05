from __future__ import annotations

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs
from ...types_defs import NodeType
from ..utils import safe_decode_with_fallback


def determine_node_type(
    class_node: Node,
    class_name: str | None,
    class_qn: str,
    language: cs.SupportedLanguage,
) -> NodeType:
    match class_node.type:
        case cs.TS_INTERFACE_DECLARATION:
            logger.info(logs.CLASS_FOUND_INTERFACE.format(name=class_name, qn=class_qn))
            return NodeType.INTERFACE
        case cs.TS_ENUM_DECLARATION | cs.TS_ENUM_SPECIFIER | cs.TS_ENUM_CLASS_SPECIFIER:
            logger.info(logs.CLASS_FOUND_ENUM.format(name=class_name, qn=class_qn))
            return NodeType.ENUM
        case cs.TS_TYPE_ALIAS_DECLARATION:
            logger.info(logs.CLASS_FOUND_TYPE.format(name=class_name, qn=class_qn))
            return NodeType.TYPE
        case cs.TS_STRUCT_SPECIFIER:
            logger.info(logs.CLASS_FOUND_STRUCT.format(name=class_name, qn=class_qn))
            return NodeType.CLASS
        case cs.TS_UNION_SPECIFIER:
            logger.info(logs.CLASS_FOUND_UNION.format(name=class_name, qn=class_qn))
            return NodeType.UNION
        case cs.CppNodeType.TEMPLATE_DECLARATION:
            node_type = extract_template_class_type(class_node) or NodeType.CLASS
            logger.info(
                logs.CLASS_FOUND_TEMPLATE.format(
                    node_type=node_type, name=class_name, qn=class_qn
                )
            )
            return node_type
        case cs.CppNodeType.FUNCTION_DEFINITION if language == cs.SupportedLanguage.CPP:
            log_exported_class_type(class_node, class_name, class_qn)
            return NodeType.CLASS
        case _:
            logger.info(logs.CLASS_FOUND_CLASS.format(name=class_name, qn=class_qn))
            return NodeType.CLASS


def log_exported_class_type(
    class_node: Node, class_name: str | None, class_qn: str
) -> None:
    node_text = safe_decode_with_fallback(class_node) if class_node.text else ""
    match _detect_export_type(node_text):
        case cs.CPP_EXPORT_STRUCT_PREFIX:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_STRUCT.format(name=class_name, qn=class_qn)
            )
        case cs.CPP_EXPORT_UNION_PREFIX:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_UNION.format(name=class_name, qn=class_qn)
            )
        case cs.CPP_EXPORT_TEMPLATE_PREFIX:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_TEMPLATE.format(name=class_name, qn=class_qn)
            )
        case _:
            logger.info(
                logs.CLASS_FOUND_EXPORTED_CLASS.format(name=class_name, qn=class_qn)
            )


def _detect_export_type(node_text: str) -> str | None:
    return next(
        (prefix for prefix in cs.CPP_EXPORT_PREFIXES if prefix in node_text),
        None,
    )


def extract_template_class_type(template_node: Node) -> NodeType | None:
    for child in template_node.children:
        match child.type:
            case cs.CppNodeType.CLASS_SPECIFIER | cs.TS_STRUCT_SPECIFIER:
                return NodeType.CLASS
            case cs.TS_ENUM_SPECIFIER:
                return NodeType.ENUM
            case cs.TS_UNION_SPECIFIER:
                return NodeType.UNION
    return None


# (H) Mapping from tree-sitter AST node types to human-readable class_kind values
_CLASS_KIND_MAP: dict[str, str] = {
    "struct_declaration": "struct",
    "struct_specifier": "struct",
    "struct_item": "struct",
    "record_declaration": "record",
    "delegate_declaration": "delegate",
    "enum_declaration": "enum",
    "enum_specifier": "enum",
    "enum_class_specifier": "enum",
    "interface_declaration": "interface",
    "trait_declaration": "interface",
    "trait_item": "interface",
    "union_specifier": "union",
    "union_item": "union",
    "type_alias_declaration": "type_alias",
    "type_item": "type_alias",
}


def determine_class_kind(class_node: Node) -> str:
    """Return a human-readable sub-type for the class node.

    This preserves the original C#/Java/Rust/C++ structure kind
    (e.g. ``"struct"``, ``"record"``, ``"delegate"``) even when
    the graph label is normalised to ``Class``.
    """
    return _CLASS_KIND_MAP.get(class_node.type, "class")
