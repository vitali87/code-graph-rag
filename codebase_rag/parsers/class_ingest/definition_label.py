from __future__ import annotations

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ..node_text import safe_decode_with_fallback


def definition_label(
    class_node: Node,
    class_name: str | None,
    class_qn: str,
    language: cs.SupportedLanguage,
) -> cs.NodeLabel:
    match class_node.type:
        case cs.TS_GO_TYPE_SPEC | cs.TS_GO_TYPE_ALIAS if (
            language == cs.SupportedLanguage.GO
        ):
            return _go_type_node_type(class_node, class_name, class_qn)
        case cs.TS_INTERFACE_DECLARATION | cs.TS_RS_TRAIT_ITEM:
            logger.debug(ls.CLASS_FOUND_INTERFACE.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.INTERFACE
        case (
            cs.TS_ENUM_DECLARATION
            | cs.TS_ENUM_SPECIFIER
            | cs.TS_ENUM_CLASS_SPECIFIER
            | cs.TS_RS_ENUM_ITEM
        ):
            logger.debug(ls.CLASS_FOUND_ENUM.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.ENUM
        case cs.TS_TYPE_ALIAS_DECLARATION | cs.TS_RS_TYPE_ITEM:
            logger.debug(ls.CLASS_FOUND_TYPE.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.TYPE
        case cs.TS_STRUCT_SPECIFIER | cs.TS_RS_STRUCT_ITEM:
            logger.debug(ls.CLASS_FOUND_STRUCT.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.CLASS
        case cs.TS_UNION_SPECIFIER | cs.TS_RS_UNION_ITEM:
            logger.debug(ls.CLASS_FOUND_UNION.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.UNION
        case cs.CppNodeType.TEMPLATE_DECLARATION:
            node_type = extract_template_class_type(class_node) or cs.NodeLabel.CLASS
            logger.debug(
                ls.CLASS_FOUND_TEMPLATE.format(
                    node_type=node_type, name=class_name, qn=class_qn
                )
            )
            return node_type
        case cs.CppNodeType.FUNCTION_DEFINITION if language == cs.SupportedLanguage.CPP:
            log_exported_class_type(class_node, class_name, class_qn)
            return cs.NodeLabel.CLASS
        case _:
            logger.debug(ls.CLASS_FOUND_CLASS.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.CLASS


def _go_type_node_type(
    class_node: Node, class_name: str | None, class_qn: str
) -> cs.NodeLabel:
    underlying = class_node.child_by_field_name(cs.FIELD_TYPE)
    match underlying.type if underlying else None:
        case cs.TS_GO_STRUCT_TYPE:
            logger.debug(ls.CLASS_FOUND_STRUCT.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.CLASS
        case cs.TS_GO_INTERFACE_TYPE:
            logger.debug(ls.CLASS_FOUND_INTERFACE.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.INTERFACE
        case _:
            logger.debug(ls.CLASS_FOUND_TYPE.format(name=class_name, qn=class_qn))
            return cs.NodeLabel.TYPE


def log_exported_class_type(
    class_node: Node, class_name: str | None, class_qn: str
) -> None:
    node_text = safe_decode_with_fallback(class_node) if class_node.text else ""
    match _detect_export_type(node_text):
        case cs.CPP_EXPORT_STRUCT_PREFIX:
            logger.debug(
                ls.CLASS_FOUND_EXPORTED_STRUCT.format(name=class_name, qn=class_qn)
            )
        case cs.CPP_EXPORT_UNION_PREFIX:
            logger.debug(
                ls.CLASS_FOUND_EXPORTED_UNION.format(name=class_name, qn=class_qn)
            )
        case cs.CPP_EXPORT_TEMPLATE_PREFIX:
            logger.debug(
                ls.CLASS_FOUND_EXPORTED_TEMPLATE.format(name=class_name, qn=class_qn)
            )
        case _:
            logger.debug(
                ls.CLASS_FOUND_EXPORTED_CLASS.format(name=class_name, qn=class_qn)
            )


def _detect_export_type(node_text: str) -> str | None:
    return next(
        (prefix for prefix in cs.CPP_EXPORT_PREFIXES if prefix in node_text),
        None,
    )


def extract_template_class_type(template_node: Node) -> cs.NodeLabel | None:
    for child in template_node.children:
        match child.type:
            case cs.CppNodeType.CLASS_SPECIFIER | cs.TS_STRUCT_SPECIFIER:
                return cs.NodeLabel.CLASS
            case cs.TS_ENUM_SPECIFIER:
                return cs.NodeLabel.ENUM
            case cs.TS_UNION_SPECIFIER:
                return cs.NodeLabel.UNION
    return None
