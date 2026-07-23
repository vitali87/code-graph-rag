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
from .go import utils as go_utils
from .go.type_inference import GoTypeInferenceEngine
from .import_processor import ImportProcessor
from .io_access.constants import KEY_KIND, RESOURCE_QN_FORMAT, ResourceKind
from .io_access.processor import _RPC_PACKAGE_SUFFIX, _rpc_qualifier_resolves
from .utils import safe_decode_text

# The connect-go handler constructor: `New<Stem>Handler`, qualified by a
# generated package whose name ends in `connect` (the client-side mirror is
# `New<Stem>Client` in io_access).
_RPC_HANDLER_RE = re.compile(r"^New([A-Z]\w*)Handler$")

# (position, name, kind, data): kind is "type" (bare type name), "call"
# (callee segments), or None for an opaque binding that only shadows.
_Binding = tuple[int, str, str | None, "list[str] | str | None"]


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
        "_dir_members_cache",
        "_embedded_cache",
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
        # Package membership is stable by the call pass (the definition pass
        # has completed), so member lists memoise safely.
        self._dir_members_cache: dict[str, list[str]] = {}
        self._embedded_cache: dict[str, list[str]] = {}

    def process_caller(
        self,
        caller_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if not self._enabled or language is not cs.SupportedLanguage.GO:
            return
        wirings = self._wiring_calls(caller_node, module_qn)
        if not wirings:
            return
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        param_types = self._param_types(caller_node)
        bindings = self._body_bindings(caller_node)
        for qualifier, stem, arg, position in wirings:
            # A local shadowing the imported package name in scope AT the call
            # makes it a method on the local value, not codegen wiring
            # (mirrors the client guard); a binding after the call is out of
            # scope there.
            if qualifier in param_types or any(
                b[1] == qualifier and b[0] < position for b in bindings
            ):
                continue
            impl_qn = self._resolve_impl_qn(
                module_qn, arg, position, param_types, bindings
            )
            if impl_qn is None:
                continue
            connect_dir = import_map.get(qualifier)
            if connect_dir is None:
                continue
            for method in self._contract_methods(connect_dir, stem):
                if source_qn := self._method_source_qn(impl_qn, method, set()):
                    self._emit_exposure(source_qn, stem, method)

    def _wiring_calls(
        self, caller_node: Node, module_qn: str
    ) -> list[tuple[str, str, Node, int]]:
        # (qualifier, stem, first-argument node, call position) per handler
        # wiring call.
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        found: list[tuple[str, str, Node, int]] = []
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
            arguments = node.child_by_field_name(cs.FIELD_ARGUMENTS)
            if arguments is not None and arguments.named_children:
                found.append(
                    (
                        qualifier,
                        match.group(1),
                        arguments.named_children[0],
                        node.start_byte,
                    )
                )
        return found

    def _param_types(self, caller_node: Node) -> dict[str, str]:
        # Receiver and parameter names with their bare type names; in scope
        # for the whole body.
        types: dict[str, str] = {}
        for field in (cs.FIELD_RECEIVER, cs.FIELD_PARAMETERS):
            params = caller_node.child_by_field_name(field)
            for param in params.named_children if params is not None else []:
                if param.type != cs.TS_GO_PARAMETER_DECLARATION:
                    continue
                type_node = param.child_by_field_name(cs.FIELD_TYPE)
                type_name = (
                    go_utils.type_identifier_text(type_node) if type_node else None
                )
                if type_name is None:
                    continue
                for child in param.named_children:
                    if child.type == cs.TS_GO_IDENTIFIER and (
                        name := safe_decode_text(child)
                    ):
                        types[name] = type_name
        return types

    def _body_bindings(self, caller_node: Node) -> list[_Binding]:
        # Every `:=` / `var` binding in the body with its position, so each
        # wiring call resolves against the bindings in scope AT the call, not
        # a flat whole-body snapshot. A value neither literal-typed nor a
        # clean constructor call still shadows its name (kind None).
        bindings: list[_Binding] = []
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        stack = [body] if body is not None else []
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type == cs.TS_GO_SHORT_VAR_DECLARATION:
                self._collect_value_bindings(node, bindings)
            elif node.type == cs.TS_GO_VAR_DECLARATION:
                self._collect_var_spec_bindings(node, bindings)
        bindings.sort(key=lambda binding: binding[0])
        return bindings

    def _collect_value_bindings(self, node: Node, bindings: list[_Binding]) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        right = node.child_by_field_name(cs.FIELD_RIGHT)
        if left is None or right is None:
            return
        names = [
            safe_decode_text(c)
            for c in left.named_children
            if c.type == cs.TS_GO_IDENTIFIER
        ]
        values = list(right.named_children)
        for name, value in zip(names, values, strict=False):
            if name:
                bindings.append((node.start_byte, name, *self._value_kind(value)))
        for name in names[len(values) :]:
            # Extra names of a multi-return call (`srv, err := New()`) bind
            # opaquely from the same call.
            if name:
                bindings.append((node.start_byte, name, None, None))

    def _collect_var_spec_bindings(self, node: Node, bindings: list[_Binding]) -> None:
        for spec in node.named_children:
            if spec.type != cs.TS_GO_VAR_SPEC:
                continue
            type_node = spec.child_by_field_name(cs.FIELD_TYPE)
            type_name = go_utils.type_identifier_text(type_node) if type_node else None
            for child in spec.named_children:
                if child.type == cs.TS_GO_IDENTIFIER and (
                    name := safe_decode_text(child)
                ):
                    kind = ("type", type_name) if type_name else (None, None)
                    bindings.append((node.start_byte, name, *kind))

    def _value_kind(self, value: Node) -> tuple[str | None, list[str] | str | None]:
        if value.type == cs.TS_GO_CALL_EXPRESSION:
            segments = self._go_engine.callee_segments(value)
            return ("call", segments) if segments else (None, None)
        if type_name := self._go_engine.infer_value_type(value):
            return ("type", type_name)
        return (None, None)

    def _resolve_impl_qn(
        self,
        module_qn: str,
        arg: Node,
        position: int,
        param_types: Mapping[str, str],
        bindings: list[_Binding],
    ) -> str | None:
        # A literal-typed local (`impl := &Impl{}`, typed parameter) names the
        # type directly; a constructor binding (`uSrv := server.New(...)`)
        # resolves through the imported package's recorded return type. The
        # same two shapes also appear inline as the argument itself.
        if arg.type == cs.TS_GO_IDENTIFIER:
            arg_name = safe_decode_text(arg)
            if arg_name is None:
                return None
            last = None
            for binding in bindings:
                if binding[0] < position and binding[1] == arg_name:
                    last = binding
            if last is not None:
                _pos, _name, kind, data = last
                if kind == "type" and isinstance(data, str):
                    return self._resolve_type_in_package(
                        data, self._package_members(module_qn)
                    )
                if kind == "call" and isinstance(data, list):
                    return self._ctor_return_impl(module_qn, data)
                return None
            if type_name := param_types.get(arg_name):
                return self._resolve_type_in_package(
                    type_name, self._package_members(module_qn)
                )
            return None
        if arg.type == cs.TS_GO_CALL_EXPRESSION:
            segments = self._go_engine.callee_segments(arg)
            return self._ctor_return_impl(module_qn, segments) if segments else None
        if type_name := self._go_engine.infer_value_type(arg):
            return self._resolve_type_in_package(
                type_name, self._package_members(module_qn)
            )
        return None

    def _ctor_return_impl(self, module_qn: str, segments: list[str]) -> str | None:
        # `New()` looks up the wiring module's own package; `server.New()`
        # the imported one.
        if len(segments) == 1:
            members = self._package_members(module_qn)
        elif len(segments) == 2:
            members = self._imported_package_members(module_qn, segments[0])
        else:
            return None
        ctor = segments[-1]
        for member in members:
            if type_name := self._go_function_return_types.get(
                f"{member}{cs.SEPARATOR_DOT}{ctor}"
            ):
                return self._resolve_type_in_package(type_name, members)
        return None

    def _imported_package_members(self, module_qn: str, qualifier: str) -> list[str]:
        # An unaliased import's source qualifier is the target's `package`
        # clause, which may differ from the path segment the import map is
        # keyed by; fall back to matching the clause across imported packages.
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        if package_dir := import_map.get(qualifier):
            return self._package_dir_members(package_dir)
        matches = [
            members
            for package_dir in import_map.values()
            if (members := self._package_dir_members(package_dir))
            and all(self._go_package_names.get(m) == qualifier for m in members)
        ]
        # Two imports with the same clause cannot be told apart from the
        # qualifier alone: stay silent rather than guess.
        return matches[0] if len(matches) == 1 else []

    def _package_dir_members(self, package_dir: str) -> list[str]:
        # Modules of ONE Go package: qn under the dotted dir, all sharing the
        # single parent directory (extension-disambiguated qns keep extra
        # segments, so the path check does the grouping, per issue #930).
        if (cached := self._dir_members_cache.get(package_dir)) is not None:
            return cached
        prefix = f"{package_dir}{cs.SEPARATOR_DOT}"
        candidates = [
            (qn, path)
            for qn, path in self._module_paths.items()
            if qn.startswith(prefix)
        ]
        # Subpackage modules share the qn prefix; the package's own files are
        # the shallowest paths under it.
        shallowest = min((len(p.parts) for _qn, p in candidates), default=None)
        dirs = {p.parent for _qn, p in candidates if len(p.parts) == shallowest}
        members = (
            [qn for qn, path in candidates if path.parent in dirs]
            if len(dirs) == 1
            else []
        )
        self._dir_members_cache[package_dir] = members
        return members

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

    def _method_source_qn(
        self, impl_qn: str, method: str, visited: set[str]
    ) -> str | None:
        # The node serving a contract method: defined on the impl type
        # directly, or promoted from an embedded type (Go embedding is not
        # inheritance, so the graph has no parent edge to follow). Promoted
        # stubs from a generated `*connect` package (`Unimplemented<Stem>
        # Handler`) are not served RPCs.
        if impl_qn in visited or len(visited) > 8:
            return None
        visited.add(impl_qn)
        method_qn = f"{impl_qn}{cs.SEPARATOR_DOT}{method}"
        if self._function_registry.get(method_qn) is NodeType.METHOD:
            return method_qn
        for embedded_qn in self._embedded_type_qns(impl_qn):
            if source := self._method_source_qn(embedded_qn, method, visited):
                return source
        return None

    def _embedded_type_qns(self, impl_qn: str) -> list[str]:
        if (cached := self._embedded_cache.get(impl_qn)) is not None:
            return cached
        module_qn, _, type_name = impl_qn.rpartition(cs.SEPARATOR_DOT)
        path = self._module_paths.get(module_qn)
        entry = (
            self._ast_cache.load(path)
            if self._ast_cache is not None and path is not None
            else None
        )
        resolved: list[str] = []
        if entry is not None and entry[1] is cs.SupportedLanguage.GO:
            for qualifier, name in self._embedded_fields(entry[0], type_name):
                members = (
                    self._package_members(module_qn)
                    if qualifier is None
                    else self._imported_package_members(module_qn, qualifier)
                )
                embedded = self._resolve_type_in_package(name, members)
                if embedded is None:
                    continue
                declaring_module = embedded.rsplit(cs.SEPARATOR_DOT, 1)[0]
                clause = self._go_package_names.get(declaring_module)
                if clause is not None and clause.endswith(_RPC_PACKAGE_SUFFIX):
                    continue
                resolved.append(embedded)
        self._embedded_cache[impl_qn] = resolved
        return resolved

    @staticmethod
    def _embedded_fields(root: Node, type_name: str) -> list[tuple[str | None, str]]:
        # (package qualifier or None, type name) per embedded field of the
        # named struct: a field_declaration with no field_identifier.
        for node in root.named_children:
            if node.type != cs.TS_GO_TYPE_DECLARATION:
                continue
            for spec in node.named_children:
                if spec.type != cs.TS_GO_TYPE_SPEC:
                    continue
                struct = spec.child_by_field_name(cs.FIELD_TYPE)
                if (
                    struct is None
                    or struct.type != cs.TS_GO_STRUCT_TYPE
                    or safe_decode_text(spec.child_by_field_name(cs.FIELD_NAME))
                    != type_name
                ):
                    continue
                return GoRpcExposureProcessor._struct_embedded_entries(struct)
        return []

    @staticmethod
    def _struct_embedded_entries(struct: Node) -> list[tuple[str | None, str]]:
        entries: list[tuple[str | None, str]] = []
        field_list = next(
            (
                c
                for c in struct.named_children
                if c.type == cs.TS_GO_FIELD_DECLARATION_LIST
            ),
            None,
        )
        for decl in field_list.named_children if field_list is not None else []:
            if decl.type != cs.TS_GO_FIELD_DECLARATION or any(
                c.type == cs.TS_GO_FIELD_IDENTIFIER for c in decl.named_children
            ):
                continue
            for child in decl.named_children:
                if child.type == cs.TS_GO_QUALIFIED_TYPE:
                    qualifier = next(
                        (
                            safe_decode_text(c)
                            for c in child.named_children
                            if c.type == cs.TS_GO_PACKAGE_IDENTIFIER
                        ),
                        None,
                    )
                    if name := go_utils.type_identifier_text(child):
                        entries.append((qualifier, name))
                    break
                if name := go_utils.type_identifier_text(child):
                    entries.append((None, name))
                    break
        return entries

    def _emit_exposure(self, method_qn: str, stem: str, method: str) -> None:
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
