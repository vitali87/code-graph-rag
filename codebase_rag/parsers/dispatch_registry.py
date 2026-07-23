# String-keyed dispatch registries (issue #913). A handler registered under a
# string key serves work scheduled elsewhere by that same string, invisibly to
# call resolution. Registrations (module-level dict registries mapping string
# literals to module functions, and `@flow`/`@task` registrar decorators)
# EXPOSE `resource::DISPATCH::<key>`; producers passing the key through a
# recognised keyword emit WRITES_TO on the same node, so the two sides meet
# without a resolution pass. A produced `name/deployment` key additionally
# RESOLVES_TO the bare registered `name` when no exact registration exists.
# Dynamic keys stay out: a ceiling yields nothing, never a wrong link.
from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from ..capture import CaptureSelection
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, NodeType
from .import_processor import ImportProcessor
from .io_access.constants import (
    DISPATCH_DEPLOYMENT_SEPARATOR,
    DISPATCH_NAME_KEYWORD,
    DISPATCH_PRODUCER_KEYWORDS,
    DISPATCH_REGISTRARS,
    KEY_KIND,
    RESOURCE_QN_FORMAT,
    ResourceKind,
)
from .utils import safe_decode_text

_HANDLER_NODE_LABELS = {
    NodeType.FUNCTION: cs.NodeLabel.FUNCTION,
    NodeType.METHOD: cs.NodeLabel.METHOD,
}


class _DeferredProducer(NamedTuple):
    """A producer passing the key through a name: resolved after all module
    constants are collected (walk order between a module root and its
    functions is not guaranteed)."""

    caller_spec: tuple[str, str, str]
    module_qn: str
    identifier: str


