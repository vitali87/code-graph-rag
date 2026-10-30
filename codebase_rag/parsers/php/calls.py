"""PHP's share of the call pass: the class a `new C(...)` constructs and the
method a call on a receiver the site shows was built by `new C` binds, both
named through PHP's namespace and `use` rules, and the qualified name an
anonymous class is registered under.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and hands PHP's part to the `PhpCallResolution` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LANGUAGE_FQN_SPECS
from ...utils.fqn_resolver import scoped_name_parts
from ..language_calls import LanguageCalls
from . import class_names
from . import utils as php_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class PhpCallHost(Protocol):
    """What PHP call resolution calls on the call pass that builds it: the
    note of how surely a resolver answer binds the call node being scanned."""

    def _noted[R: tuple[str, str] | None](self, resolved: R) -> R: ...


class PhpCallScan(Protocol):
    """The caller a PHP call node is scanned in, and the receiver classes
    read on it once, on first use, for all of that caller's calls."""

    module_qn: str
    php_scope_classes: class_names.ScopeCache | None


class PhpClassCallee(NamedTuple):
    # What a PHP call whose class the site shows binds to. `decided` False
    # leaves the call to the name-based resolver; True means `callee` is the
    # answer, and None there is a known non-edge.
    decided: bool
    callee: tuple[str, str] | None


def anonymous_class_qn(node: Node, module_qn: str) -> str | None:
    """The qn the definition pass registers an anonymous class under.

    The call pass's own class-qn builder walks class ancestors only and
    would drop the enclosing callables, so it asks for this instead.
    """
    if node.type != cs.TS_PHP_ANONYMOUS_CLASS:
        return None
    parts = scoped_name_parts(
        node, LANGUAGE_FQN_SPECS[cs.SupportedLanguage.PHP], module_qn, None
    )
    return cs.SEPARATOR_DOT.join([module_qn, *parts])


class PhpCallResolution(LanguageCalls):
    """Binds the PHP calls whose class the site shows: a construction, and a
    member call on a receiver the site shows was constructed.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = ("_host", "_resolver")

    def __init__(self, host: PhpCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver

    def class_name(self, class_node: Node) -> str | None:
        # An anonymous class is named as the definition pass names it, so its
        # methods get a class pass and the module pass leaves them alone
        # (issue #2538).
        return php_utils.anonymous_class_name(class_node) or super().class_name(
            class_node
        )

    def class_qn(self, class_node: Node, module_qn: str, class_name: str) -> str | None:
        return anonymous_class_qn(class_node, module_qn)

    def node_call_name(self, call_node: Node) -> str | None:
        # `new Box(...)` writes the class as an unfielded child, so no shared
        # shape names it and construction emitted nothing (issue #2466). The
        # class's own simple name, after `use` aliases, is what a file added
        # later would define; the resolver reads the full namespace path off
        # the node.
        if call_node.type == cs.TS_PHP_OBJECT_CREATION_EXPRESSION and (
            path := class_names.new_expression_class_path(call_node)
        ):
            return path[-1]
        return None

    def resolve_class_callee(
        self, ctx: PhpCallScan, call_node: Node, call_name: str
    ) -> PhpClassCallee:
        # `new C(...)` is always decided here; a member call only when its
        # receiver's class is known, so a miss keeps the name-only fallback.
        if call_node.type == cs.TS_PHP_OBJECT_CREATION_EXPRESSION:
            return PhpClassCallee(True, self._new_callee(ctx, call_node))
        if call_node.type in cs.PHP_MEMBER_CALL_TYPES and (
            typed := self._typed_member(ctx, call_node, call_name)
        ):
            return PhpClassCallee(True, typed)
        return PhpClassCallee(False, None)

    def _new_callee(self, ctx: PhpCallScan, call_node: Node) -> tuple[str, str] | None:
        # `new C(...)` names a class through PHP's namespace and `use` rules
        # alone; the generic resolver would judge a `use`d namespace path
        # external, or rebind the bare name to a function (issue #2466).
        path = class_names.new_expression_class_path(call_node)
        if not path:
            return None
        return self._host._noted(
            self._resolver.resolve_php_class_path(path, ctx.module_qn)
        )

    def _typed_member(
        self, ctx: PhpCallScan, call_node: Node, call_name: str
    ) -> tuple[str, str] | None:
        # A receiver the site shows was built by `new C` has class C, so the
        # method binds on C (or what C inherits) rather than by bare name. A
        # miss here leaves the name-only fallback exactly as it was.
        if ctx.php_scope_classes is None:
            ctx.php_scope_classes = {}
        path = class_names.receiver_class_path(call_node, ctx.php_scope_classes)
        if not path:
            return None
        return self._host._noted(
            self._resolver.resolve_php_method_on_class_path(
                path, call_name, ctx.module_qn
            )
        )
