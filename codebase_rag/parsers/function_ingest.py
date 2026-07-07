from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LANGUAGE_FQN_SPECS, LanguageSpec
from ..types_defs import (
    ASTNode,
    FunctionRegistryTrieProtocol,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from . import export_detection
from .cpp import utils as cpp_utils
from .go import utils as go_utils
from .lua import utils as lua_utils
from .rs import utils as rs_utils
from .utils import (
    callable_parameter_indices,
    get_function_captures,
    ingest_method,
    is_method_node,
    safe_decode_text,
)

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .handlers import LanguageHandler


class FunctionResolution(NamedTuple):
    qualified_name: str
    name: str
    is_exported: bool


class _DeferredMethod(NamedTuple):
    """Out-of-class C++ method whose class hasn't been parsed yet."""

    method_name: str
    class_name: str
    fallback_class_qn: str
    method_props: PropertyDict


class _DeferredGoMethod(NamedTuple):
    """Go receiver method, linked to its receiver type once all types are known."""

    method_node: Node
    module_qn: str
    receiver_type: str
    file_path: Path | None


# (H) Go node labels a receiver type can resolve to (struct -> Class, defined
# (H) type/alias -> Type, interface -> Interface); used to pick the declaring
# (H) type out of same-named candidates when binding a cross-file method.
_GO_TYPE_NODE_TYPES = frozenset({NodeType.CLASS, NodeType.TYPE, NodeType.INTERFACE})


class FunctionIngestMixin:
    __slots__ = ()
    ingestor: IngestorProtocol
    repo_path: Path
    project_name: str
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    _handler: LanguageHandler
    _deferred_cpp_methods: list[_DeferredMethod]
    _deferred_go_methods: list[_DeferredGoMethod]
    method_return_types: dict[str, str]

    @abstractmethod
    def _get_docstring(self, node: ASTNode) -> str | None: ...

    @abstractmethod
    def _extract_decorators(self, node: ASTNode) -> list[str]: ...

    def _ingest_all_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None = None,
    ) -> None:
        if combined_captures is not None:
            lang_queries = queries[language]
            lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]
            captures = combined_captures
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
        file_path = self.module_qn_to_file_path.get(module_qn)
        has_classes = bool(captures.get(cs.CAPTURE_CLASS))

        for func_node in captures.get(cs.CAPTURE_FUNCTION, []):
            if has_classes and self._is_method(func_node, lang_config):
                continue

            if language == cs.SupportedLanguage.CPP:
                if self._handle_cpp_out_of_class_method(func_node, module_qn):
                    continue

            if language == cs.SupportedLanguage.GO and self._defer_go_receiver_method(
                func_node, module_qn
            ):
                continue

            resolution = self._resolve_function_identity(
                func_node, module_qn, language, lang_config, file_path
            )
            if not resolution:
                continue

            self._register_function(
                func_node, resolution, module_qn, language, lang_config
            )

            # (H) Record a free C++ function's return type so a chained call off a
            # (H) factory (`make().run()`) can type the receiver and resolve the next
            # (H) hop. Runs here (not in the CPP resolver) because the unified-FQN path
            # (H) wins for C++ and would otherwise bypass the recording.
            if language == cs.SupportedLanguage.CPP and (
                return_type := cpp_utils.extract_return_type_name(func_node)
            ):
                self.method_return_types[resolution.qualified_name] = return_type

    def _resolve_function_identity(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        resolution = self._try_unified_fqn_resolution(
            func_node, module_qn, language, file_path
        )
        if resolution:
            return resolution

        return self._fallback_function_resolution(
            func_node, module_qn, language, lang_config
        )

    def _try_unified_fqn_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        file_path: Path | None,
    ) -> FunctionResolution | None:
        fqn_config = LANGUAGE_FQN_SPECS.get(language)
        if not fqn_config or not file_path:
            return None

        func_name = fqn_config.get_name(func_node)
        if not func_name:
            return None

        parts = [func_name]
        current = func_node.parent
        while current:
            if current.type in fqn_config.scope_node_types:
                if scope_name := fqn_config.get_name(current):
                    parts.append(scope_name)
            current = current.parent
        parts.reverse()

        # (H) Prefix with the module's resolved (collision-disambiguated) qn rather
        # (H) than recomputing from the path, so same-stem cross-language siblings
        # (H) stay distinct.
        func_qn = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(parts)
        simple_name = func_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        is_exported = export_detection.is_exported(func_node, simple_name, language)
        return FunctionResolution(func_qn, simple_name, is_exported)

    def _fallback_function_resolution(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution | None:
        if language == cs.SupportedLanguage.CPP:
            return self._resolve_cpp_function(func_node, module_qn)
        return self._resolve_generic_function(
            func_node, module_qn, language, lang_config
        )

    def _resolve_cpp_class_qn(
        self, class_name: str, module_qn: str
    ) -> tuple[str, bool]:
        """Look up an existing Class node for *class_name* across all parsed files.

        Returns ``(class_qn, resolved)`` where *resolved* is True when the
        qualified name was obtained from the function registry (i.e. the
        class has already been parsed, typically from a header file).
        """
        class_name_normalized = class_name.replace(
            cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT
        )
        leaf_name = class_name_normalized.rsplit(cs.SEPARATOR_DOT, 1)[-1]

        if leaf_name in self.simple_name_lookup:
            for candidate_qn in self.simple_name_lookup[leaf_name]:
                node_type = self.function_registry.get(candidate_qn)
                if node_type in {NodeType.CLASS, NodeType.TYPE}:
                    if candidate_qn.endswith(
                        f".{class_name_normalized}"
                    ) and self._is_cpp_defined(candidate_qn):
                        return candidate_qn, True

        return f"{module_qn}.{class_name_normalized}", False

    def _is_cpp_defined(self, qn: str) -> bool:
        # (H) A C++ out-of-class method may only bind to a class defined in a
        # (H) C/C++ source file; matching a same-named class in another language
        # (H) would collide their qualified names. Resolve qn -> defining file by
        # (H) the longest module-qn prefix and check its extension.
        parts = qn.split(cs.SEPARATOR_DOT)
        while parts:
            if path := self.module_qn_to_file_path.get(cs.SEPARATOR_DOT.join(parts)):
                return (
                    path.suffix in cs.CPP_EXTENSIONS or path.suffix in cs.C_EXTENSIONS
                )
            parts = parts[:-1]
        return False

    def _handle_cpp_out_of_class_method(self, func_node: Node, module_qn: str) -> bool:
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return False

        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_node)
        if not class_name:
            return False

        class_qn, resolved = self._resolve_cpp_class_qn(class_name, module_qn)
        file_path = self.module_qn_to_file_path.get(module_qn)

        if resolved:
            ingest_method(
                method_node=func_node,
                container_qn=class_qn,
                container_type=cs.NodeLabel.CLASS,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                get_docstring_func=self._get_docstring,
                language=cs.SupportedLanguage.CPP,
                extract_decorators_func=self._extract_decorators,
                file_path=file_path,
                repo_path=self.repo_path,
            )
        else:
            method_name = cpp_utils.extract_function_name(func_node)
            if not method_name:
                return True
            decorators = self._extract_decorators(func_node)
            props: PropertyDict = {
                cs.KEY_NAME: method_name,
                cs.KEY_DECORATORS: decorators,
                cs.KEY_START_LINE: func_node.start_point[0] + 1,
                cs.KEY_END_LINE: func_node.end_point[0] + 1,
                cs.KEY_DOCSTRING: self._get_docstring(func_node),
            }
            if file_path is not None and self.repo_path is not None:
                props[cs.KEY_PATH] = cached_relative_path(
                    file_path, self.repo_path
                ).as_posix()
                props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
            if not hasattr(self, "_deferred_cpp_methods"):
                self._deferred_cpp_methods = []
            self._deferred_cpp_methods.append(
                _DeferredMethod(
                    method_name=method_name,
                    class_name=class_name,
                    fallback_class_qn=class_qn,
                    method_props=props,
                )
            )

        return True

    def resolve_deferred_cpp_methods(self) -> int:
        """Ingest deferred out-of-class C++ methods now that all classes are known.

        Called after all files have been parsed so that every Class node
        is guaranteed to be in the registry.  Returns the number of
        methods that were ingested.
        """
        deferred = getattr(self, "_deferred_cpp_methods", None)
        if not deferred:
            return 0

        ingested = 0
        for entry in deferred:
            real_class_qn, resolved = self._resolve_cpp_class_qn(entry.class_name, "")
            class_qn = real_class_qn if resolved else entry.fallback_class_qn
            method_qn = f"{class_qn}.{entry.method_name}"

            props = dict(entry.method_props)
            props[cs.KEY_QUALIFIED_NAME] = method_qn

            logger.info(ls.METHOD_FOUND.format(name=entry.method_name, qn=method_qn))
            self.ingestor.ensure_node_batch(cs.NodeLabel.METHOD, props)
            self.function_registry[method_qn] = NodeType.METHOD
            self.simple_name_lookup[entry.method_name].add(method_qn)

            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_qn),
                cs.RelationshipType.DEFINES_METHOD,
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
            )
            ingested += 1

        self._deferred_cpp_methods = []
        return ingested

    def _defer_go_receiver_method(self, func_node: Node, module_qn: str) -> bool:
        if not go_utils.is_receiver_method(func_node):
            return False
        receiver_type = go_utils.extract_receiver_type_name(func_node)
        if not receiver_type:
            return False
        if not hasattr(self, "_deferred_go_methods"):
            self._deferred_go_methods = []
        self._deferred_go_methods.append(
            _DeferredGoMethod(
                method_node=func_node,
                module_qn=module_qn,
                receiver_type=receiver_type,
                file_path=self.module_qn_to_file_path.get(module_qn),
            )
        )
        return True

    def _resolve_go_container_qn(self, module_qn: str, receiver_type: str) -> str:
        # (H) A method binds to its receiver type. Prefer the same-file type, but
        # (H) a Go package spans every file in its directory, so fall back to a
        # (H) sibling-file type with the same name in the same package. This keeps
        # (H) the method's qn and DEFINES_METHOD parent anchored to the real type
        # (H) node instead of a phantom under the method's own module.
        same_file = f"{module_qn}{cs.SEPARATOR_DOT}{receiver_type}"
        if self.function_registry.get(same_file) is not None:
            return same_file
        package = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        for qn in self.simple_name_lookup.get(receiver_type, set()):
            if self.function_registry.get(qn) not in _GO_TYPE_NODE_TYPES:
                continue
            type_module = qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if type_module.rsplit(cs.SEPARATOR_DOT, 1)[0] == package:
                return qn
        return same_file

    def resolve_deferred_go_methods(self) -> int:
        """Ingest Go receiver methods now that every receiver type is registered.

        A Go method (``func (p Point) Area()``) is declared at file scope, not
        inside its receiver type, so the receiver's node may not exist yet when
        the method is first seen. Deferring to after Pass 2 lets the method bind
        to the actual node label (``Class`` for structs, ``Type`` for defined
        types, ``Interface`` for interfaces). Returns the number ingested.
        """
        deferred = getattr(self, "_deferred_go_methods", None)
        if not deferred:
            return 0

        for entry in deferred:
            container_qn = self._resolve_go_container_qn(
                entry.module_qn, entry.receiver_type
            )
            container_type = self.function_registry.get(container_qn)
            container_label = (
                cs.NodeLabel(container_type.value)
                if container_type is not None
                else cs.NodeLabel.CLASS
            )
            ingest_method(
                method_node=entry.method_node,
                container_qn=container_qn,
                container_type=container_label,
                ingestor=self.ingestor,
                function_registry=self.function_registry,
                simple_name_lookup=self.simple_name_lookup,
                get_docstring_func=self._get_docstring,
                language=cs.SupportedLanguage.GO,
                file_path=entry.file_path,
                repo_path=self.repo_path,
            )
            # (H) Record the method's return type so a chained call `c.Root().Run()`
            # (H) can resolve `Run` on the type `Root()` returns.
            method_name = self._extract_function_name(entry.method_node)
            if method_name and (
                return_type := go_utils.extract_return_type_name(entry.method_node)
            ):
                self.method_return_types[
                    f"{container_qn}{cs.SEPARATOR_DOT}{method_name}"
                ] = return_type
        ingested = len(deferred)
        self._deferred_go_methods = []
        return ingested

    def _resolve_cpp_function(
        self, func_node: Node, module_qn: str
    ) -> FunctionResolution | None:
        func_name = cpp_utils.extract_function_name(func_node)
        if not func_name:
            if func_node.type == cs.TS_CPP_LAMBDA_EXPRESSION:
                func_name = f"{cs.PREFIX_LAMBDA}{func_node.start_point[0]}_{func_node.start_point[1]}"
            else:
                return None

        func_qn = cpp_utils.build_qualified_name(func_node, module_qn, func_name)
        is_exported = cpp_utils.is_exported(func_node)
        return FunctionResolution(func_qn, func_name, is_exported)

    def _resolve_generic_function(
        self,
        func_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> FunctionResolution:
        func_name = self._extract_function_name(func_node)

        if (
            not func_name
            and language == cs.SupportedLanguage.LUA
            and func_node.type == cs.TS_LUA_FUNCTION_DEFINITION
        ):
            func_name = self._extract_lua_assignment_function_name(func_node)

        if not func_name:
            func_name = self._generate_anonymous_function_name(func_node, module_qn)

        func_qn = self._build_function_qn(
            func_node, module_qn, func_name, language, lang_config
        )
        is_exported = export_detection.is_exported(func_node, func_name, language)
        return FunctionResolution(func_qn, func_name, is_exported)

    def _build_function_qn(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> str:
        if language == cs.SupportedLanguage.RUST:
            return self._build_rust_function_qualified_name(
                func_node, module_qn, func_name
            )

        nested_qn = self._build_nested_qualified_name(
            func_node, module_qn, func_name, lang_config
        )
        return nested_qn or f"{module_qn}.{func_name}"

    def _register_function(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        unique_qn = self.function_registry.register_unique_qn(
            resolution.qualified_name, func_node.start_point[0] + 1
        )
        if unique_qn != resolution.qualified_name:
            resolution = resolution._replace(qualified_name=unique_qn)

        func_props = self._build_function_props(func_node, resolution, module_qn)
        logger.info(
            ls.FUNC_FOUND.format(name=resolution.name, qn=resolution.qualified_name)
        )
        self.ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, func_props)

        self.function_registry[resolution.qualified_name] = NodeType.FUNCTION
        self.function_registry.mark_callable_params(
            resolution.qualified_name,
            callable_parameter_indices(func_node, language),
        )
        if resolution.name:
            self.simple_name_lookup[resolution.name].add(resolution.qualified_name)

        self._create_function_relationships(
            func_node, resolution, module_qn, language, lang_config
        )

    def _build_function_props(
        self, func_node: Node, resolution: FunctionResolution, module_qn: str
    ) -> PropertyDict:
        file_path = self.module_qn_to_file_path.get(module_qn)
        props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: resolution.qualified_name,
            cs.KEY_NAME: resolution.name,
            cs.KEY_DECORATORS: self._extract_decorators(func_node),
            cs.KEY_START_LINE: func_node.start_point[0] + 1,
            cs.KEY_END_LINE: func_node.end_point[0] + 1,
            cs.KEY_DOCSTRING: self._get_docstring(func_node),
            cs.KEY_IS_EXPORTED: resolution.is_exported,
        }
        if file_path is not None:
            props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        return props

    def _create_function_relationships(
        self,
        func_node: Node,
        resolution: FunctionResolution,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_config: LanguageSpec,
    ) -> None:
        parent_type, parent_qn = self._determine_function_parent(
            func_node, resolution.qualified_name, module_qn, lang_config, language
        )
        self.ingestor.ensure_relationship_batch(
            (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
            cs.RelationshipType.DEFINES,
            (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, resolution.qualified_name),
        )

        # (H) A Rust closure is a value constructed at its definition site (a `.map`
        # (H) arg, spawn body, `let` binding), so it is used wherever it is written.
        # (H) Dead-code reachability walks CALLS/REFERENCES (not DEFINES), so mirror the
        # (H) DEFINES with a REFERENCES from the enclosing scope -- else every closure is
        # (H) an orphan and reports dead. (JS/TS emit the analogous inline-callback ref.)
        if (
            language == cs.SupportedLanguage.RUST
            and func_node.type == cs.TS_RS_CLOSURE_EXPRESSION
        ):
            self.ingestor.ensure_relationship_batch(
                (parent_type, cs.KEY_QUALIFIED_NAME, parent_qn),
                cs.RelationshipType.REFERENCES,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

        if resolution.is_exported and language == cs.SupportedLanguage.CPP:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn),
                cs.RelationshipType.EXPORTS,
                (
                    cs.NodeLabel.FUNCTION,
                    cs.KEY_QUALIFIED_NAME,
                    resolution.qualified_name,
                ),
            )

    def _extract_function_name(self, func_node: Node) -> str | None:
        name_node = func_node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text:
            return safe_decode_text(name_node)

        if func_node.type == cs.TS_ARROW_FUNCTION:
            current = func_node.parent
            while current:
                if current.type == cs.TS_VARIABLE_DECLARATOR:
                    # (H) `const m = useMutation({fn: () => {}})` binds the CALL
                    # (H) result to `m`, not the inner arrow; naming the arrow `m`
                    # (H) invents a phantom function (dead-code false positive). Only
                    # (H) claim the declarator name when its value is not a call.
                    value = current.child_by_field_name(cs.FIELD_VALUE)
                    if (
                        value is not None
                        and value.type in cs.JS_CALL_RESULT_VALUE_TYPES
                    ):
                        return None
                    for child in current.children:
                        if child.type == cs.TS_IDENTIFIER and child.text:
                            return safe_decode_text(child)
                    return None
                # (H) Crossing another function's body means this arrow is nested
                # (H) inside it (a JSX handler, a `.map()` callback), not bound to the
                # (H) outer const -- stop so it stays anonymous instead of inheriting
                # (H) the component's name as a `Component.Component` phantom.
                if current.type in cs.JS_ARROW_NAME_CLIMB_BOUNDARY:
                    return None
                current = current.parent

        return None

    def _generate_anonymous_function_name(self, func_node: Node, module_qn: str) -> str:
        parent = func_node.parent
        if parent and parent.type == cs.TS_PARENTHESIZED_EXPRESSION:
            grandparent = parent.parent
            if (
                grandparent
                and grandparent.type == cs.TS_CALL_EXPRESSION
                and grandparent.child_by_field_name(cs.FIELD_FUNCTION) == parent
            ):
                func_type = (
                    cs.PREFIX_ARROW
                    if func_node.type == cs.TS_ARROW_FUNCTION
                    else cs.PREFIX_FUNC
                )
                return f"{cs.PREFIX_IIFE}{func_type}_{func_node.start_point[0]}_{func_node.start_point[1]}"

        if (
            parent
            and parent.type == cs.TS_CALL_EXPRESSION
            and parent.child_by_field_name(cs.FIELD_FUNCTION) == func_node
        ):
            return f"{cs.PREFIX_IIFE_DIRECT}{func_node.start_point[0]}_{func_node.start_point[1]}"

        return f"{cs.PREFIX_ANONYMOUS}{func_node.start_point[0]}_{func_node.start_point[1]}"

    def _extract_lua_assignment_function_name(self, func_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            func_node,
            accepted_var_types=(cs.TS_DOT_INDEX_EXPRESSION, cs.TS_IDENTIFIER),
        )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
        skip_classes: bool = False,
    ) -> str | None:
        current = func_node.parent
        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        path_parts = self._collect_ancestor_path_parts(
            func_node, current, lang_config, skip_classes
        )
        if path_parts is None:
            return None

        return self._format_nested_qn(module_qn, path_parts, func_name)

    def _collect_ancestor_path_parts(
        self,
        func_node: Node,
        current: Node | None,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> list[str] | None:
        path_parts: list[str] = []

        while current and current.type not in lang_config.module_node_types:
            result = self._process_ancestor_for_path(
                func_node, current, lang_config, skip_classes
            )
            if result is False:
                return None
            if result is not None:
                path_parts.append(result)
            current = current.parent

        path_parts.reverse()
        return path_parts

    def _process_ancestor_for_path(
        self,
        func_node: Node,
        current: Node,
        lang_config: LanguageSpec,
        skip_classes: bool,
    ) -> str | None | Literal[False]:
        if current.type in lang_config.function_node_types:
            return self._get_name_from_function_ancestor(current)

        if current.type in lang_config.class_node_types:
            return self._handle_class_ancestor(func_node, current, skip_classes)

        if current.type == cs.TS_METHOD_DEFINITION:
            return self._extract_node_name(current)

        return None

    def _get_name_from_function_ancestor(self, node: Node) -> str | None:
        if name := self._extract_node_name(node):
            return name
        return self._extract_function_name(node)

    def _handle_class_ancestor(
        self, func_node: Node, class_node: Node, skip_classes: bool
    ) -> str | None | Literal[False]:
        if skip_classes:
            return None
        if self._handler.is_inside_method_with_object_literals(func_node):
            return self._extract_node_name(class_node)
        return False

    def _extract_node_name(self, node: Node) -> str | None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        if name_node and name_node.text is not None:
            return safe_decode_text(name_node)
        return None

    def _format_nested_qn(
        self, module_qn: str, path_parts: list[str], func_name: str
    ) -> str:
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _build_rust_function_qualified_name(
        self, func_node: Node, module_qn: str, func_name: str
    ) -> str:
        path_parts = rs_utils.build_module_path(func_node)
        if path_parts:
            return f"{module_qn}.{cs.SEPARATOR_DOT.join(path_parts)}.{func_name}"
        return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)

    def _determine_function_parent(
        self,
        func_node: Node,
        func_qn: str,
        module_qn: str,
        lang_config: LanguageSpec,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str]:
        current = func_node.parent
        if not isinstance(current, Node):
            return cs.NodeLabel.MODULE, module_qn

        file_path = self.module_qn_to_file_path.get(module_qn)
        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                parent_label = (
                    cs.NodeLabel.METHOD
                    if self._is_method(current, lang_config)
                    else cs.NodeLabel.FUNCTION
                )
                # (H) Bind to the enclosing function's OWN qn, recomputed from its
                # (H) node. A function nested in an anonymous callback otherwise
                # (H) loses that callback: anonymous scopes contribute no segment to
                # (H) the child qn, so trimming the child qn would skip the callback
                # (H) and hoist the child to the nearest named ancestor.
                resolution = (
                    self._resolve_function_identity(
                        current, module_qn, language, lang_config, file_path
                    )
                    if language is not None
                    else None
                )
                parent_qn = (
                    resolution.qualified_name
                    if resolution
                    else func_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
                )
                if not parent_qn or parent_qn == func_qn:
                    break
                return parent_label, parent_qn

            current = current.parent

        # (H) A Rust item inside `mod inner` is contained by that inline module,
        # (H) not the file module. Its enclosing module qn is the file module plus
        # (H) the mod path; the inline Module node carries that exact qn.
        if language == cs.SupportedLanguage.RUST and (
            mod_parts := rs_utils.build_module_path(func_node)
        ):
            nested = module_qn + cs.SEPARATOR_DOT + cs.SEPARATOR_DOT.join(mod_parts)
            return cs.NodeLabel.MODULE, nested

        return cs.NodeLabel.MODULE, module_qn
