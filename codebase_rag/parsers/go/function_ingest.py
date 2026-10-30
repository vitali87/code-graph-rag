"""Go's share of the function pass: the receiver methods it holds until
every receiver type is registered and then binds to that type, the name
token a Go definition's location is also keyed at, a free function's
return types, and the receiver type a function nested in a method belongs
under.

`FunctionIngest` owns what every language shares (the scan of a file's
function captures, a function's identity, node and DEFINES edge) and hands
Go's part to the `GoFunctionIngest` it builds."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_text
from ...types_defs import (
    DeferredParentLink,
    FunctionLocation,
)
from ..language_definitions import LanguageFunctionIngest
from ..method_ingest import MethodIngestHost, ingest_method
from . import interfaces as go_interfaces
from . import utils as go_utils
from .builds import GoBuilds

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import FunctionSpanKey, LanguageQueries
    from ..definition_context import DefinitionContext


class GoFunctionHost(Protocol):
    """What Go function ingestion calls on the function pass that builds it:
    the name a definition node takes, which a deferred method's return type
    is recorded under."""

    def _extract_function_name(self, func_node: Node) -> str | None: ...


class _DeferredMethod(NamedTuple):
    """Go receiver method, linked to its receiver type once all types are known."""

    method_node: Node
    module_qn: str
    receiver_type: str
    file_path: Path | None
    lang_queries: LanguageQueries | None = None


# Go node labels a receiver type can resolve to (struct -> Class, defined
# type/alias -> Type, interface -> Interface); picks the declaring type out of
# same-named candidates when binding a cross-file method.
_TYPE_NODE_TYPES = frozenset(
    {cs.NodeLabel.CLASS, cs.NodeLabel.TYPE, cs.NodeLabel.INTERFACE}
)


class GoFunctionIngest(LanguageFunctionIngest):
    """Binds Go receiver methods to their receiver type once every type is
    registered, and writes what a Go function records beside the shared
    function pass.

    Built and owned by `FunctionIngest`. The registries, the module paths,
    the definition facts, the builds and the method specs are the pass's
    own, held by reference: the Go interface link reads the specs this
    fills."""

    __slots__ = (
        "_host",
        "_method_host",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "facts",
        "builds",
        "methods",
        "_deferred_parent_links",
        "_deferred_methods",
    )

    def __init__(
        self,
        host: GoFunctionHost,
        context: DefinitionContext,
        *,
        method_host: MethodIngestHost,
        builds: GoBuilds,
        methods: dict[str, go_interfaces.GoMethodSpec],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        self._host = host
        # What a receiver method is written through.
        self._method_host = method_host
        self.repo_path = context.repo_path
        self.function_registry = context.function_registry
        self.simple_name_lookup = context.simple_name_lookup
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.facts = context.facts
        self.builds = builds
        self.methods = methods
        # Shared with the class pass, which emits them once every parent
        # is registered.
        self._deferred_parent_links = deferred_parent_links
        self._deferred_methods: list[_DeferredMethod] = []

    def skips_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> bool:
        # A receiver method waits for its receiver type, which a file parsed
        # later may declare (resolve_deferred_methods).
        if not go_utils.is_receiver_method(func_node):
            return False
        receiver_type = go_utils.extract_receiver_type_name(func_node)
        if not receiver_type:
            return False
        self._deferred_methods.append(
            _DeferredMethod(
                method_node=func_node,
                module_qn=module_qn,
                receiver_type=receiver_type,
                file_path=self.module_qn_to_file_path.get(module_qn),
            )
        )
        return True

    def resolve_deferred_methods(self) -> int:
        """Ingest Go receiver methods now that every receiver type is registered.

        A Go method (``func (p Point) Area()``) is declared at file scope, not
        inside its receiver type, so the receiver's node may not exist yet when
        the method is first seen. Deferring to after Pass 2 lets the method bind
        to the actual node label (``Class`` for structs, ``Type`` for defined
        types, ``Interface`` for interfaces). Returns the number ingested.
        """
        deferred = self._deferred_methods
        if not deferred:
            return 0

        for entry in deferred:
            container_qn = self.container_qn(entry.module_qn, entry.receiver_type)
            container_type = self.function_registry.get(container_qn)
            container_label = (
                container_type if container_type is not None else cs.NodeLabel.CLASS
            )
            method_qn = ingest_method(
                self._method_host,
                entry.method_node,
                container_qn,
                container_label,
                cs.SupportedLanguage.GO,
                file_path=entry.file_path,
                # An undeclared receiver keeps the same-file fallback qn; the
                # deferred link verifies the container and anchors to the
                # module instead of a phantom the database would drop.
                defer_containment=self._deferred_parent_links,
                module_qn=entry.module_qn,
            )
            if method_qn is not None:
                self.register_name_alias(
                    entry.module_qn,
                    entry.method_node,
                    FunctionLocation(
                        label=cs.NodeLabel.METHOD.value,
                        qualified_name=method_qn,
                        container_qn=container_qn,
                    ),
                )
                rel_file = go_utils.rel_file(entry.file_path, self.repo_path)
                self.methods[method_qn] = go_interfaces.GoMethodSpec(
                    go_interfaces.signature(entry.method_node),
                    go_utils.package_key(
                        rel_file, self.facts.go_package_names.get(entry.module_qn)
                    ),
                    rel_file,
                )
            # Record the method's return type so a chained call `c.Root().Run()`
            # resolves `Run` on the type `Root()` returns.
            method_name = self._host._extract_function_name(entry.method_node)
            if method_name and (
                return_type := go_utils.extract_return_type_name(entry.method_node)
            ):
                self.facts.method_return_types[
                    f"{container_qn}{cs.SEPARATOR_DOT}{method_name}"
                ] = return_type
        ingested = len(deferred)
        self._deferred_methods = []
        return ingested

    def container_qn(self, module_qn: str, receiver_type: str) -> str:
        # A method binds to its receiver type. Prefer the same-file type, but a Go
        # package spans every file in its directory, so fall back to a sibling-file
        # type with the same name in the same package. This keeps the method's qn
        # and DEFINES_METHOD parent anchored to the real type node, not a phantom
        # under the method's own module.
        same_file = f"{module_qn}{cs.SEPARATOR_DOT}{receiver_type}"
        if self.function_registry.get(same_file) is not None:
            return same_file
        method_file = self.module_qn_to_file_path.get(module_qn)
        visible = sorted(
            qn
            for qn in self.simple_name_lookup.get(receiver_type, set())
            if self.function_registry.get(qn) in _TYPE_NODE_TYPES
            and self._type_visible_from(
                qn.rsplit(cs.SEPARATOR_DOT, 1)[0], module_qn, method_file
            )
        )
        if len(visible) > 1:
            # A type each build declares again (`t_linux.go`, `t_windows.go`):
            # the one a build compiling this file holds, else the first by
            # name, never whichever a set yields first, which changes with
            # the process (#3360).
            method_rel = go_utils.rel_file(method_file, self.repo_path)
            building = [
                qn
                for qn in visible
                if self.builds.together((method_rel, self._type_rel_file(qn)))
            ]
            visible = building or visible
        return visible[0] if visible else same_file

    def _type_rel_file(self, type_qn: str) -> str:
        return go_utils.rel_file(
            self.module_qn_to_file_path.get(type_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]),
            self.repo_path,
        )

    def _type_visible_from(
        self, type_module: str, method_module: str, method_file: Path | None
    ) -> bool:
        # Go package membership is (directory, `package` clause). A `_test.go`
        # declaration is additionally compiled only under `go test`, so it is
        # invisible to non-test files even inside the same package.
        type_file = self.module_qn_to_file_path.get(type_module)
        method_is_test = method_file is not None and method_file.stem.endswith(
            cs.GO_TEST_FILE_SUFFIX
        )
        if (
            not method_is_test
            and type_file is not None
            and type_file.stem.endswith(cs.GO_TEST_FILE_SUFFIX)
        ):
            return False
        method_package = self.facts.go_package_names.get(method_module)
        type_package = self.facts.go_package_names.get(type_module)
        if (
            method_package is not None
            and type_package is not None
            and method_package != type_package
        ):
            return False
        if type_file is not None and method_file is not None:
            return type_file.parent == method_file.parent
        # Path-less registrations (mock harnesses) fall back to qn-prefix
        # grouping.
        return (
            type_module.rsplit(cs.SEPARATOR_DOT, 1)[0]
            == method_module.rsplit(cs.SEPARATOR_DOT, 1)[0]
        )

    def register_name_alias(
        self, module_qn: str, decl_node: Node, location: FunctionLocation
    ) -> None:
        # The go/types frontend (issue #1179) keys a call target at the callee
        # NAME identifier, but function_span_key keys at the `func` keyword
        # (col 0); register a SECOND location at the name token so the semantic
        # join resolves. The name col is never 0, so this never collides with the
        # span-key entry, and FunctionLocations.drop_module forgets it on an
        # incremental re-parse.
        name_node = decl_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None:
            return
        self.facts.function_locations[
            (module_qn, name_node.start_point[0] + 1, name_node.start_point[1])
        ] = location

    def record_return_type(self, func_node: Node, qualified_name: str) -> None:
        # Go free functions record their FIRST return type in a
        # dedicated map so `cm, err := getManager()` types cm under the
        # (T, error) idiom (viper's false Get->Get self edge). Not
        # method_return_types: chaining must keep skipping uncallable
        # multi-return callees.
        if return_type := go_utils.extract_first_return_type_name(func_node):
            self.facts.go_function_return_types[qualified_name] = return_type

        # Its SINGLE result does go there, as a Go method's does, so that
        # `NewBox().Bump()` types its receiver the way `c.Root().Run()`
        # does (issue #2467). Same extractor as the methods: a multi-value
        # or container result records nothing, so the skip above holds.
        if return_type := go_utils.extract_return_type_name(func_node):
            self.facts.method_return_types[qualified_name] = return_type

    def enclosing_parent(
        self, current: Node, func_qn: str, module_qn: str
    ) -> tuple[str, str, FunctionSpanKey | None] | None:
        # A Go receiver method's node lives under its receiver type
        # (module.Type.Method); identity resolution alone gives the
        # receiver-dropping module.Method, a phantom, so a local type
        # declared in the method body would fall back to the module instead
        # of its true lexical parent.
        if (
            go_utils.is_receiver_method(current)
            and (name_node := current.child_by_field_name(cs.FIELD_NAME)) is not None
            and (method_name := safe_decode_text(name_node))
            and (receiver_type := go_utils.extract_receiver_type_name(current))
        ):
            container_qn = self.container_qn(module_qn, receiver_type)
            return (
                cs.NodeLabel.METHOD,
                f"{container_qn}{cs.SEPARATOR_DOT}{method_name}",
                None,
            )
        return None
