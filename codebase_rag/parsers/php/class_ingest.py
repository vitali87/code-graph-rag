"""PHP's share of the class pass: the namespace around a class's
declaration, which other files `use` it by.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and records a PHP
class's namespace through `record_namespace`; it holds no PHP state."""

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from . import namespaces as php_namespaces


def record_namespace(class_node: Node, class_props: PropertyDict) -> None:
    # A PHP class's qn follows its path, but other files `use` it by the
    # namespace around its declaration; the dead-code walk needs that name
    # to tell an imported project base from a vendor class of the same
    # name (issue #2472).
    if namespace := php_namespaces.enclosing_namespace(class_node):
        class_props[cs.KEY_NAMESPACE] = namespace
