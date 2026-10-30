"""PHP's share of the file pass: the namespace a file declares, stored on its
Module node.

`DefinitionProcessor` owns what every language shares (a file's parse, its
Module node, and the function and class passes over it) and records a PHP
file's namespace through the `PhpFileIngest` it builds; it holds no PHP
state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFileIngest
from .namespaces import declared_namespace

if TYPE_CHECKING:
    from ...types_defs import PropertyDict


class PhpFileIngest(LanguageFileIngest):
    """Records the namespace a PHP file declares on its Module node.

    Built and owned by `DefinitionProcessor`."""

    __slots__ = ()

    def record_module_props(self, root_node: Node, module_props: PropertyDict) -> None:
        if namespace := declared_namespace(root_node):
            module_props[cs.KEY_PHP_NAMESPACE] = namespace
