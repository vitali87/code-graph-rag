# Server-side connect-go exposure (issue #912 slice 2). A wiring call
# `path, handler := <pkg>connect.New<Stem>Handler(impl)` proves the impl
# type serves the generated contract: each contract method the impl defines
# gets an EXPOSES edge to the UNSCOPED `resource::RPC::<Stem>.<Method>` node
# that client sinks already target, so caller and server join on one node
# without RESOLVES_TO.
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from ..capture import CaptureSelection
from ..services import IngestorProtocol
from ..types_defs import ASTCacheProtocol, FunctionRegistryTrieProtocol, NodeType
from .go.type_inference import GoTypeInferenceEngine
from .import_processor import ImportProcessor
from .io_access.constants import KEY_KIND, RESOURCE_QN_FORMAT, ResourceKind
from .io_access.processor import _rpc_qualifier_resolves
from .utils import safe_decode_text

# The connect-go handler constructor: `New<Stem>Handler`, qualified by a
# generated package whose name ends in `connect` (the client-side mirror is
# `New<Stem>Client` in io_access).
_RPC_HANDLER_RE = re.compile(r"^New([A-Z]\w*)Handler$")


class GoRpcExposureProcessor:
    """Detects connect-go handler wiring in a function body and emits EXPOSES
    edges from the impl type's contract methods to RPC Resource nodes."""

    __slots__ = (
        "_ingestor",
        "_import_processor",
        "_enabled",
        "_function_registry",
        "_simple_name_lookup",
        "_module_paths",
        "_go_package_names",
        "_go_function_return_types",
        "_ast_cache",
        "_go_engine",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        import_processor: ImportProcessor,
        selection: CaptureSelection,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: Mapping[str, set[str]],
        module_paths: Mapping[str, Path],
        go_package_names: Mapping[str, str],
        go_function_return_types: Mapping[str, str],
        ast_cache: ASTCacheProtocol | None = None,
    ) -> None:
        self._ingestor = ingestor
        self._import_processor = import_processor
        self._enabled = selection.rel_enabled(cs.RelationshipType.EXPOSES)
        self._function_registry = function_registry
        self._simple_name_lookup = simple_name_lookup
        self._module_paths = module_paths
        self._go_package_names = go_package_names
        self._go_function_return_types = go_function_return_types
        self._ast_cache = ast_cache
        self._go_engine = GoTypeInferenceEngine()

    def process_caller(
        self,
        caller_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if not self._enabled or language is not cs.SupportedLanguage.GO:
            return
        wirings = list(self._wiring_calls(caller_node, module_qn))
        if not wirings:
            return
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        for qualifier, stem, arg_name in wirings:
            impl_qn = self._resolve_impl_qn(caller_node, module_qn, arg_name)
            if impl_qn is None:
                continue
            connect_dir = import_map.get(qualifier)
            if connect_dir is None:
                continue
            for method in self._contract_methods(connect_dir, stem):
                self._emit_exposure(impl_qn, stem, method)

    def _wiring_calls(
        self, caller_node: Node, module_qn: str
    ) -> list[tuple[str, str, str]]:
        # (qualifier, stem, first-argument identifier) per handler wiring call.
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        found: list[tuple[str, str, str]] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        stack = [body] if body is not None else []
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type != cs.TS_GO_CALL_EXPRESSION:
                continue
            func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if func is None or func.type != cs.TS_GO_SELECTOR_EXPRESSION:
                continue
            operand = func.child_by_field_name(cs.FIELD_OPERAND)
            field = func.child_by_field_name(cs.TS_GO_FIELD_FIELD)
            if operand is None or operand.type != cs.TS_GO_IDENTIFIER or field is None:
                continue
            name = safe_decode_text(field) or ""
            match = _RPC_HANDLER_RE.match(name)
            qualifier = safe_decode_text(operand) or ""
            if not match or not _rpc_qualifier_resolves(qualifier, import_map):
                continue
            if arg := self._first_identifier_arg(node):
                found.append((qualifier, match.group(1), arg))
        return found

    @staticmethod
    def _first_identifier_arg(call: Node) -> str | None:
        arguments = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        first = arguments.named_children[0] if arguments and arguments.named_children else None
        if first is None or first.type != cs.TS_GO_IDENTIFIER:
            return None
        return safe_decode_text(first)

    def _resolve_impl_qn(
        self, caller_node: Node, module_qn: str, arg_name: str
    ) -> str | None:
        # A literal-typed local (`impl := &Impl{}`, typed parameter) names the
        # type directly; a constructor binding (`uSrv := server.New(...)`)
        # resolves through the imported package's recorded return type.
        var_types = self._go_engine.build_local_variable_type_map(
            caller_node, module_qn
        )
        if type_name := var_types.get(arg_name):
            return self._resolve_type_in_package(
                type_name, self._package_members(module_qn)
            )
        for name, segments in self._go_engine.collect_call_var_bindings(caller_node):
            if name == arg_name and len(segments) == 2:
                return self._ctor_return_impl(module_qn, segments[0], segments[1])
        return None

    def _ctor_return_impl(
        self, module_qn: str, qualifier: str, ctor: str
    ) -> str | None:
        members = self._imported_package_members(module_qn, qualifier)
        for member in members:
            if type_name := self._go_function_return_types.get(
                f"{member}{cs.SEPARATOR_DOT}{ctor}"
            ):
                return self._resolve_type_in_package(type_name, members)
        return None

    def _imported_package_members(
        self, module_qn: str, qualifier: str
    ) -> list[str]:
        # An unaliased import's source qualifier is the target's `package`
        # clause, which may differ from the path segment the import map is
        # keyed by; fall back to matching the clause across imported packages.
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        if package_dir := import_map.get(qualifier):
            return self._package_dir_members(package_dir)
        for package_dir in import_map.values():
            members = self._package_dir_members(package_dir)
            if members and all(
                self._go_package_names.get(m) == qualifier for m in members
            ):
                return members
        return []

    def _package_dir_members(self, package_dir: str) -> list[str]:
        # Modules of ONE Go package: qn under the dotted dir, all sharing the
        # single parent directory (extension-disambiguated qns keep extra
        # segments, so the path check does the grouping, per issue #930).
        prefix = f"{package_dir}{cs.SEPARATOR_DOT}"
        dirs = {
            path.parent
            for qn, path in self._module_paths.items()
            if qn.startswith(prefix)
        }
        if len(dirs) != 1:
            candidates = [
                (qn, path)
                for qn, path in self._module_paths.items()
                if qn.startswith(prefix)
            ]
            shallowest = min(
                (len(p.parts) for _qn, p in candidates), default=None
            )
            dirs = {p.parent for _qn, p in candidates if len(p.parts) == shallowest}
            if len(dirs) != 1:
                return []
        target = next(iter(dirs))
        return [
            qn
            for qn, path in self._module_paths.items()
            if qn.startswith(prefix) and path.parent == target
        ]

    def _package_members(self, module_qn: str) -> list[str]:
        my_path = self._module_paths.get(module_qn)
        if my_path is None:
            return [module_qn]
        my_clause = self._go_package_names.get(module_qn)
        return [
            qn
            for qn, path in self._module_paths.items()
            if path.parent == my_path.parent
            and self._go_package_names.get(qn) == my_clause
        ]

    def _resolve_type_in_package(
        self, type_name: str, members: list[str]
    ) -> str | None:
        member_set = set(members)
        matches = [
            qn
            for qn in self._simple_name_lookup.get(type_name, set())
            if qn.rsplit(cs.SEPARATOR_DOT, 1)[0] in member_set
        ]
        return matches[0] if len(matches) == 1 else None

    def _contract_methods(self, connect_dir: str, stem: str) -> list[str]:
        # Method names of the generated `<Stem>Handler` interface, read
        # syntactically from the vendored codegen (interface method specs are
        # not graph nodes). Embedded interfaces are not followed: connect-go
        # never emits them.
        if self._ast_cache is None:
            return []
        interface_name = f"{stem}Handler"
        for member in self._package_dir_members(connect_dir):
            path = self._module_paths.get(member)
            entry = self._ast_cache.load(path) if path is not None else None
            if entry is None or entry[1] is not cs.SupportedLanguage.GO:
                continue
            if methods := self._interface_methods(entry[0], interface_name):
                return methods
        return []

    @staticmethod
    def _interface_methods(root: Node, interface_name: str) -> list[str]:
        # Go type declarations relevant to codegen are file-scoped: no descent.
        for node in root.named_children:
            if node.type != cs.TS_GO_TYPE_DECLARATION:
                continue
            for spec in node.named_children:
                if spec.type != cs.TS_GO_TYPE_SPEC:
                    continue
                name_node = spec.child_by_field_name(cs.FIELD_NAME)
                type_node = spec.child_by_field_name(cs.FIELD_TYPE)
                if (
                    type_node is None
                    or type_node.type != cs.TS_GO_INTERFACE_TYPE
                    or safe_decode_text(name_node) != interface_name
                ):
                    continue
                return [
                    name
                    for elem in type_node.named_children
                    if elem.type == cs.TS_GO_METHOD_ELEM
                    and (
                        name := safe_decode_text(
                            elem.child_by_field_name(cs.FIELD_NAME)
                        )
                    )
                ]
        return []

    def _emit_exposure(self, impl_qn: str, stem: str, method: str) -> None:
        method_qn = f"{impl_qn}{cs.SEPARATOR_DOT}{method}"
        if self._function_registry.get(method_qn) is not NodeType.METHOD:
            return
        identity = f"{stem}{cs.SEPARATOR_DOT}{method}"
        resource_qn = RESOURCE_QN_FORMAT.format(
            kind=ResourceKind.RPC.value, identity=identity
        )
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.RESOURCE,
            {
                cs.KEY_QUALIFIED_NAME: resource_qn,
                cs.KEY_NAME: identity,
                KEY_KIND: ResourceKind.RPC.value,
            },
        )
        self._ingestor.ensure_relationship_batch(
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
            cs.RelationshipType.EXPOSES,
            (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, resource_qn),
        )
