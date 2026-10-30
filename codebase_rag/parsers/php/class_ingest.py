"""PHP's share of the class pass: the namespace around a class's
declaration, which other files `use` it by.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships) and records a PHP
class's namespace through the `PhpClassIngest` it builds; it holds no PHP
state."""

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import PropertyDict
from ..language_definitions import LanguageClassIngest
from . import namespaces as php_namespaces


class PhpClassIngest(LanguageClassIngest):
    """Records the namespace a PHP class is declared in.

    Built and owned by `ClassIngest`."""

    __slots__ = ()

    def record_type_props(
        self,
        class_node: Node,
        class_qn: str,
        class_name: str,
        label: cs.NodeLabel,
        class_props: PropertyDict,
    ) -> None:
        # A PHP class's qn follows its path, but other files `use` it by the
        # namespace around its declaration; the dead-code walk needs that name
        # to tell an imported project base from a vendor class of the same
        # name (issue #2472).
        if namespace := php_namespaces.enclosing_namespace(class_node):
            class_props[cs.KEY_NAMESPACE] = namespace
