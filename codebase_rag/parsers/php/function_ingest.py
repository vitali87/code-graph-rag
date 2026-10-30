"""PHP's share of the function pass: the path segment an anonymous class
gives a closure written in its methods, and where the walk naming that
closure resumes above the class.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge, and the
nested-qn walk) and names what a PHP anonymous class puts around a closure
through the `PhpFunctionIngest` it builds; it holds no PHP state."""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..language_definitions import LanguageFunctionIngest
from . import utils as php_utils


class PhpFunctionIngest(LanguageFunctionIngest):
    """Names the scope a PHP anonymous class gives a closure in its methods.

    Built and owned by `FunctionIngest`."""

    __slots__ = ()

    def anonymous_class_name(self, class_node: Node) -> str | None:
        # A PHP anonymous class is named as the definition pass names it,
        # under the callable it is written in (issue #2538).
        return php_utils.anonymous_class_scope_name(class_node)

    def next_path_ancestor(self, node: Node) -> Node | None:
        # A PHP anonymous class's part already spells the callable it is
        # written in, as that callable is registered; naming the callables
        # around it again here would put the closure under a different path
        # than the class's own methods (issue #2538).
        if node.type == cs.TS_PHP_ANONYMOUS_CLASS:
            return php_utils.anonymous_class_anchor_stop(node)
        return node.parent
