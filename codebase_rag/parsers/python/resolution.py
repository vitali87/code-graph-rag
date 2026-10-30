"""Python's share of the name-based callee resolution: the receivers a call
must not be bound through (a local of the name, an untyped shadow of an
import, an external module, a typed class lacking the method, a guess among
several), a module-level class alias, `typing.Protocol` stubs and the
dunders and `__call__` an operator or a call on an instance runs, and the
callee Jedi resolved for a call site.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the class-body
member aliases and the method lookup along the MRO) and hands Python's part
to the `PythonResolver` it builds."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text
from ...types_defs import FunctionRegistryTrieProtocol
from ...utils import qn_markers
from ..call_site import CallSite, split_receiver_chain, trie_search_name
from ..definition_facts import DefinitionFacts
from ..semantic_call_join import call_site_key, declared_location

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


_PY_SELF_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})

PY_EXTERNAL_TARGET: tuple[str, str] = ("", "")


def _module_level_assignments(root_node: Node) -> Iterator[Node]:
    """Every `assignment` that is a top-level expression statement of the module.

    A chained `Alias = Other = Outer` nests an assignment on the outer
    right-hand side; each link is yielded, so every direct target is seen
    (#1759).
    """
    for child in root_node.children:
        if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.children:
            continue
        assignment: Node | None = child.children[0]
        while assignment is not None and assignment.type == cs.TS_PY_ASSIGNMENT:
            yield assignment
            assignment = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)


def _terminal_value(node: Node | None) -> Node | None:
    """The value at the end of an assignment chain: `Outer` in `A = B = Outer`."""
    while node is not None and node.type == cs.TS_PY_ASSIGNMENT:
        node = node.child_by_field_name(cs.TS_FIELD_RIGHT)
    return node


def _rebound_alias(assignment: Node, name: str, bound: str | None) -> str | None:
    """`bound` after `assignment`: the alias it binds `name` to, None when it
    rebinds `name` to a value, `bound` unchanged when it does not touch `name`."""
    left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
    if left is None:
        return bound
    # An unpacking target (`Alias, other = make_pair()`) rebinds the name to
    # a runtime value just as a plain assignment does, and it is never an
    # alias (#1759).
    if left.type in cs.PY_UNPACKING_TARGET_TYPES:
        return None if _binds_identifier(left, name) else bound
    if left.type != cs.TS_PY_IDENTIFIER or safe_decode_text(left) != name:
        return bound
    right = _terminal_value(assignment.child_by_field_name(cs.TS_FIELD_RIGHT))
    if right is not None and right.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
        return safe_decode_text(right)
    return None


def _binds_identifier(target: Node, name: str) -> bool:
    """Whether an unpacking target binds the bare name `name` at any depth.

    Only bare identifiers bind a module-level name: `holder.Alias, x = ...`
    assigns an attribute and `table[Alias], x = ...` a subscript, so those
    subtrees are not descended into (#1759).
    """
    stack = [target]
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_PY_ATTRIBUTE, cs.TS_PY_SUBSCRIPT):
            continue
        if node.type == cs.TS_PY_IDENTIFIER and safe_decode_text(node) == name:
            return True
        stack.extend(node.named_children)
    return False


def _python_callee_name_node(call_node: Node) -> Node | None:
    # The callee NAME token: a bare identifier for `f()`, the `attribute`
    # child for `obj.m()` / `pkg.f()`; anything else (subscripts, calls of
    # call results) has no single name token and stays heuristic.
    func = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
    if func is None:
        return None
    if func.type == cs.TS_PY_IDENTIFIER:
        return func
    if func.type == cs.TS_PY_ATTRIBUTE:
        return func.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
    return None


class PythonResolverHost(Protocol):
    """What Python resolution reads and calls on the `CallResolver` that
    builds it.

    The lookups every language shares, which a Python rule narrows or vetoes:
    the module-language memo and parsed module trees, a written type to its
    class and the registry's spelling of a class, a class's own or inherited
    method, what a receiver's head names, and the simple-name fallback's
    candidates."""

    def _module_language(self, qualified_name: str) -> cs.SupportedLanguage | None: ...

    def _cached_module_root(self, module_qn: str) -> Node | None: ...

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str: ...

    def _registered_class_qn(self, class_qn: str, module_qn: str) -> str | None: ...

    def _strip_optional(self, var_type: str) -> str: ...

    def _two_part_receiver_type(
        self, call_name: str, local_var_types: dict[str, str] | None
    ) -> str | None: ...

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None: ...

    def _receiver_base_qn(self, head: str, module_qn: str) -> str | None: ...

    def _import_target_is_a_namespace(self, target: str) -> bool: ...

    def _nameable_candidates(
        self, candidates: list[str], module_qn: str, call_point: int | None = None
    ) -> list[str]: ...

    def _trie_candidates(
        self, call_name: str, module_qn: str, call_point: int | None = None
    ) -> list[str]: ...


class PythonResolver:
    """Binds and vetoes Python calls by Python's scoping and protocol rules.

    Built and owned by `CallResolver`. The registry, the import processor,
    the type inference engine, the definition facts and the per-caller name
    records the call pass fills (`CallResolver.python_shadowed_imports` and
    its siblings) are the resolver's own, held by reference and read as they
    stand when a call resolves. The Protocol and Jedi memos are this
    resolver's: the watch pass clears them with the resolver's other
    caches."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
        "python_name_typed_params",
        "python_shadowed_imports",
        "python_local_names",
        "_protocol_impl_cache",
        "_protocol_classes_cache",
        "_struct_impl_cache",
        "_py_rel_to_module",
    )

    def __init__(
        self,
        host: PythonResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
        python_name_typed_params: dict[str, frozenset[str]],
        python_shadowed_imports: dict[str, frozenset[str]],
        python_local_names: dict[str, frozenset[str]],
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts
        self.python_name_typed_params = python_name_typed_params
        self.python_shadowed_imports = python_shadowed_imports
        self.python_local_names = python_local_names
        self._protocol_impl_cache: dict[str, str] | None = None
        self._protocol_classes_cache: set[str] | None = None
        self._struct_impl_cache: dict[str, set[str]] = {}
        self._py_rel_to_module: dict[str, str] = {}

    def reset_resolution_memos(self) -> None:
        # A second Protocol implementer created mid-watch must change what
        # dispatch through the Protocol reaches, as a fresh run would.
        self._protocol_impl_cache = None
        self._protocol_classes_cache = None
        self._struct_impl_cache.clear()
        # The rel-path -> module qn inverse rebuilds only when its size
        # differs from `module_qn_to_file_path`; a rename removes one entry
        # and adds one, so the size is unchanged and the memo would keep the
        # old path and never learn the new one, and every JEDI fact
        # targeting the renamed file would miss its declared location.
        self._py_rel_to_module.clear()

    def receiver_typed_by_name(self, call_name: str, caller_qn: str | None) -> bool:
        if not caller_qn or cs.SEPARATOR_DOT not in call_name:
            return False
        guessed = self.python_name_typed_params.get(caller_qn)
        return guessed is not None and (
            call_name.split(cs.SEPARATOR_DOT, 1)[0] in guessed
        )

    def is_python_local_name(self, call: CallSite) -> bool:
        if not call.caller_qn or cs.SEPARATOR_DOT in call.call_name:
            return False
        local = self.python_local_names.get(call.caller_qn)
        return local is not None and call.call_name in local

    def resolve_python_receiver_shadow(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        if call.language != cs.SupportedLanguage.PYTHON:
            return False, None
        handled, inline = self._resolve_inline_receiver_call(
            call.call_name, call.module_qn, call.local_var_types
        )
        if handled:
            return True, inline
        # `helpers.make_pair()` where the caller binds `helpers` itself:
        # the receiver is a local the type map could not type, so the
        # call is on an unknown value. It must not reach the import probe
        # (which would answer with the module the name shadows) nor the
        # caller-independent cache (a sibling caller's answer, or ours
        # poisoning theirs), and the bare-name trie must not guess
        # either (issue #1907). A typed shadow (`helpers: W = ...`)
        # resolves through its type as any local does.
        if self._receiver_is_untyped_shadow(
            call.call_name, call.caller_qn, call.local_var_types
        ):
            return True, None
        return False, None

    def _receiver_is_untyped_shadow(
        self,
        call_name: str,
        caller_qn: str | None,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        if not caller_qn or cs.SEPARATOR_DOT not in call_name:
            return False
        shadowed = self.python_shadowed_imports.get(caller_qn)
        if not shadowed:
            return False
        head = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        return head in shadowed and not (local_var_types and head in local_var_types)

    def _resolve_inline_receiver_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[bool, tuple[str, str] | None]:
        """`(a / b).m()`, `(x or y).m()`: resolve `m` on the receiver's type.

        A parenthesised receiver never becomes a variable, so nothing typed
        it, and `_is_method_chain` does not see it either (it looks for a
        segment holding both parens); the call fell all the way to the trie,
        which matched `m` by name against every class defining it (issue
        #1893, the call-site half of #1868). The receiver text is handed to
        the same rules an assignment gets. Returns (handled, result): handled
        with a result when the method resolves on the inferred class; handled
        with None when the receiver IS typed but its class is not one the
        project defines (`pathlib.Path`), which is a known non-edge rather
        than a licence to guess; not handled when nothing can be inferred.
        """
        parts = split_receiver_chain(call_name)
        if (
            parts is None
            or len(parts) < 2
            or not parts[0].startswith(cs.CHAR_PAREN_OPEN)
        ):
            return False, None
        receiver = cs.SEPARATOR_DOT.join(parts[:-1])
        method = parts[-1].split(cs.CHAR_PAREN_OPEN, 1)[0]
        python = self.type_inference.python_type_inference
        receiver_type = python.expression_analyzer.get_inline_expression_type(
            receiver, module_qn, local_var_types
        )
        if not receiver_type:
            return False, None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._host._resolve_class_qn_from_type(
            receiver_type, import_map, module_qn
        )
        if class_qn and (hit := self._host._try_resolve_method(class_qn, method)):
            return True, hit
        return True, None

    def module_level_alias_target(self, name: str, module_qn: str) -> str | None:
        """The name a module-level Python `name = Other` binds, or None.

        Reads the module's cached AST rather than a recorded map: the
        definition pass records no module-level assignments, and the
        dotted spelling (`Alias = pkg.Outer`) is kept so the class lookup
        can follow the import it names. The LAST module-level binding
        decides: `Alias = Outer` followed by `Alias = make()` holds a value
        by the time anything runs, so it names nothing (#1672).
        """
        root_node = self._host._cached_module_root(module_qn)
        if root_node is None:
            return None
        bound: str | None = None
        for assignment in _module_level_assignments(root_node):
            bound = _rebound_alias(assignment, name, bound)
        return bound

    def _protocol_impl_map(self) -> dict[str, str]:
        # A Protocol stub never runs; the concrete implementer does. Map each
        # XxxProtocol to a unique non-Protocol class named Xxx (the suffix
        # convention disambiguates the real impl from test mocks or other
        # structural conformers, which structural matching alone cannot).
        if self._protocol_impl_cache is not None:
            return self._protocol_impl_cache
        sep = cs.SEPARATOR_DOT
        protocols: set[str] = set()
        classes_by_simple: dict[str, list[str]] = defaultdict(list)
        for qn, bases in self.facts.class_inheritance.items():
            classes_by_simple[qn.rsplit(sep, 1)[-1]].append(qn)
            if any(base.rsplit(sep, 1)[-1] == cs.PY_PROTOCOL for base in bases):
                protocols.add(qn)
        impl: dict[str, str] = {}
        for protocol_qn in protocols:
            simple = protocol_qn.rsplit(sep, 1)[-1]
            if simple == cs.PY_PROTOCOL or not simple.endswith(cs.PY_PROTOCOL):
                continue
            base_name = simple[: -len(cs.PY_PROTOCOL)]
            candidates = [
                qn for qn in classes_by_simple.get(base_name, []) if qn not in protocols
            ]
            if len(candidates) == 1:
                impl[protocol_qn] = candidates[0]
        self._protocol_impl_cache = impl
        return impl

    def _protocol_classes(self) -> set[str]:
        if self._protocol_classes_cache is None:
            sep = cs.SEPARATOR_DOT
            self._protocol_classes_cache = {
                qn
                for qn, bases in self.facts.class_inheritance.items()
                if any(base.rsplit(sep, 1)[-1] == cs.PY_PROTOCOL for base in bases)
            }
        return self._protocol_classes_cache

    def protocol_dispatch_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # A call resolved to a Protocol stub method (P.M) never runs the stub: the
        # runtime receiver is some conformer, so the sound call graph emits an edge
        # to M on every class that implements P. Gating on the resolved target
        # being a Protocol method keeps this from firing on ordinary calls.
        class_qn, sep, method_name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or class_qn not in self._protocol_classes():
            return set()
        protocols = self._protocol_classes()
        # The receiver is a class that implements the Protocol: one defining
        # every method it declares, or its XxxProtocol -> Xxx implementer. A
        # class that merely shares this method's name (an LRU cache's `get`
        # beside a client Protocol's `get`/`set`) is not one (issue #2931).
        conformers = set(self._protocol_structural_implementers(class_qn))
        if named_impl := self._protocol_impl_map().get(class_qn):
            conformers.add(named_impl)
        methods = {
            resolved[1]
            for conformer in conformers
            if (resolved := self._host._try_resolve_method(conformer, method_name))
        }
        # The definers of a same-named method, read from the registry, which
        # an incremental run holds whole: the class hierarchy those scans
        # walk holds only the re-parsed files' classes then (issue #532).
        required = self._protocol_method_names(class_qn)
        definers = {
            definer: qn
            for qn in self.function_registry.find_ending_with(method_name)
            if (parts := qn.rpartition(cs.SEPARATOR_DOT))[1]
            and parts[2] == method_name
            and (definer := parts[0]) not in protocols
        }
        # The XxxProtocol -> Xxx convention, held to the uniqueness
        # `_protocol_impl_map` asks of it.
        simple = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        named = [
            definer
            for definer in definers
            if simple != cs.PY_PROTOCOL
            and simple.endswith(cs.PY_PROTOCOL)
            and definer.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            == simple.removesuffix(cs.PY_PROTOCOL)
        ]
        for definer, qn in definers.items():
            if (len(named) == 1 and definer == named[0]) or all(
                self._host._try_resolve_method(definer, m) for m in required
            ):
                methods.add(qn)
        targets: set[tuple[str, str]] = set()
        # The same reachability bound the simple-name fallback uses applies:
        # nothing written in a language the Protocol's own cannot call into
        # conforms to it (issue #945).
        for qn in self._host._nameable_candidates(sorted(methods), class_qn):
            definer, dot, name = qn.rpartition(cs.SEPARATOR_DOT)
            if dot and name == method_name and definer not in protocols:
                targets.add((self.function_registry[qn], qn))
        return targets

    def redirect_protocol_method(
        self, result: tuple[str, str] | None
    ) -> tuple[str, str] | None:
        # Only Python Protocol stubs REPLACE the resolved target: a Protocol
        # method body never runs (it is `...`), so the concrete method is the
        # sole real callee. An interface/trait method is a live declaration the
        # call depends on, so its sole-impl companion edge is ADDITIVE
        # (interface_sole_impl_targets), never a replacement.
        if result is None:
            return result
        class_qn, sep, method_name = result[1].rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return result
        impl_qn = self._protocol_impl_map().get(class_qn)
        if impl_qn is None:
            return result
        redirected = f"{impl_qn}{cs.SEPARATOR_DOT}{method_name}"
        if redirected in self.function_registry:
            return self.function_registry[redirected], redirected
        return result

    def typed_receiver_lacks_method(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None,
    ) -> bool:
        # True for a Python `obj.method` where `obj` has a known type that IS
        # an indexed first-party class, and neither that class, a base it
        # inherits from, nor any same-named class in this module defines
        # `method`. The precise local-type path already failed for exactly
        # that reason, so the call cannot be this class's method; binding it
        # by bare name to some other `method` is a false edge. An untyped
        # receiver, an external type (`_receiver_type_is_external`) or a type
        # that resolves to no indexed class stays out: unknown, not absent.
        #
        # Python only. Elsewhere a type's method set is not readable from its
        # class qn: Go promotes methods from an embedded struct the inheritance
        # walk does not model, and Rust registers `impl` methods under the
        # impl's module and trait defaults under the trait. There the fallback
        # stays as it was.
        if language != cs.SupportedLanguage.PYTHON:
            return False
        var_type = self._host._two_part_receiver_type(call_name, local_var_types)
        if var_type is None:
            return False
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._host._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        )
        if not class_qn:
            return False
        registered = self._host._registered_class_qn(class_qn, module_qn)
        if registered is None:
            return False
        if self.function_registry[registered] != cs.NodeLabel.CLASS.value:
            return False
        method_name = call_name.split(cs.SEPARATOR_DOT)[1]
        # Own or inherited under the registered spelling. The precise
        # local-type path has normally already answered both, so this
        # re-check matters where that path used an unprefixed class qn.
        if self._host._try_resolve_method(registered, method_name) is not None:
            return False
        # A bare type name that several classes in THIS module carry (two
        # functions each defining a local `Analyzer`) resolved to one of them
        # above, possibly the wrong one; if any same-named class defined in
        # this module has the method, the call is left to the fallback. A
        # same-named class in another module, a CHILD module of a package
        # included, is unrelated and does not rescue the call. The annotation
        # is reduced first: `Product | None` names `Product`.
        simple_type = self._host._strip_optional(var_type).rsplit(cs.SEPARATOR_DOT, 1)[
            -1
        ]
        return not any(
            self._is_class_method_defined_in(qn, module_qn)
            for qn in self.function_registry.find_ending_with(
                f"{simple_type}{cs.SEPARATOR_DOT}{method_name}"
            )
        )

    def _is_class_method_defined_in(self, qn: str, module_qn: str) -> bool:
        """Whether `qn` is a method of a CLASS that `module_qn` itself defines.

        The owner must be a registered class (a nested function named like
        the type, `factory.Product.go`, does not count), and it must live in
        `module_qn` at any scope depth: every scope between the module and the
        class (`second` in `proj.app.second.Analyzer`) must be a definition
        this module registered. An unregistered scope is a module boundary,
        `proj.app.util` under the `proj.app` package, or unknown; either way
        not this module. Modules are not registry entries, and the module map
        holds only re-parsed files during an incremental run, so the registry
        (rehydrated for every file) is what decides, with the map as a second
        check where it does know the scope.
        """
        owner, _sep, _method = qn.rpartition(cs.SEPARATOR_DOT)
        if self.function_registry.get(owner) != cs.NodeLabel.CLASS.value:
            return False
        prefix = f"{module_qn}{cs.SEPARATOR_DOT}"
        if not owner.startswith(prefix):
            return False
        modules = self.type_inference.module_qn_to_file_path
        scope = module_qn
        for segment in owner[len(prefix) :].split(cs.SEPARATOR_DOT)[:-1]:
            scope = f"{scope}{cs.SEPARATOR_DOT}{segment}"
            if scope in modules or scope not in self.function_registry:
                return False
        return True

    def _is_python_call(self, call: CallSite) -> bool:
        # The pre-passes (constructor fields, decorators, property reads) omit
        # the language, and their answers share the module-keyed cache with
        # the call scan's, so a gate on `call.language` alone would let a
        # pre-pass cache the very guess the scan is meant to refuse.
        language = call.language or self._host._module_language(call.module_qn)
        return language == cs.SupportedLanguage.PYTHON

    def receiver_is_external_python_module(self, call: CallSite) -> bool:
        # True for a Python `mod.attr(...)` whose `mod` is bound by an import
        # of a module outside the project (`import subprocess`, `from
        # concurrent import futures`). The attribute lives in that module, so
        # the call has no first-party target, yet the name-only probes bound
        # `subprocess.run` to whichever first-party `run` sat closest:
        # `GraphUpdater.run` on this repository (issue #2360).
        call_name, module_qn = call.call_name, call.module_qn
        head, sep, _ = call_name.partition(cs.SEPARATOR_DOT)
        if not sep or not self._is_python_call(call):
            return False
        # A local of that name shadows the module; the typed paths own it.
        if call.local_var_types and head in call.local_var_types:
            return False
        import_map = self.import_processor.import_mapping.get(module_qn)
        target = import_map.get(head) if import_map else None
        if not target:
            return False
        # Every binding must be external: `try: import tomllib` / `except
        # ImportError: import tomli as tomllib` is, while a branch that binds
        # the name to first-party code leaves the fallbacks to find it.
        rebinds = self.import_processor.python.python_import_rebinds.get(module_qn, {})
        if not all(
            self._import_target_is_external(bound, call_name, module_qn)
            for bound in {target, *rebinds.get(head, ())}
        ):
            return False
        logger.debug(
            ls.CALL_EXTERNAL_MODULE_RECEIVER, call_name=call_name, target=target
        )
        return True

    def _import_target_is_external(
        self, target: str, call_name: str, module_qn: str
    ) -> bool:
        # The import processor writes a first-party target project-prefixed,
        # but records bare the modules it cannot place: a script's `import
        # helper` of the sibling `helper.py`, or `import helpers` in a
        # repository whose root is a package. Such a target is first-party
        # when the registry holds it under the project, or when a
        # first-party definition spells out the called member in full.
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if target.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        if self._host._registered_class_qn(target, module_qn) is not None:
            return False
        if self._host._import_target_is_a_namespace(
            f"{project_root}{cs.SEPARATOR_DOT}{target}"
        ):
            return False
        member = call_name.partition(cs.SEPARATOR_DOT)[2]
        module_name = target.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        spelled = f"{cs.SEPARATOR_DOT}{module_name}{cs.SEPARATOR_DOT}{member}"
        return not any(
            qn.endswith(spelled)
            for qn in self.function_registry.find_ending_with(
                trie_search_name(call_name)
            )
        )

    def untyped_receiver_guess_is_ambiguous(self, call: CallSite) -> bool:
        # True for a Python member call on a receiver holding a value nothing
        # typed (a parameter, a dict lookup's result, an unannotated field)
        # when several first-party definitions share the member's name. The
        # simple-name fallback picks among them by import distance alone,
        # which is how `frontend.run()` and `agent.run()` bound
        # `GraphUpdater.run` (issue #2360). A unique name still binds as a
        # heuristic edge, the rule callable fields of unknown receiver type
        # follow. A namespace receiver (an import, a class, a module-level
        # alias) is not a value, and a typed receiver (a bare `self.m()`, an
        # annotated field) is the typed paths' to answer or refuse, so all of
        # them keep the fallback exactly as before.
        call_name = call.call_name
        if call.constructing or cs.SEPARATOR_DOT not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        head = parts[0]
        if not head.isidentifier() or not self._is_python_call(call):
            return False
        on_self = head in _PY_SELF_RECEIVERS
        if on_self and len(parts) == 2:
            return False
        if self._receiver_is_bound(call, parts, on_self):
            return False
        if (
            len(self._host._trie_candidates(call_name, call.module_qn, call.call_point))
            < 2
        ):
            return False
        # Last, being the one probe that reads the module's AST.
        return on_self or self._host._receiver_base_qn(head, call.module_qn) is None

    def _receiver_is_bound(
        self, call: CallSite, parts: list[str], on_self: bool
    ) -> bool:
        # A typed local or an import names the receiver, so the typed and
        # import paths answer the call and the fallback guard stays out.
        local_types = call.local_var_types or {}
        if cs.SEPARATOR_DOT.join(parts[:-1]) in local_types:
            return True
        if on_self:
            return False
        head = parts[0]
        if head in local_types:
            return True
        import_map = self.import_processor.import_mapping.get(call.module_qn)
        return bool(import_map) and head in import_map

    def operator_dunder_targets(
        self,
        operand_text: str,
        dunder: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> set[tuple[str, str]]:
        # Operator syntax dispatches to a dunder on the operand's type. Resolve only
        # when the operand type is known; never via the name-only trie fallback, so a
        # builtin container does not borrow a first-party dunder. A Protocol-typed
        # operand dispatches to the dunder on each structural implementer (which may
        # define the dunder even when the Protocol stub does not, e.g. __len__).
        if not local_var_types or not (var_type := local_var_types.get(operand_text)):
            return set()
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._host._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        )
        if not class_qn:
            return set()
        if class_qn in self._protocol_classes():
            # Naming convention (XxxProtocol -> Xxx) is robust when it applies;
            # structural conformance covers protocols whose implementer is named
            # differently. Union both so neither gap drops a concrete target.
            classes = set(self._protocol_structural_implementers(class_qn))
            if named_impl := self._protocol_impl_map().get(class_qn):
                classes.add(named_impl)
        else:
            classes = {class_qn}
        targets: set[tuple[str, str]] = set()
        for candidate in classes:
            if resolved := self._host._try_resolve_method(candidate, dunder):
                targets.add(resolved)
        return targets

    def _protocol_structural_implementers(self, protocol_qn: str) -> set[str]:
        # Classes that define every method declared on the Protocol (own or
        # inherited). Used to dispatch operator dunders to the concrete type when the
        # Protocol/implementer names don't follow the XxxProtocol convention.
        if protocol_qn in self._struct_impl_cache:
            return self._struct_impl_cache[protocol_qn]
        protocol_methods = self._protocol_method_names(protocol_qn)
        result: set[str] = set()
        if protocol_methods:
            result = self._classes_defining_all(protocol_methods)
        self._struct_impl_cache[protocol_qn] = result
        return result

    def _protocol_method_names(self, protocol_qn: str) -> set[str]:
        """The names of the methods a Protocol and its Protocol bases declare.

        An inherited method is required as much as an own one, and the
        `@overload` stubs of one method are one name, though the registry
        keeps each under its own `@<line>` marker.
        """
        sep = cs.SEPARATOR_DOT
        protocols = self._protocol_classes()
        names: set[str] = set()
        pending = [protocol_qn]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            names.update(
                qn_markers.strip_dup_marker(qn.rsplit(sep, 1)[-1])
                for qn, node_type in self.function_registry.find_with_prefix(current)
                if node_type == cs.NodeLabel.METHOD and qn.rsplit(sep, 1)[0] == current
            )
            pending.extend(
                base
                for base in self.facts.class_inheritance.get(current, ())
                if base in protocols
            )
        return names

    def _classes_defining_all(self, protocol_methods: set[str]) -> set[str]:
        result: set[str] = set()
        protocols = self._protocol_classes()
        for candidate in self.facts.class_inheritance:
            if candidate in protocols:
                continue
            if all(
                self._host._try_resolve_method(candidate, method)
                for method in protocol_methods
            ):
                result.add(candidate)
        return result

    def instance_call_targets(
        self,
        callee: str,
        is_call_result: bool,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> set[tuple[str, str]]:
        """What `callee(...)` runs when `callee` is an instance: its `__call__`.

        A typed name or attribute is resolved like any operator operand; a
        call result (`Dispatcher()(1)`) is typed by its return. Calling the
        class itself constructs it and never gets here, because the class
        name is no typed local.
        """
        if not is_call_result:
            return self.operator_dunder_targets(
                callee, cs.PY_DUNDER_CALL, module_qn, local_var_types
            )
        python = self.type_inference.python_type_inference
        returned = python.expression_analyzer.infer_expression_return_type(
            callee, module_qn, local_var_types
        )
        if not returned:
            return set()
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._host._resolve_class_qn_from_type(
            returned, import_map, module_qn
        )
        if not class_qn or self.function_registry.get(class_qn) != cs.NodeLabel.CLASS:
            return set()
        hit = self._host._try_resolve_method(class_qn, cs.PY_DUNDER_CALL)
        return {hit} if hit else set()

    def resolve_python_call_site(
        self,
        call_node: Node,
        module_qn: str,
    ) -> tuple[str, str] | None:
        # The Jedi semantic path (issue #1183), mirroring the Go shape: an
        # exact first-party callee through re-exports/decorators, or the
        # external sentinel proving the call leaves the repo. Any miss falls
        # back to the heuristics.
        ti = self.type_inference
        facts = ti.facts.frontend_facts[cs.SupportedLanguage.PYTHON]
        if not (facts.resolved_call_sites or facts.external_sites):
            return None
        name_node = _python_callee_name_node(call_node)
        if name_node is None:
            return None
        name = name_node.text.decode(cs.ENCODING_UTF8) if name_node.text else ""
        if not name:
            return None
        key = call_site_key(
            name_node,
            name,
            module_qn,
            ti.module_qn_to_file_path,
            ti.repo_path,
        )
        if key is None:
            return None
        fact = facts.resolved_call_sites.get(key)
        if fact is not None:
            return declared_location(
                fact.target_file,
                fact.target_line,
                fact.target_col,
                ti.facts.function_locations,
                ti.module_qn_to_file_path,
                ti.repo_path,
                self._py_rel_to_module,
            )
        if key in facts.external_sites:
            return PY_EXTERNAL_TARGET
        return None