class DispatchRegistryProcessor:
    """Emits EXPOSES/WRITES_TO edges joining string-keyed dispatch handlers
    and their producers on shared DISPATCH resource nodes."""

    __slots__ = (
        "_ingestor",
        "_import_processor",
        "_function_registry",
        "_exposes_enabled",
        "_writes_enabled",
        "_resolves_enabled",
        "_registered_keys",
        "_produced_keys",
        "_module_constants",
        "_deferred_producers",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        selection: CaptureSelection,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
    ) -> None:
        self._ingestor = ingestor
        self._import_processor = import_processor
        self._function_registry = function_registry
        self._exposes_enabled = selection.rel_enabled(cs.RelationshipType.EXPOSES)
        self._writes_enabled = selection.rel_enabled(cs.RelationshipType.WRITES_TO)
        self._resolves_enabled = selection.rel_enabled(cs.RelationshipType.RESOLVES_TO)
        self._registered_keys: set[str] = set()
        self._produced_keys: set[str] = set()
        self._module_constants: dict[str, dict[str, str]] = {}
        self._deferred_producers: list[_DeferredProducer] = []

    def process_file(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if language is not cs.SupportedLanguage.PYTHON or not (
            self._exposes_enabled or self._writes_enabled
        ):
            return
        self._process_module_scope(root_node, module_qn)
        self._process_producers(root_node, module_qn)

    def finalize(self) -> None:
        # Producers that passed a module-level string constant by name, then
        # the bounded deployment-suffix resolution: `x/dev` resolves onto a
        # registered `x` only when `x/dev` itself is unregistered.
        for deferred in self._deferred_producers:
            constants = self._module_constants.get(deferred.module_qn, {})
            if key := constants.get(deferred.identifier):
                self._emit_produced(deferred.caller_spec, key)
        if not self._resolves_enabled:
            return
        for key in sorted(self._produced_keys):
            if DISPATCH_DEPLOYMENT_SEPARATOR not in key or key in self._registered_keys:
                continue
            head = key.split(DISPATCH_DEPLOYMENT_SEPARATOR, 1)[0]
            if head in self._registered_keys:
                # A partial capture can drop the side that would otherwise
                # create an endpoint node; ensure BOTH here so the edge never
                # dangles (issue #652 defect class).
                self._ensure_resource(key)
                self._ensure_resource(head)
                self._ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, _resource_qn(key)),
                    cs.RelationshipType.RESOLVES_TO,
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, _resource_qn(head)),
                )

    def _process_module_scope(self, root: Node, module_qn: str) -> None:
        constants = self._module_constants.setdefault(module_qn, {})
        for stmt in root.named_children:
            if stmt.type == cs.TS_PY_EXPRESSION_STATEMENT and stmt.named_children:
                self._process_module_assignment(
                    stmt.named_children[0], module_qn, constants
                )
            elif stmt.type == cs.TS_PY_DECORATED_DEFINITION:
                self._process_decorated(stmt, module_qn)

    def _process_module_assignment(
        self, node: Node, module_qn: str, constants: dict[str, str]
    ) -> None:
        if node.type != cs.TS_PY_ASSIGNMENT:
            return
        target = node.child_by_field_name(cs.TS_FIELD_LEFT)
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if target is None or target.type != cs.TS_PY_IDENTIFIER or value is None:
            return
        if value.type == cs.TS_PY_STRING:
            name = safe_decode_text(target)
            if name is not None and (text := _plain_string(value)) is not None:
                constants[name] = text
        elif value.type == cs.TS_PY_DICTIONARY:
            self._process_dict_registry(value, module_qn)

    def _process_dict_registry(self, dictionary: Node, module_qn: str) -> None:
        # A registry ONLY when every entry maps a plain string literal to a
        # module-declared function; one exception keeps config dicts out.
        entries: list[tuple[str, str, NodeType]] = []
        for pair in dictionary.named_children:
            if pair.type != cs.TS_PY_PAIR:
                return
            key_node = pair.child_by_field_name(cs.FIELD_KEY)
            value_node = pair.child_by_field_name(cs.FIELD_VALUE)
            key = _plain_string(key_node) if key_node is not None else None
            if key is None or value_node is None:
                return
            if value_node.type != cs.TS_PY_IDENTIFIER:
                return
            handler = safe_decode_text(value_node)
            if handler is None:
                return
            resolved = self._resolve_handler(module_qn, handler)
            if resolved is None:
                return
            entries.append((key, *resolved))
        for key, handler_qn, node_type in entries:
            self._emit_registration(handler_qn, node_type, key)

    def _resolve_handler(
        self, module_qn: str, handler: str
    ) -> tuple[str, NodeType] | None:
        # A handler declared in the registry module itself, or imported into
        # it (the production registries import from sibling modules; Python
        # import mappings hold full project-prefixed qns).
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        for handler_qn in (
            f"{module_qn}{cs.SEPARATOR_DOT}{handler}",
            import_map.get(handler),
        ):
            if handler_qn is None:
                continue
            node_type = self._function_registry.get(handler_qn)
            if node_type in _HANDLER_NODE_LABELS:
                return handler_qn, node_type
        return None

    def _process_decorated(self, node: Node, module_qn: str) -> None:
        definition = node.child_by_field_name(cs.FIELD_DEFINITION)
        if definition is None or definition.type != cs.TS_PY_FUNCTION_DEFINITION:
            return
        func_name = safe_decode_text(definition.child_by_field_name(cs.FIELD_NAME))
        if func_name is None:
            return
        for decorator in node.named_children:
            if decorator.type != cs.TS_PY_DECORATOR:
                continue
            key = self._registrar_key(decorator, module_qn, func_name)
            if key is None:
                continue
            handler_qn = f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"
            node_type = self._function_registry.get(handler_qn)
            if node_type in _HANDLER_NODE_LABELS:
                self._emit_registration(handler_qn, node_type, key)

    def _registrar_key(
        self, decorator: Node, module_qn: str, func_name: str
    ) -> str | None:
        # `@flow` registers under the hyphenated function name (the Prefect
        # default); `@flow(name="x")` under the explicit name. A same-module
        # function named like a registrar is NOT the external registrar.
        inner = decorator.named_children[0] if decorator.named_children else None
        if inner is None:
            return None
        if inner.type == cs.TS_PY_CALL:
            callee = inner.child_by_field_name(cs.TS_FIELD_FUNCTION)
            registrar = _tail_name(callee)
            if registrar not in DISPATCH_REGISTRARS or self._locally_defined(
                module_qn, registrar
            ):
                return None
            arguments = inner.child_by_field_name(cs.FIELD_ARGUMENTS)
            for arg in arguments.named_children if arguments is not None else []:
                if arg.type != cs.TS_PY_KEYWORD_ARGUMENT:
                    continue
                if (
                    safe_decode_text(arg.child_by_field_name(cs.FIELD_NAME))
                    == DISPATCH_NAME_KEYWORD
                ):
                    value = arg.child_by_field_name(cs.FIELD_VALUE)
                    return _plain_string(value) if value is not None else None
            return func_name.replace("_", "-")
        registrar = _tail_name(inner)
        if registrar in DISPATCH_REGISTRARS and not self._locally_defined(
            module_qn, registrar
        ):
            return func_name.replace("_", "-")
        return None

    def _locally_defined(self, module_qn: str, name: str) -> bool:
        return (
            self._function_registry.get(f"{module_qn}{cs.SEPARATOR_DOT}{name}")
            is not None
        )

    def _process_producers(self, root_node: Node, module_qn: str) -> None:
        stack = list(root_node.named_children)
        while stack:
            node = stack.pop()
            stack.extend(node.named_children)
            if node.type != cs.TS_PY_KEYWORD_ARGUMENT:
                continue
            keyword = safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))
            if keyword not in DISPATCH_PRODUCER_KEYWORDS:
                continue
            value = node.child_by_field_name(cs.FIELD_VALUE)
            if value is None:
                continue
            caller_spec = self._enclosing_caller_spec(node, module_qn)
            if value.type == cs.TS_PY_STRING:
                if (key := _plain_string(value)) is not None:
                    self._emit_produced(caller_spec, key)
            elif value.type == cs.TS_PY_IDENTIFIER and (
                identifier := safe_decode_text(value)
            ):
                self._deferred_producers.append(
                    _DeferredProducer(caller_spec, module_qn, identifier)
                )

    def _enclosing_caller_spec(
        self, node: Node, module_qn: str
    ) -> tuple[str, str, str]:
        # The scope that RUNS the scheduling call: the enclosing function or
        # method when the registry knows it, else the module (module-level
        # scheduling executes at import time).
        names: list[str] = []
        current = node.parent
        while current is not None:
            if current.type in (
                cs.TS_PY_FUNCTION_DEFINITION,
                cs.TS_PY_CLASS_DEFINITION,
            ) and (
                name := safe_decode_text(current.child_by_field_name(cs.FIELD_NAME))
            ):
                names.append(name)
            current = current.parent
        if names:
            qn = cs.SEPARATOR_DOT.join([module_qn, *reversed(names)])
            node_type = self._function_registry.get(qn)
            if node_type in _HANDLER_NODE_LABELS:
                return (_HANDLER_NODE_LABELS[node_type], cs.KEY_QUALIFIED_NAME, qn)
        return (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)

    def _emit_registration(
        self, handler_qn: str, node_type: NodeType, key: str
    ) -> None:
        self._registered_keys.add(key)
        if not self._exposes_enabled:
            return
        self._ensure_resource(key)
        self._ingestor.ensure_relationship_batch(
            (_HANDLER_NODE_LABELS[node_type], cs.KEY_QUALIFIED_NAME, handler_qn),
            cs.RelationshipType.EXPOSES,
            (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, _resource_qn(key)),
        )

    def _emit_produced(self, caller_spec: tuple[str, str, str], key: str) -> None:
        self._produced_keys.add(key)
        if not self._writes_enabled:
            return
        self._ensure_resource(key)
        self._ingestor.ensure_relationship_batch(
            caller_spec,
            cs.RelationshipType.WRITES_TO,
            (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, _resource_qn(key)),
        )

    def _ensure_resource(self, key: str) -> None:
        self._ingestor.ensure_node_batch(
            cs.NodeLabel.RESOURCE,
            {
                cs.KEY_QUALIFIED_NAME: _resource_qn(key),
                cs.KEY_NAME: key,
                KEY_KIND: ResourceKind.DISPATCH.value,
            },
        )


def _resource_qn(key: str) -> str:
    return RESOURCE_QN_FORMAT.format(kind=ResourceKind.DISPATCH.value, identity=key)


def _tail_name(node: Node | None) -> str | None:
    # `flow` or `prefect.flow`: the trailing identifier names the registrar.
    if node is None:
        return None
    if node.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(node)
    if node.type == cs.TS_PY_ATTRIBUTE:
        return _tail_name(node.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE))
    return None


def _plain_string(node: Node) -> str | None:
    # A plain string literal only: any interpolation (f-string) is dynamic
    # and yields nothing. Escape sequences keep their source text.
    if node.type != cs.TS_PY_STRING:
        return None
    parts: list[str] = []
    for child in node.named_children:
        if child.type == cs.TS_PY_INTERPOLATION:
            return None
        if child.type in (cs.TS_PY_STRING_CONTENT, cs.TS_PY_ESCAPE_SEQUENCE):
            parts.append(safe_decode_text(child) or "")
    return "".join(parts)
