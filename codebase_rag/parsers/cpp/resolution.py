"""C++'s share of the name-based callee resolution: the destructors a
construction runs, the class a braced `return {...}` builds, the fan-out of a
call through a template parameter, and the overload an operator binds by its
operand's type.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the class and
method lookups) and hands C++'s part to the `CppResolver` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import CppOperatorSignature, FunctionRegistryTrieProtocol
from ...utils import qn_markers
from ..definition_facts import DefinitionFacts
from ..parameter_nodes import c_family_parameter_list
from . import utils as cpp_utils

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


class CppResolverHost(Protocol):
    """What C++ resolution reads and calls on the `CallResolver` that builds it.

    The lookups every language shares: a written type to its class, a
    recorded return type to a registered class, a class's bases and its own
    or inherited method, and the parsed module trees a free operator's
    declarations are read from."""

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str: ...

    def _resolve_type_to_class_qn(
        self, type_path: str, module_qn: str
    ) -> str | None: ...

    def _mro(self, class_qn: str) -> list[str]: ...

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None: ...

    def _cached_module_root(self, module_qn: str) -> Node | None: ...


class CppResolver:
    """Binds the C++ calls no call node names, and operators by operand type.

    Built and owned by `CallResolver`. The registry, the import processor,
    the type inference engine and the definition facts are the resolver's
    own, held by reference and read as they stand when a call resolves. The
    free-operator signature memo is this resolver's: the watch pass clears
    it with the resolver's other caches."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
        "_cpp_free_operators",
    )

    def __init__(
        self,
        host: CppResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts
        # {module qn: {free operator qn: its declarations' signatures}}.
        self._cpp_free_operators: dict[
            str, dict[str, tuple[CppOperatorSignature, ...]]
        ] = {}

    def reset_resolution_memos(self) -> None:
        self._cpp_free_operators.clear()

    def cpp_destructor_targets(self, class_qn: str) -> set[tuple[str, str]]:
        # Constructing a C++ object guarantees `~X` runs at end of
        # lifetime, but no call node ever names it, so construction sites
        # redirect a CALLS edge to the class's destructor exactly as they
        # do to its constructors (same direct-member gate as
        # java_constructor_targets: a nested class's dtor has an extra qn
        # segment and is excluded).
        # Destroying an object runs its own dtor AND every ancestor dtor
        # unconditionally (issue #892), so the whole INHERITS closure's
        # declared dtors are targets. The full chain matters even when an
        # intermediate dtor is declared: its definition may live outside
        # the parsed source, in which case no caller pass ever emits its
        # own base-dtor edge (PR #894). A destructor cannot be
        # overloaded, so each qn is one direct registry lookup (PR #799).
        targets: set[tuple[str, str]] = set()
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            simple = qn_markers.strip_dup_marker(
                current.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            )
            dtor_qn = f"{current}{cs.SEPARATOR_DOT}{cs.CPP_DESTRUCTOR_PREFIX}{simple}"
            dtor_type = self.function_registry.get(dtor_qn)
            if dtor_type is not None:
                targets.add((dtor_type, dtor_qn))
            stack.extend(self.facts.class_inheritance.get(current, ()))
        return targets

    def cpp_braced_return_class(self, caller_qn: str, module_qn: str) -> str | None:
        # The class constructed by a `return {...};` inside caller_qn: the
        # caller's recorded return type, resolved to a registered class
        # (None for primitives/auto/unrecorded).
        return_type = self.type_inference.facts.method_return_types.get(caller_qn)
        if not return_type:
            return None
        return self._host._resolve_type_to_class_qn(return_type, module_qn)

    def cpp_dispatch_targets(
        self,
        call_name: str,
        local_var_types: dict[str, str] | None,
        template_params: frozenset[str],
    ) -> set[tuple[str, str]]:
        # A C++ call through a template-parameter receiver (`sax->start_object()`
        # inside a `template<typename SAX>` fn) has no concrete receiver type, so
        # precise resolution fails and the trie binds one arbitrary same-named method
        # (or the external-type guard drops the edge), leaving every OTHER structural
        # interface implementer reported dead (nlohmann/json's json_sax_* visitors).
        # When the receiver does NOT resolve to a first-party class, fan the call out
        # to the method on EVERY class that defines it. A receiver typed to a concrete
        # first-party class dispatches precisely and is skipped, so this only adds
        # edges the precise path could not place. Full-fallback by design: a
        # template-parameter or unresolved receiver is indistinguishable from an
        # external one by name, so std::-typed receivers also fan out.
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return set()
        object_name, method_name = parts
        # Fan out only when the receiver is typed to a template PARAMETER (`SAX* sax`
        # in a `template<typename SAX>` fn), whose concrete type is the argument at
        # each instantiation, unknowable statically, so every implementer is a
        # possible target. A concrete type is left to the precise path (a first-party
        # class dispatches exactly; an external `std::string` receiver must NOT be
        # rebound to an unrelated first-party method). An untyped receiver is left to
        # the single-best trie fallback; fanning every untyped call out to all
        # same-named methods would flood the graph with false edges.
        if (local_var_types or {}).get(object_name) not in template_params:
            return set()
        targets: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(method_name):
            _, dot, name = qn.rpartition(cs.SEPARATOR_DOT)
            if (
                dot
                and name == method_name
                and self.function_registry[qn] == cs.NodeLabel.METHOD
            ):
                targets.add((self.function_registry[qn], qn))
        return targets

    def cpp_member_operator(
        self, call_name: str, operand_type_qn: str
    ) -> tuple[str, str] | None:
        # Operand-type-directed operator binding: the overload is either a
        # member of the operand's own class or a free overload beside it. A
        # typed operand with NEITHER is a builtin operation (enum/int
        # comparison) with no first-party callee; nlohmann's
        # `token == token_type::x` must not rebind to an unrelated class's
        # operator== and fan out to all its overload variants.
        # _try_resolve_method covers both the direct member and one
        # INHERITED from a base (Derived : Base with Base::operator==).
        return self._host._try_resolve_method(operand_type_qn, call_name)

    def cpp_free_operator_for_type(
        self, call_name: str, operand_type_qn: str, position: int, arity: int
    ) -> tuple[str, str] | None:
        # ADL: a free overload may live in ANY enclosing namespace of the
        # operand's type, not only its immediate parent scope. Sharing the
        # name is not enough: `1 + v` with V converting to int is built-in
        # `+` even beside an unrelated `operator+(W, W)`, so an overload set
        # counts only when one of its declarations takes the operand's class
        # at the operand's position (issue #2554).
        parts = operand_type_qn.split(cs.SEPARATOR_DOT)
        for depth in range(len(parts) - 1, 0, -1):
            scope = cs.SEPARATOR_DOT.join(parts[:depth])
            free_qn = f"{scope}{cs.SEPARATOR_DOT}{call_name}"
            if free_qn in self.function_registry and any(
                self._cpp_operator_accepts(signature, operand_type_qn, position, arity)
                for signature in self._cpp_free_operator_signatures(free_qn)
            ):
                return (self.function_registry[free_qn], free_qn)
        return None

    def _cpp_operator_accepts(
        self,
        signature: CppOperatorSignature,
        operand_type_qn: str,
        position: int,
        arity: int,
    ) -> bool:
        # The parameter at the operand's position takes its class by value
        # or reference (a base class too), or is a template parameter that
        # deduces to it; a pointer parameter never takes a class value.
        if len(signature.parameters) != arity:
            return False
        param = signature.parameters[position]
        if param.indirection or not param.type_name:
            return False
        if param.type_name in signature.template_params:
            return True
        import_map = self.import_processor.import_mapping.get(signature.module_qn, {})
        param_qn = self._host._resolve_class_qn_from_type(
            param.type_name, import_map, signature.module_qn
        )
        return bool(param_qn) and param_qn in self._host._mro(operand_type_qn)

    def _cpp_free_operator_signatures(
        self, free_qn: str
    ) -> tuple[CppOperatorSignature, ...]:
        # Parameter types are not stored on C++ Function nodes, so they are
        # read from the declaring module's AST: one walk per module collects
        # every free operator it declares, a prototype included. The AST
        # rather than the definition pass's span records, so an incremental
        # run that did not re-parse the module reads the same signatures.
        module_qn = free_qn
        module_paths = self.type_inference.module_qn_to_file_path
        while module_qn and module_qn not in module_paths:
            module_qn = module_qn.rpartition(cs.SEPARATOR_DOT)[0]
        if not module_qn:
            return ()
        if (by_qn := self._cpp_free_operators.get(module_qn)) is None:
            by_qn = self._cpp_free_operators[module_qn] = (
                self._collect_cpp_free_operators(module_qn)
            )
        return by_qn.get(free_qn, ())

    def _collect_cpp_free_operators(
        self, module_qn: str
    ) -> dict[str, tuple[CppOperatorSignature, ...]]:
        found: dict[str, list[CppOperatorSignature]] = {}
        root = self._host._cached_module_root(module_qn)
        if root is None:
            return {}
        engine = self.type_inference.cpp_type_inference
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == cs.TS_CPP_COMPOUND_STATEMENT:
                continue
            stack.extend(node.children)
            if (
                node.type not in cs.CPP_FREE_FUNCTION_DECLARATION_TYPES
                or node.parent is None
                or node.parent.type == cs.CppNodeType.FIELD_DECLARATION_LIST
                or not (name := cpp_utils.extract_function_name(node))
                or not name.startswith(cs.OPERATOR_PREFIX)
                or (params := c_family_parameter_list(node)) is None
            ):
                continue
            found.setdefault(
                cpp_utils.build_qualified_name(node, module_qn, name), []
            ).append(
                CppOperatorSignature(
                    module_qn,
                    engine.parameter_types(params),
                    engine.collect_template_param_names(node),
                )
            )
        return {qn: tuple(signatures) for qn, signatures in found.items()}

    def cpp_operand_class_qn(
        self,
        operand_name: str | None,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> str | None:
        # A bare-identifier operand with a locally inferred type resolves
        # to a REGISTERED first-party type qn, or nothing: only a known
        # class type can select an operator overload (issue #2554).
        if not operand_name or not local_var_types:
            return None
        var_type = local_var_types.get(operand_name)
        if not var_type:
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        resolved = self._host._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        )
        if resolved and resolved in self.function_registry:
            return resolved
        return None

    def resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name.startswith(cs.OPERATOR_PREFIX):
            return None

        # A user-defined overload always beats the builtin: the old table
        # of synthetic `builtin.cpp.operator_*` qns shadowed real overloads
        # and produced edges to nodes that never exist (dropped by the
        # database). A primitive builtin operator is not a first-party
        # callee, so with no registered overload there is no edge at all.
        if possible_matches := self.function_registry.find_ending_with(call_name):
            same_module_ops = [
                qn
                for qn in possible_matches
                if qn.startswith(module_qn) and call_name in qn
            ]
            candidates = same_module_ops or possible_matches
            candidates.sort(key=lambda qn: (len(qn), qn))
            best = candidates[0]
            return (self.function_registry[best], best)

        return None
