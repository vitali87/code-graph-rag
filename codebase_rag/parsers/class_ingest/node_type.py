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
    if "export struct " in node_text:
        logger.info(
            logs.CLASS_FOUND_EXPORTED_STRUCT.format(name=class_name, qn=class_qn)
        )
    elif "export union " in node_text:
        logger.info(
            logs.CLASS_FOUND_EXPORTED_UNION.format(name=class_name, qn=class_qn)
        )
    elif "export template" in node_text:
        logger.info(
            logs.CLASS_FOUND_EXPORTED_TEMPLATE.format(name=class_name, qn=class_qn)
        )
    else:
        logger.info(
            logs.CLASS_FOUND_EXPORTED_CLASS.format(name=class_name, qn=class_qn)
        )


def extract_template_class_type(template_node: Node) -> NodeType | None:
    for child in template_node.children:
        if child.type in cs.CPP_CLASS_TYPES:
            return NodeType.CLASS
        elif child.type == cs.TS_ENUM_SPECIFIER:
            return NodeType.ENUM
        elif child.type == cs.TS_UNION_SPECIFIER:
            return NodeType.UNION
    return None
