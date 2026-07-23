from __future__ import annotations

import re
from collections import defaultdict, deque

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import get_language_for_extension
from ..types_defs import FunctionRegistryTrieProtocol, NodeType
from .import_processor import ImportProcessor
from .py import resolve_class_name
from .type_inference import TypeInferenceEngine
from .utils import follow_reexports

_SEPARATOR_PATTERN = re.compile(r"[.:]|::")
_SEARCH_NAME_CACHE: dict[str, str] = {}
_CHAINED_METHOD_PATTERN = re.compile(r"\.([^.()]+)$")
_QN_SPLIT_CACHE: dict[str, tuple[list[str], int]] = {}
_CHAIN_OPEN_BRACKETS = "([{"
_CHAIN_CLOSE_BRACKETS = ")]}"
# Node labels a Rust receiver type name may resolve to: a struct (Class), an
# enum, a type alias, or a trait (Interface, when the receiver is typed to a
# `dyn`/`impl` trait or a trait-returning factory); all can carry methods.
_RS_TYPE_NODE_TYPES = frozenset(
    {NodeType.CLASS, NodeType.ENUM, NodeType.TYPE, NodeType.INTERFACE}
)
# A definition nested inside one of these is scoped to that body, so no other
# module can name it and the simple-name fallback must not offer it (#945).
_SCOPING_PARENT_TYPES = frozenset({NodeType.FUNCTION, NodeType.METHOD})
# Sets of languages whose sources call each other directly, so a candidate
# written in a sibling language is a legitimate target for the simple-name
# fallback: the JS family compiles to one runtime, C++ calls C, and Scala
# calls Java on the JVM. Any language absent here calls only its own.
_CALLABLE_LANGUAGE_FAMILIES: tuple[frozenset[cs.SupportedLanguage], ...] = (
    cs.JS_TS_LANGUAGES,
    frozenset({cs.SupportedLanguage.C, cs.SupportedLanguage.CPP}),
    frozenset({cs.SupportedLanguage.JAVA, cs.SupportedLanguage.SCALA}),
)


def _split_receiver_chain(expr: str) -> list[str]:
    # Split a receiver chain (`c.Find(1.5).Root`) on the `.` separators between
    # hops only, never on a `.` inside call arguments, an index, or a generic
    # (`1.5`, `x.y` args, `List<A.B>`), which a naive str.split would mangle.
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in expr:
        if char in _CHAIN_OPEN_BRACKETS:
            depth += 1
        elif char in _CHAIN_CLOSE_BRACKETS:
            depth = max(0, depth - 1)
        if char == cs.SEPARATOR_DOT and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


class CallResolver:
    __slots__ = (
        "function_registry",
        "import_processor",
        "type_inference",
        "class_inheritance",
        "type_aliases",
        "interface_implementers",
        "_interface_impl_cache",
        "_simple_resolution_cache",
        "_wildcard_cache",
        "_protocol_impl_cache",
        "_field_bindings",
        "_field_to_classes",
        "_subclass_map_cache",
        "_protocol_classes_cache",
        "_struct_impl_cache",
        "_ctor_params",
        "_ctor_param_attrs",
        "_pending_field_bindings",
        "_module_language_cache",
    )

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
        type_aliases: dict[str, str] | None = None,
        interface_implementers: dict[str, set[str]] | None = None,
    ) -> None:
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance
        # {interface_qn: [implementer_qns]} (shared ref, populated during
        # ingestion). Used to redirect an interface-typed call to the single
        # concrete implementer's method (call-graph accuracy; single-impl only).
        self.interface_implementers = (
            interface_implementers if interface_implementers is not None else {}
        )
        self._interface_impl_cache: dict[str, str] | None = None
        # C++ typedef/using alias -> underlying bare type, consulted when a
        # receiver type name is mapped to a class (empty for other languages).
        self.type_aliases = type_aliases if type_aliases is not None else {}
        self._simple_resolution_cache: dict[
            tuple[str, str], tuple[str, str] | None
        ] = {}
        self._wildcard_cache: dict[int, list[tuple[str, str]]] = {}
        self._protocol_impl_cache: dict[str, str] | None = None
        self._field_bindings: dict[tuple[str, str], set[str]] = {}
        self._field_to_classes: dict[str, set[str]] = {}
        self._subclass_map_cache: dict[str, set[str]] | None = None
        self._protocol_classes_cache: set[str] | None = None
        self._struct_impl_cache: dict[str, set[str]] = {}
        # Ordered constructor parameter names per class (explicit __init__
        # params, or annotated class-body fields for NamedTuple/dataclass),
        # plus the param -> stored-attribute renames found in __init__ bodies
        # (self.ctx_factory = create_context). Construction-site bindings are
        # held PENDING until every file's ctor metadata is collected, since a
        # site may be scanned before its class's file.
        self._ctor_params: dict[str, tuple[str, ...]] = {}
        self._ctor_param_attrs: dict[tuple[str, str], str] = {}
        self._pending_field_bindings: list[tuple[str, int | str, str]] = []
        self._module_language_cache: dict[str, cs.SupportedLanguage | None] = {}

    def record_ctor_params(self, class_qn: str, params: tuple[str, ...]) -> None:
        self._ctor_params[class_qn] = params

    def record_ctor_param_attr(self, class_qn: str, param: str, attr: str) -> None:
        self._ctor_param_attrs[(class_qn, param)] = attr

    def record_pending_field_binding(
        self, class_qn: str, key: int | str, func_qn: str
    ) -> None:
        # key: keyword name, or positional index awaiting the ctor param order.
        self._pending_field_bindings.append((class_qn, key, func_qn))

    def finalize_field_bindings(self) -> None:
        # Resolve pendings now that every class's ctor metadata is known. A
        # subclass without its own __init__ inherits the base's params and
        # field, so both a positional index and a keyword name resolve against
        # the nearest self-or-ancestor that owns the ctor param, and the
        # binding is recorded under THAT owner (where the field lives) so an
        # inherited self.handler(), typed to the base, still matches.
        for class_qn, key, func_qn in self._pending_field_bindings:
            if isinstance(key, int):
                owner_qn, params = self._ctor_params_owner(class_qn)
                if key >= len(params):
                    continue
                param = params[key]
            else:
                param = key
                owner_qn = self._ctor_param_owner(class_qn, param)
            field = self._ctor_param_attrs.get((owner_qn, param), param)
            self.record_callable_field_binding(owner_qn, field, func_qn)
        self._pending_field_bindings.clear()

    def _ctor_params_owner(self, class_qn: str) -> tuple[str, tuple[str, ...]]:
        # Nearest self-or-ancestor with a non-empty recorded ctor param list
        # (a subclass with no __init__ has an empty list, so keep walking).
        for ancestor in self._mro(class_qn):
            if params := self._ctor_params.get(ancestor):
                return ancestor, params
        return class_qn, self._ctor_params.get(class_qn, ())

    def _ctor_param_owner(self, class_qn: str, param: str) -> str:
        # Nearest self-or-ancestor whose ctor declares `param`, so an
        # inherited keyword binding attaches to the class that owns the field.
        for ancestor in self._mro(class_qn):
            if param in self._ctor_params.get(ancestor, ()):
                return ancestor
        return class_qn

    def _mro(self, class_qn: str) -> list[str]:
        # BFS over the inheritance graph, self first; guards cycles.
        seen: set[str] = set()
        order: list[str] = []
        queue: deque[str] = deque([class_qn])
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            queue.extend(self.class_inheritance.get(cur, []))
        return order

    def record_callable_field_binding(
        self, class_qn: str, field: str, func_qn: str
    ) -> None:
        # A NamedTuple/dataclass field holding a function reference: every
        # function bound to it at any construction site is a possible callee
        # when the field is invoked. Recording all of them is a sound call
        # graph (each runs for its own configuration), so recall is complete.
        self._field_bindings.setdefault((class_qn, field), set()).add(func_qn)
        self._field_to_classes.setdefault(field, set()).add(class_qn)

    def callable_field_targets(
        self, field: str, recv_type: str | None = None
    ) -> set[str]:
        classes = self._field_to_classes.get(field)
        if not classes:
            return set()
        if recv_type:
            simple = recv_type.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            matched = [
                qn
                for qn in classes
                if qn == recv_type or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == simple
            ]
            if len(matched) == 1:
                return self._field_bindings.get((matched[0], field), set())
        # Receiver type unknown or ambiguous: only resolve when exactly one
        # class declares this callable field, so the targets are unambiguous.
        if len(classes) == 1:
            return self._field_bindings.get((next(iter(classes)), field), set())
        return set()

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str:
        var_type = self._strip_optional(var_type)
        if cs.SEPARATOR_DOUBLE_COLON in var_type:
            return self._resolve_rust_class_qn(var_type)
        if cs.SEPARATOR_DOT in var_type:
            return self._follow_reexports(var_type)
        if var_type in import_map:
            # A Rust import target is a raw `::`-path (`crate::parse::Parse`) that
            # is not a registry qn; resolve it to the real type node so both
            # local-type dispatch and the external-receiver guard see it as
            # first-party (else its trie fallback is wrongly suppressed).
            target = import_map[var_type]
            if cs.SEPARATOR_DOUBLE_COLON in target:
                return self._resolve_rust_class_qn(target)
            return self._follow_reexports(target)
        return self._resolve_class_name(var_type, module_qn) or ""

    def _strip_optional(self, var_type: str) -> str:
        # An Optional annotation (X | None) names a single concrete class; reduce it
        # so attribute/operator resolution can find that class. Genuine multi-type
        # unions stay unresolved (ambiguous).
        if cs.PY_UNION_SEPARATOR not in var_type:
            return var_type
        non_none = [
            member
            for part in var_type.split(cs.PY_UNION_SEPARATOR)
            if (member := part.strip()) and member != cs.PY_NONE
        ]
        return non_none[0] if len(non_none) == 1 else var_type

    def _follow_reexports(self, class_qn: str) -> str:
        return follow_reexports(
            class_qn, self.import_processor.import_mapping, self.function_registry
        )

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

    def resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        return self._redirect_protocol_method(
            self._resolve_function_call(
                call_name,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
            )
        )

    def _resolve_js_prototype_sibling(
        self,
        call_name: str,
        caller_qn: str | None,
        language: cs.SupportedLanguage | None,
    ) -> tuple[str, str] | None:
        # Only a two-part `this.m` call in a JS/TS caller qualifies; the caller's
        # parent scope (module.Date for Date.prototype.strftime) is where the
        # prototype siblings were registered. A module-level caller degrades to
        # the same module-method lookup the CommonJS fallback performs.
        # A dotless caller_qn has no parent scope: rsplit would return the
        # caller itself and the lookup would land on the caller's own NESTED
        # function, which `this.m` never names.
        if language not in cs.JS_TS_LANGUAGES or not caller_qn:
            return None
        if cs.SEPARATOR_DOT not in caller_qn:
            return None
        if not call_name.startswith(cs.JS_THIS_CALL_PREFIX):
            return None
        method_name = call_name[len(cs.JS_THIS_CALL_PREFIX) :]
        if not method_name or cs.SEPARATOR_DOT in method_name:
            return None
        parent_scope = caller_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        method_qn = f"{parent_scope}{cs.SEPARATOR_DOT}{method_name}"
        if func_type := self.function_registry.get(method_qn):
            return func_type, method_qn
        return None

    def _resolve_enclosing_scope(
        self, call_name: str, caller_qn: str | None, module_qn: str
    ) -> tuple[str, str] | None:
        # Python LEGB: a bare name defined in the caller's own body or an enclosing
        # FUNCTION scope (a nested def) shadows module-level and same-named nested
        # defs in sibling scopes. The module-keyed trie fallback cannot tell two
        # sibling `traverse` defs apart, so resolve against the caller's scope chain
        # first. Walk up only through function/method scopes (each ancestor must be
        # in the registry); a class or the module boundary stops the walk, because
        # class scope is NOT part of a method's name-lookup chain in Python.
        if not caller_qn or cs.SEPARATOR_DOT in call_name:
            return None
        scope = caller_qn
        while True:
            candidate = f"{scope}{cs.SEPARATOR_DOT}{call_name}"
            if candidate in self.function_registry:
                return self.function_registry[candidate], candidate
            # A duplicate-variant caller (click's real `command` registers as
            # `command@168` behind its @t.overload stubs) owns nested defs the
            # def pass registers under the NATURAL qn (`command.decorator`);
            # probe the variant-stripped scope too, or the call falls to the
            # module trie and mis-binds to a sibling's same-named nested.
            last = scope.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            if cs.DUP_QN_MARKER in last:
                natural_scope = (
                    scope[: len(scope) - len(last)] + last.split(cs.DUP_QN_MARKER, 1)[0]
                )
                natural_candidate = f"{natural_scope}{cs.SEPARATOR_DOT}{call_name}"
                if natural_candidate in self.function_registry:
                    return (
                        self.function_registry[natural_candidate],
                        natural_candidate,
                    )
            if cs.SEPARATOR_DOT not in scope:
                return None
            parent = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if parent == module_qn or parent not in self.function_registry:
                return None
            scope = parent

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
        for qn, bases in self.class_inheritance.items():
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
                for qn, bases in self.class_inheritance.items()
                if any(base.rsplit(sep, 1)[-1] == cs.PY_PROTOCOL for base in bases)
            }
        return self._protocol_classes_cache

    def protocol_dispatch_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # A call resolved to a Protocol stub method (P.M) never runs the stub: the
        # runtime receiver is some conformer, so the sound call graph emits an edge
        # to M on every non-Protocol class that defines it. Gating on the resolved
        # target being a Protocol method keeps this from firing on ordinary calls.
        class_qn, sep, method_name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep or class_qn not in self._protocol_classes():
            return set()
        protocols = self._protocol_classes()
        targets: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(method_name):
            definer, dot, name = qn.rpartition(cs.SEPARATOR_DOT)
            if dot and name == method_name and definer not in protocols:
                targets.add((self.function_registry[qn], qn))
        return targets

    def self_dispatch_targets(
        self, class_context: str, method_name: str
    ) -> set[tuple[str, str]]:
        # self.M()/cls.M() statically targets the enclosing class's own or inherited
        # M, and dynamically dispatches to any concrete subclass override of M. Anchor
        # on the ENCLOSING class (not the resolved callee, which the trie may pick as
        # an arbitrary sibling override when M is abstract with several overrides) and
        # emit an edge to the enclosing-class method AND every concrete override, so an
        # override or an abstract base reached only through a self-call is not reported
        # dead.
        if not class_context or not method_name:
            return set()
        targets: set[tuple[str, str]] = set()
        # Skip abstract targets: an @abstractmethod stub never runs (the concrete
        # override does), so it must not be a call target; a concrete sibling/impl
        # wins. Abstract methods only "reached" polymorphically are handled as
        # dead-code roots, not by a spurious CALLS edge.
        if (base := self._try_resolve_method(class_context, method_name)) and (
            not self.function_registry.is_abstract(base[1])
        ):
            targets.add(base)
        for subclass_qn in self._concrete_subclasses(class_context):
            override_qn = f"{subclass_qn}{cs.SEPARATOR_DOT}{method_name}"
            if override_qn in self.function_registry and not (
                self.function_registry.is_abstract(override_qn)
            ):
                targets.add((self.function_registry[override_qn], override_qn))
        return targets

    def js_member_twin_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # `View.prototype.lookup = function lookup(...)` registers TWO nodes for
        # one method: the prototype path's `View.lookup` and the fn-expr's
        # own-name module-flat `view.lookup`. A call binds one twin and the
        # other reports dead. Return the same-name twin(s) whose parent chain
        # extends (or is extended by) the callee's parent, i.e. the same
        # module's flat/member pair, so the caller can edge both (the
        # duplicate-QN keep-both design). Never crosses modules.
        parent_qn, sep, leaf = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return set()
        twins: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(leaf):
            if qn == callee_qn:
                continue
            other_parent, d, other_leaf = qn.rpartition(cs.SEPARATOR_DOT)
            if not d or other_leaf != leaf:
                continue
            if not (
                other_parent.startswith(f"{parent_qn}{cs.SEPARATOR_DOT}")
                or parent_qn.startswith(f"{other_parent}{cs.SEPARATOR_DOT}")
            ):
                continue
            label = self.function_registry.get(qn)
            if label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
                twins.add((label, qn))
        return twins

    def go_package_sibling_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # Go package-level functions are package-scoped, but cgr keys each file as
        # its own module (`pkgdir.file.name`). Two same-package functions with the
        # same name can ONLY be mutually-exclusive build-tag variants (gin's
        # `validate` under `//go:build !nomsgpack` vs `nomsgpack`); the compiler
        # rejects duplicate top-level identifiers in a package otherwise. A bare call
        # resolves to just one file's copy, orphaning the other build's copy; return
        # every same-package same-name package-level sibling so no build variant is
        # reported dead. Revive-only and precise: a same-name function in a DIFFERENT
        # package (different directory) is a distinct function, never a variant, so
        # the package-dir equality guard excludes it.
        file_module_qn, sep, name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return set()
        pkg_dir, dsep, _file = file_module_qn.rpartition(cs.SEPARATOR_DOT)
        if not dsep:
            return set()
        targets: set[tuple[str, str]] = set()
        for qn in self.function_registry.find_ending_with(name):
            label = self.function_registry.get(qn)
            if qn == callee_qn or label != cs.NodeLabel.FUNCTION:
                continue
            other_module, d, other_name = qn.rpartition(cs.SEPARATOR_DOT)
            if not d or other_name != name or other_module == file_module_qn:
                continue
            other_pkg, d2, other_file = other_module.rpartition(cs.SEPARATOR_DOT)
            # Same directory is necessary but not sufficient for same-package: Go
            # permits an external test package (`package p_test`) in a `_test.go` file
            # sharing the directory. Production code can never call a function defined
            # in a `_test.go` file, so exclude such siblings; else a genuinely
            # test-only dead function would be masked as live.
            if (
                d2
                and other_pkg == pkg_dir
                and not other_file.endswith(cs.GO_TEST_FILE_SUFFIX)
            ):
                targets.add((label, qn))
        return targets

    def java_constructor_targets(self, class_qn: str) -> set[tuple[str, str]]:
        # A Java constructor is registered as a method directly under its class whose
        # simple name equals the class's simple name (`Foo.Foo(int)`). `new Foo(...)`
        # resolves to the CLASS, so redirect a CALLS edge to each declared constructor
        # (all overloads); argument-type overload selection is not attempted, which is
        # unnecessary for reachability and never fabricates a call to a
        # non-constructor. Only constructors DIRECTLY on the class match (a nested
        # class's constructor has an extra qn segment and is excluded).
        simple = class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        targets: set[tuple[str, str]] = set()
        for qn, node_type in self.function_registry.find_with_prefix(class_qn):
            head = qn.split(cs.CHAR_PAREN_OPEN, 1)[0]
            parent, dot, mname = head.rpartition(cs.SEPARATOR_DOT)
            if dot and parent == class_qn and mname == simple:
                targets.add((node_type, qn))
        return targets

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
        # own base-dtor edge (Greptile, PR #894). A destructor cannot be
        # overloaded, so each qn is one direct registry lookup (PR #799).
        targets: set[tuple[str, str]] = set()
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            simple = current.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            dtor_qn = f"{current}{cs.SEPARATOR_DOT}{cs.CPP_DESTRUCTOR_PREFIX}{simple}"
            dtor_type = self.function_registry.get(dtor_qn)
            if dtor_type is not None:
                targets.add((dtor_type, dtor_qn))
            stack.extend(self.class_inheritance.get(current, ()))
        return targets

    def cpp_braced_return_class(self, caller_qn: str, module_qn: str) -> str | None:
        # The class constructed by a `return {...};` inside caller_qn: the
        # caller's recorded return type, resolved to a registered class
        # (None for primitives/auto/unrecorded).
        return_type = self.type_inference.method_return_types.get(caller_qn)
        if not return_type:
            return None
        return self._resolve_type_to_class_qn(return_type, module_qn)

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
            definer, dot, name = qn.rpartition(cs.SEPARATOR_DOT)
            if (
                dot
                and name == method_name
                and self.function_registry[qn] == cs.NodeLabel.METHOD
            ):
                targets.add((self.function_registry[qn], qn))
        return targets

    def _interface_impl_map(self) -> dict[str, str]:
        # Map an interface to its SOLE first-party implementer. A call typed to an
        # interface resolves to the interface's own method declaration (the static
        # callee); when the interface has exactly one implementer the concrete
        # method is the one that runs, so ALSO edge it (call-graph accuracy).
        # >1 implementer is ambiguous -> not mapped -> the call stays on the
        # interface method alone (no precision risk, recall preserved).
        if self._interface_impl_cache is None:
            self._interface_impl_cache = {
                interface_qn: next(iter(implementers))
                for interface_qn, implementers in self.interface_implementers.items()
                if len(implementers) == 1
            }
        return self._interface_impl_cache

    def interface_sole_impl_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        # A callee that IS an interface/trait method (the receiver was typed to
        # the interface -- a concrete receiver dispatches to the impl directly)
        # with exactly one implementer also runs the concrete method, so return
        # it for an additional CALLS edge. REPLACING the interface edge instead
        # (the pre-#665-era redirect) orphaned the interface stub: OVERRIDES
        # expansion only walks interface -> impl, so the stub's declaration
        # (gson's FieldNamingStrategy.translateName) reported dead.
        class_qn, sep, method_name = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return set()
        impl_qn = self._interface_impl_map().get(class_qn)
        if impl_qn is None:
            return set()
        if result := self._try_resolve_method(impl_qn, method_name):
            return {result}
        return set()

    def _redirect_protocol_method(
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

    def _resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        # Enclosing-scope (nested def) lookup is caller-specific, so it must run
        # before the module-keyed cache/trie, which would otherwise return a sibling
        # scope's same-named nested function.
        if result := self._resolve_enclosing_scope(call_name, caller_qn, module_qn):
            return result

        # `this.m()` inside a prototype-assigned function dispatches to a sibling
        # method of the same prototype target (Date.prototype.strftime calling
        # this.getTwoDigitMonth()); the caller's parent scope names that target.
        # Caller-specific, so it too must run before the module-keyed cache; the
        # CommonJS module-receiver fallback still applies when no sibling exists.
        if result := self._resolve_js_prototype_sibling(call_name, caller_qn, language):
            return result

        # The cache is keyed by (call_name, module_qn) only, so a caller whose
        # class carries extends-clause type arguments must bypass it: its
        # member resolution is class-context-dependent (#875) and a sibling
        # class's cached answer for the same name would be wrong.
        use_cache = not local_var_types and not (
            language == cs.SupportedLanguage.DART
            and class_context in self.type_inference.dart_extends_type_args
        )
        if use_cache:
            cache_key = (call_name, module_qn)
            if cache_key in self._simple_resolution_cache:
                return self._simple_resolution_cache[cache_key]

        if result := self._try_resolve_iife(call_name, module_qn):
            return result

        if self._is_super_call(call_name):
            return self._resolve_super_call(call_name, class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            # A chained call resolves via return-type inference only; it does NOT
            # fall through to the trie fallback, because a hop returning a container
            # (`Kids() []Command`) or an unknown type must drop the edge rather than
            # rebind the final method by bare name (a false `Command.Run`). C++ is the
            # exception: a `foo().bar()` receiver with an unrecordable return type
            # (`auto`/trailing/decltype, e.g. fmt's get_container(out).append) fell to
            # the bare-method trie before chained typing existed, so preserve that
            # fallback for C++ to avoid dropping edges the typing can't yet recover.
            return self._resolve_chained_call(
                call_name,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
            )

        if result := self._try_resolve_via_imports(
            call_name, module_qn, local_var_types, language
        ):
            if use_cache:
                self._simple_resolution_cache[cache_key] = result
            return result

        if result := self._try_resolve_same_module(call_name, module_qn):
            if use_cache:
                self._simple_resolution_cache[cache_key] = result
            return result

        if class_context and (
            result := self._resolve_self_sibling_method(call_name, class_context)
        ):
            return result

        # A bare name explicitly imported from outside the project binds to that
        # external symbol. Since precise import / same-module resolution above
        # already failed, the symbol is unindexed; do NOT let the simple-name
        # trie fallback rebind it to an unrelated first-party symbol of the same
        # name. (The instantiation eval caught `from evals import GraphData;
        # GraphData()` being resolved to codebase_rag's own GraphData class.)
        if cs.SEPARATOR_DOT not in call_name and self._is_external_import(
            call_name, module_qn
        ):
            if use_cache:
                self._simple_resolution_cache[cache_key] = None
            return None

        # A dotted call on an import whose target still holds a raw
        # slash-separated module path (github.com/x/y) is a call into an
        # EXTERNAL module: local Go paths were rewritten to project qns at
        # import time, so a surviving slash path is definitionally outside
        # the repo. The symbol is unindexed; do not let the last-segment trie
        # fallback rebind it to an unrelated first-party function.
        if self._is_external_path_import(call_name, module_qn):
            if use_cache:
                self._simple_resolution_cache[cache_key] = None
            return None

        # A member call `obj.method` whose receiver has a KNOWN inferred type that is
        # not a first-party class is a call on an external object (e.g. a
        # `std::string`). Precise local-type resolution above already failed, so the
        # method lives on the external type; do NOT let the simple-name trie fallback
        # rebind it to an unrelated first-party method of the same name. Untyped
        # receivers keep the fallback (their type is unknown, not known-external).
        if self._receiver_type_is_external(call_name, module_qn, local_var_types):
            if use_cache:
                self._simple_resolution_cache[cache_key] = None
            return None

        # A JS/TS/Dart member call on an UNTYPED receiver (`view.render(...)`
        # where `view` is a param constructed in the caller) targets a MEMBER:
        # the bare-name trie would rebind it to an arbitrary same-named
        # candidate (express's application.render; a Dart `h.greet()` picking
        # the closest class's greet), a false edge that also hides the real
        # method from dead-code. Bind the UNIQUE member-like candidate (parent
        # qn itself registered) or drop. Typed Dart receivers never reach this:
        # the local-type path above already bound them.
        if (
            language in cs.JS_TS_LANGUAGES or language == cs.SupportedLanguage.DART
        ) and cs.SEPARATOR_DOT in call_name:
            if language == cs.SupportedLanguage.DART and (
                result := self._resolve_dart_external_base_arg_member(
                    call_name, class_context, local_var_types
                )
            ):
                return result
            result = self._resolve_js_member_call_unique(call_name, module_qn)
            if use_cache:
                self._simple_resolution_cache[cache_key] = result
            return result

        result = self._try_resolve_via_trie(call_name, module_qn)
        if use_cache:
            self._simple_resolution_cache[cache_key] = result
        return result

    def _resolve_dart_external_base_arg_member(
        self,
        call_name: str,
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        # `recv.member(...)` on a receiver undeclared in first-party scope,
        # inside a class extending an EXTERNAL generic base: the only
        # first-party types the base can hand back are its type arguments
        # (`State<GridBtn>.widget` is a GridBtn), so bind when exactly one
        # argument's class defines the member (issue #875). A first-party
        # base, a declared receiver, or a member named like the receiver on
        # the class or ANY registered ancestor (a first-party mixin's getter
        # is inherited, not handed back by the external base) all fall
        # through to the unique-member gate instead.
        if not class_context:
            return None
        type_args = self.type_inference.dart_extends_type_args.get(class_context)
        if not type_args:
            return None
        receiver, sep, member = call_name.partition(cs.SEPARATOR_DOT)
        if not sep or cs.SEPARATOR_DOT in member:
            return None
        if local_var_types and receiver in local_var_types:
            return None
        if self._dart_registered_member_named(class_context, receiver):
            return None
        bases = self.class_inheritance.get(class_context)
        # resolve_deferred_inherits rewrote bases in place, so a registered
        # bases[0] means a first-party extends base owns its members.
        if not bases or self.function_registry.get(bases[0]) is not None:
            return None
        matches = [
            qn
            for arg_qn in dict.fromkeys(type_args)
            if (qn := f"{arg_qn}{cs.SEPARATOR_DOT}{member}") in self.function_registry
        ]
        if len(matches) == 1:
            return self.function_registry[matches[0]], matches[0]
        return None

    def _dart_registered_member_named(self, class_qn: str, name: str) -> bool:
        # True when the class or any registered first-party ancestor
        # (extends base or `with` mixin, both in class_inheritance) defines
        # a member with this name. Dart `implements` contributes interface
        # only, never implementation, so it cannot supply a receiver.
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if f"{current}{cs.SEPARATOR_DOT}{name}" in self.function_registry:
                return True
            stack.extend(self.class_inheritance.get(current, ()))
        return False

    def _resolve_js_member_call_unique(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        method_name = call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        candidates: list[str] = []
        for qn in self.function_registry.find_ending_with(method_name):
            parent_qn, sep, leaf = qn.rpartition(cs.SEPARATOR_DOT)
            if not sep or leaf != method_name:
                continue
            # Member-like: the parent is itself a registered node (a class, a
            # prototype constructor Function, an object scope); a free
            # function's parent is a module, which is never in the registry.
            if parent_qn in self.function_registry:
                candidates.append(qn)
        # Only candidates VISIBLE to the calling module count: parent module
        # imported here or defined here (express's tryRender imports ./view, so
        # View.render qualifies; an unrelated example's GithubView.render does
        # not). Required even for a SINGLETON: an untyped `client.render()` in
        # a file with no relation to the sole View.render must drop, not grow a
        # false cross-module edge that hides real dead code. A JS require maps
        # the MODULE (`require('./view')` -> express.lib.view), so a visible
        # candidate's parent may equal an import or sit anywhere under one.
        import_map = self.import_processor.import_mapping.get(module_qn) or {}
        imported = set(import_map.values())
        visible = [
            qn
            for qn in candidates
            if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
            or any(
                qn.rpartition(cs.SEPARATOR_DOT)[0] == imp
                or qn.rpartition(cs.SEPARATOR_DOT)[0].startswith(
                    f"{imp}{cs.SEPARATOR_DOT}"
                )
                for imp in imported
            )
        ]
        if len(visible) == 1:
            return self.function_registry[visible[0]], visible[0]
        return None

    def _is_external_path_import(self, call_name: str, module_qn: str) -> bool:
        # True when the dotted call's object segment is imported from a target
        # that is still a slash-separated module path; for Go, every local
        # path was rewritten to a project qn at import time, so a surviving
        # slash means external. JS/TS non-standard-scheme imports
        # (ext:deno_node/y) alias first-party code and keep their trie
        # fallback, mirroring _is_external_import.
        if cs.SEPARATOR_DOT not in call_name:
            return False
        object_name = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        import_map = self.import_processor.import_mapping.get(module_qn)
        if not import_map:
            return False
        target = import_map.get(object_name)
        if not target or cs.SEPARATOR_SLASH not in target:
            return False
        bare_imports = self.import_processor.js_ts_bare_imports.get(module_qn)
        return not (bare_imports and object_name in bare_imports)

    def _is_external_import(self, call_name: str, module_qn: str) -> bool:
        # True when call_name is imported in module_qn from a module outside the
        # project. First-party imports are written either project-prefixed
        # (`from proj.w import X`) or bare (`from utils.helpers import X`, where
        # the registered node is `proj.utils.helpers.X`); both are first-party
        # and left to the trie fallback. Only a target neither rooted at the
        # project nor registered under the project prefix is external, so this
        # suppresses cross-project fuzzy rebinds without dropping real
        # first-party calls.
        import_map = self.import_processor.import_mapping.get(module_qn)
        if not import_map:
            return False
        target = import_map.get(call_name)
        if not target:
            return False
        # A PHP `use function A\B\c` target is a namespace path, which never
        # matches cgr's file-path qualified name (a global helper declares
        # `namespace Illuminate\Support` from Collections/functions.php). Treating
        # it as external would suppress the simple-name trie fallback that a bare
        # PHP call already relies on, dropping the call; leave it to the trie.
        # LIMITATION: cgr qualifies PHP functions by file path and does not track
        # the `namespace` declaration, so a genuinely external
        # `use function Vendor\pkg\helper` cannot be told apart from a
        # path-mismatched first-party one; both defer to the trie, as a bare
        # `helper()` call already does. Precise disambiguation would need
        # systemic PHP namespace tracking.
        php_imports = self.import_processor.php_function_imports.get(module_qn)
        if php_imports and call_name in php_imports:
            return False
        # A JS/TS import with a non-standard scheme (deno `ext:deno_node/x`) does
        # not resolve to a file-path module qn, so its target is unregistered and
        # looks external even though it aliases first-party code. Defer to the
        # simple-name trie (like a relative import that misses) instead of
        # suppressing. Ordinary package specifiers (bare, scoped, node:/npm:) are
        # NOT recorded here, so genuine external calls stay suppressed.
        bare_imports = self.import_processor.js_ts_bare_imports.get(module_qn)
        if bare_imports and call_name in bare_imports:
            return False
        # Only dotted absolute-path imports (Python/Java `pkg.mod.Name`) are
        # judged here. Rust/C++ record relative or `::`-separated targets
        # (`super::b::helper`) that never carry the project prefix and rely on
        # the trie fallback to resolve, so they must not be mistaken external.
        if cs.SEPARATOR_DOT not in target or cs.SEPARATOR_DOUBLE_COLON in target:
            return False
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if target.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return f"{project_root}{cs.SEPARATOR_DOT}{target}" not in self.function_registry

    def _receiver_type_is_external(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        # True only for a two-part dotted member call `obj.method` whose `obj` has an
        # inferred local type that is known to be external. The receiver type is
        # external when it resolves to nothing, or to a qn neither registered nor
        # rooted at the project (a `std::string` -> `std.string`). Then the method
        # lives on the external type, so the simple-name trie fallback must not
        # rebind it to a same-named first-party method. An untyped receiver (obj
        # absent from the map) or a project-rooted type is left alone: its method may
        # still be resolved by the fallback (e.g. a cross-file imported-class call the
        # precise path missed), so only a provably external type is suppressed.
        if not local_var_types or cs.SEPARATOR_DOT not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return False
        var_type = local_var_types.get(parts[0])
        if var_type is None:
            return False
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
        if not class_qn:
            return True
        # First-party class qns may be written without the project prefix (a bare
        # `from models.user import User` resolves to `models.user.User` while the
        # registry stores `proj.models.user.User`), so check both the qn as-is and
        # the project-prefixed form before judging a type external, mirroring
        # _is_external_import. A project-rooted qn is always treated as first-party.
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if class_qn.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return (
            class_qn not in self.function_registry
            and f"{project_root}{cs.SEPARATOR_DOT}{class_qn}"
            not in self.function_registry
        )

    def _try_resolve_iife(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name:
            return None
        if not (
            call_name.startswith(cs.IIFE_FUNC_PREFIX)
            or call_name.startswith(cs.IIFE_ARROW_PREFIX)
        ):
            return None
        iife_qn = f"{module_qn}.{call_name}"
        if iife_qn in self.function_registry:
            return self.function_registry[iife_qn], iife_qn
        return None

    def _is_super_call(self, call_name: str) -> bool:
        return (
            call_name == cs.KEYWORD_SUPER
            or call_name.startswith(f"{cs.KEYWORD_SUPER}.")
            or call_name.startswith(f"{cs.KEYWORD_SUPER}()")
        )

    def _try_resolve_via_imports(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        import_map = self.import_processor.import_mapping.get(module_qn)
        if import_map is None:
            # A module with no `use`/import statements can still resolve a member
            # call `obj.method()` through an inferred local/self type (a
            # self-contained Rust lib.rs is the common case). Only the
            # import-dependent lookups below (direct, qualified-by-import,
            # wildcard) are no-ops here, so proceed with an empty map when there
            # is type info to drive resolution; otherwise nothing would match.
            if not local_var_types:
                return None
            import_map = {}

        if result := self._try_resolve_direct_import(call_name, import_map):
            return result

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types, language
        ):
            return result

        return self._try_resolve_wildcard_imports(call_name, import_map)

    def _try_resolve_direct_import(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        if call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        if imported_qn in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=imported_qn)
            return self.function_registry[imported_qn], imported_qn
        return None

    def _try_resolve_qualified_call(
        self,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            separator = cs.SEPARATOR_DOUBLE_COLON
        elif cs.CHAR_COLON in call_name:
            separator = cs.CHAR_COLON
        elif cs.SEPARATOR_DOT in call_name:
            separator = cs.SEPARATOR_DOT
        else:
            return None

        parts = call_name.split(separator)

        if len(parts) == 2:
            if result := self._resolve_two_part_call(
                parts,
                call_name,
                separator,
                import_map,
                module_qn,
                local_var_types,
                language,
            ):
                return result

        if len(parts) >= 3 and parts[0] == cs.KEYWORD_SELF:
            return self._resolve_self_attribute_call(
                parts, call_name, import_map, module_qn, local_var_types
            )

        return self._resolve_multi_part_call(
            parts, call_name, import_map, module_qn, local_var_types
        )

    def _has_separator(self, call_name: str) -> bool:
        return (
            cs.SEPARATOR_DOT in call_name
            or cs.SEPARATOR_DOUBLE_COLON in call_name
            or cs.CHAR_COLON in call_name
        )

    def _get_separator(self, call_name: str) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            return cs.SEPARATOR_DOUBLE_COLON
        if cs.CHAR_COLON in call_name:
            return cs.CHAR_COLON
        return cs.SEPARATOR_DOT

    def _try_resolve_wildcard_imports(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        map_id = id(import_map)
        if map_id not in self._wildcard_cache:
            self._wildcard_cache[map_id] = (
                [(k, v) for k, v in import_map.items() if k[0] == "*"]
                if import_map
                else []
            )
        wildcards = self._wildcard_cache[map_id]
        if not wildcards:
            return None
        for _, imported_qn in wildcards:
            if result := self._try_wildcard_qns(call_name, imported_qn):
                return result
        return None

    def _try_wildcard_qns(
        self, call_name: str, imported_qn: str
    ) -> tuple[str, str] | None:
        potential_qns = []
        if cs.SEPARATOR_DOUBLE_COLON not in imported_qn:
            potential_qns.append(f"{imported_qn}.{call_name}")
        potential_qns.append(f"{imported_qn}{cs.SEPARATOR_DOUBLE_COLON}{call_name}")

        for wildcard_qn in potential_qns:
            if wildcard_qn in self.function_registry:
                logger.debug(ls.CALL_WILDCARD, call_name=call_name, qn=wildcard_qn)
                return self.function_registry[wildcard_qn], wildcard_qn
        return None

    def _try_resolve_same_module(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                ls.CALL_SAME_MODULE, call_name=call_name, qn=same_module_func_qn
            )
            return self.function_registry[same_module_func_qn], same_module_func_qn
        return None

    def _nameable_candidates(self, candidates: list[str], module_qn: str) -> list[str]:
        # The simple-name fallback matches on the last name segment alone, so
        # it can offer a definition the caller could never name: one scoped
        # inside another function's body, or one written in a language the
        # caller's cannot call into (a Python `asyncio.sleep` binding to a
        # `sleep` closure in a generated TypeScript client, issue #945). Drop
        # both; a candidate whose language cannot be determined is kept, so
        # the fallback still answers wherever this evidence is missing.
        caller_language = self._module_language(module_qn)
        return [
            qn
            for qn in candidates
            if self.function_registry.get(self._parent_qn(qn))
            not in _SCOPING_PARENT_TYPES
            and self._languages_can_call(caller_language, self._module_language(qn))
        ]

    @staticmethod
    def _parent_qn(qualified_name: str) -> str:
        return qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[0]

    @staticmethod
    def _languages_can_call(
        caller: cs.SupportedLanguage | None, candidate: cs.SupportedLanguage | None
    ) -> bool:
        if caller is None or candidate is None or caller == candidate:
            return True
        return any(
            caller in family and candidate in family
            for family in _CALLABLE_LANGUAGE_FAMILIES
        )

    def _module_language(self, qualified_name: str) -> cs.SupportedLanguage | None:
        # The language of the module a qn lives in, found by walking off its
        # trailing segments until one names an ingested module (a caller's own
        # module qn hits on the first probe). Cached per qn: the fallback asks
        # for the same candidates repeatedly.
        if qualified_name in self._module_language_cache:
            return self._module_language_cache[qualified_name]
        modules = self.type_inference.module_qn_to_file_path
        probe = qualified_name
        language: cs.SupportedLanguage | None = None
        while probe:
            if (path := modules.get(probe)) is not None:
                language = get_language_for_extension(path.suffix)
                break
            if cs.SEPARATOR_DOT not in probe:
                break
            probe = self._parent_qn(probe)
        self._module_language_cache[qualified_name] = language
        return language

    def _try_resolve_via_trie(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        search_name = _SEARCH_NAME_CACHE.get(call_name)
        if search_name is None:
            search_name = _SEPARATOR_PATTERN.split(call_name)[-1]
            _SEARCH_NAME_CACHE[call_name] = search_name
        possible_matches = self._nameable_candidates(
            self.function_registry.find_ending_with(search_name), module_qn
        )
        if not possible_matches:
            logger.debug(ls.CALL_UNRESOLVED, call_name=call_name)
            return None

        if len(possible_matches) == 1:
            best_candidate_qn = possible_matches[0]
        else:
            caller_parts = module_qn.split(cs.SEPARATOR_DOT)
            caller_len = len(caller_parts)
            caller_parent_prefix = (
                cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
                if caller_len > 1
                else ""
            )
            best_candidate_qn = min(
                possible_matches,
                key=lambda qn: (
                    # An @abstractmethod stub never runs when a concrete override
                    # exists, so prefer concrete candidates over abstract ones
                    # even when the abstract stub is closer by import distance.
                    self.function_registry.is_abstract(qn),
                    self._import_distance_fast(
                        qn, caller_parts, caller_len, caller_parent_prefix
                    ),
                    qn,
                ),
            )
        logger.debug(ls.CALL_TRIE_FALLBACK, call_name=call_name, qn=best_candidate_qn)
        return self.function_registry[best_candidate_qn], best_candidate_qn

    def _resolve_two_part_call(
        self,
        parts: list[str],
        call_name: str,
        separator: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        object_name, method_name = parts

        if result := self._try_resolve_via_local_type(
            object_name,
            method_name,
            separator,
            call_name,
            import_map,
            module_qn,
            local_var_types,
        ):
            return result

        if result := self._try_resolve_via_import(
            object_name, method_name, separator, call_name, import_map
        ):
            return result

        # A same-module associated-function call `Type::assoc()` (`Ping::new()`):
        # the object is a type defined in this module, not an import. Resolve it to
        # its class node and look up the method there. Gated to `::` so a `.`-dotted
        # receiver of unknown type still falls through to the trie fallback.
        if separator == cs.SEPARATOR_DOUBLE_COLON and (
            result := self._try_resolve_static_type_method(
                object_name, method_name, call_name, module_qn
            )
        ):
            return result

        # A JS/TS dotted call binds to a same-module free function ONLY through
        # a module-ish receiver (`exports.render()`, `this.render()` in the
        # CommonJS/prototype pattern). An ordinary identifier receiver
        # (`view.render()`) is an instance call: binding it to the free
        # function is a false edge that also kills the real prototype method
        # (express's View.render); let it fall to the unique-member gate.
        if (
            language in cs.JS_TS_LANGUAGES
            and separator == cs.SEPARATOR_DOT
            and object_name not in cs.JS_MODULE_RECEIVERS
        ):
            return None
        return self._try_resolve_module_method(method_name, call_name, module_qn)

    def _try_resolve_static_type_method(
        self, object_name: str, method_name: str, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not (class_qn := self._resolve_class_name(object_name, module_qn)):
            return None
        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_TYPE_INFERRED,
                call_name=call_name,
                method_qn=method_qn,
                obj=object_name,
                var_type=object_name,
            )
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

    def _try_resolve_via_local_type(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if not local_var_types or object_name not in local_var_types:
            return None

        var_type = local_var_types[object_name]

        if class_qn := self._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        ):
            if result := self._try_method_on_class(
                class_qn, method_name, separator, call_name, object_name, var_type
            ):
                return result

        if var_type in cs.JS_BUILTIN_TYPES:
            return (
                cs.NodeLabel.FUNCTION,
                f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{var_type}{cs.SEPARATOR_PROTOTYPE}{method_name}",
            )
        return None

    def _try_method_on_class(
        self,
        class_qn: str,
        method_name: str,
        separator: str,
        call_name: str,
        object_name: str,
        var_type: str,
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_TYPE_INFERRED,
                call_name=call_name,
                method_qn=method_qn,
                obj=object_name,
                var_type=var_type,
            )
            return self.function_registry[method_qn], method_qn

        if inherited := self._resolve_inherited_method(class_qn, method_name):
            logger.debug(
                ls.CALL_TYPE_INFERRED_INHERITED,
                call_name=call_name,
                method_qn=inherited[1],
                obj=object_name,
                var_type=var_type,
            )
            return inherited
        return None

    def _try_resolve_via_import(
        self,
        object_name: str,
        method_name: str,
        separator: str,
        call_name: str,
        import_map: dict[str, str],
    ) -> tuple[str, str] | None:
        if object_name not in import_map:
            return None

        class_qn = self._resolve_imported_class_qn(
            import_map[object_name], object_name, method_name, separator
        )

        registry_separator = (
            separator if separator == cs.CHAR_COLON else cs.SEPARATOR_DOT
        )
        method_qn = f"{class_qn}{registry_separator}{method_name}"

        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_IMPORT_STATIC, call_name=call_name, method_qn=method_qn
            )
            return self.function_registry[method_qn], method_qn
        return self._try_resolve_package_member(class_qn, method_name)

    def _try_resolve_package_member(
        self, package_qn: str, member_name: str
    ) -> tuple[str, str] | None:
        # A Go package spans multiple files and cgr qualifies its members by
        # FILE (pkg.file.Func), so an import-mapped package qn plus member name
        # misses the registry by one segment. Search the package's file modules
        # for the member; Go names are unique per package, so at most one
        # function matches (min() keeps a same-named type-method collision
        # deterministic). The module ROOT package maps to the bare project name
        # (no dot), so accept it alongside project-dot-prefixed package qns.
        project_name = self.import_processor.project_name
        if package_qn != project_name and not package_qn.startswith(
            f"{project_name}{cs.SEPARATOR_DOT}"
        ):
            return None
        member_depth = package_qn.count(cs.SEPARATOR_DOT) + 2
        candidates = [
            qn
            for qn, _ in self.function_registry.find_with_prefix(package_qn)
            if qn.count(cs.SEPARATOR_DOT) == member_depth
            and qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == member_name
        ]
        if not candidates:
            return None
        member_qn = min(candidates)
        logger.debug(ls.CALL_PACKAGE_MEMBER, member=member_name, qn=member_qn)
        return self.function_registry[member_qn], member_qn

    def _resolve_imported_class_qn(
        self,
        class_qn: str,
        object_name: str,
        method_name: str,
        separator: str,
    ) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in class_qn:
            class_qn = self._resolve_rust_class_qn(class_qn)

        potential_class_qn = f"{class_qn}.{object_name}"
        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
        if test_method_qn in self.function_registry:
            return potential_class_qn
        return class_qn

    def _resolve_rust_class_qn(self, class_qn: str) -> str:
        rust_parts = class_qn.split(cs.SEPARATOR_DOUBLE_COLON)
        class_name = rust_parts[-1]

        # A Rust receiver type may be a struct (Class), an enum (`Frame`), or a
        # type alias, all of which carry impl methods; match any of them, else an
        # enum-typed receiver fails to resolve and its type reads as external,
        # wrongly suppressing the trie fallback.
        matching_qns = self.function_registry.find_ending_with(class_name)
        return next(
            (
                qn
                for qn in matching_qns
                if self.function_registry.get(qn) in _RS_TYPE_NODE_TYPES
            ),
            class_qn,
        )

    def _try_resolve_module_method(
        self, method_name: str, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        method_qn = f"{module_qn}.{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_OBJECT_METHOD, call_name=call_name, method_qn=method_qn
            )
            return self.function_registry[method_qn], method_qn
        return None

    def _resolve_self_attribute_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        attribute_ref = cs.SEPARATOR_DOT.join(parts[:-1])
        method_name = parts[-1]

        if local_var_types and attribute_ref in local_var_types:
            var_type = local_var_types[attribute_ref]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR,
                        call_name=call_name,
                        method_qn=method_qn,
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(
                    class_qn, method_name
                ):
                    logger.debug(
                        ls.CALL_INSTANCE_ATTR_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        attr_ref=attribute_ref,
                        var_type=var_type,
                    )
                    return inherited_method

        return None

    def _resolve_multi_part_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        class_name = parts[0]
        method_name = cs.SEPARATOR_DOT.join(parts[1:])

        if class_name in import_map:
            class_qn = import_map[class_name]
            method_qn = f"{class_qn}.{method_name}"
            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_IMPORT_QUALIFIED,
                    call_name=call_name,
                    method_qn=method_qn,
                )
                return self.function_registry[method_qn], method_qn

        if local_var_types and class_name in local_var_types:
            var_type = local_var_types[class_name]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
                method_qn = f"{class_qn}.{method_name}"
                if method_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_INSTANCE_QUALIFIED,
                        call_name=call_name,
                        method_qn=method_qn,
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return self.function_registry[method_qn], method_qn

                if inherited_method := self._resolve_inherited_method(
                    class_qn, method_name
                ):
                    logger.debug(
                        ls.CALL_INSTANCE_INHERITED,
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        class_name=class_name,
                        var_type=var_type,
                    )
                    return inherited_method

        return self._resolve_field_hop_method(
            parts, call_name, import_map, module_qn, local_var_types
        )

    def _resolve_field_hop_method(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        # A paren-free field-hop receiver called inline (gin's `c.writermem.reset`):
        # `c` is a typed local (Context), each middle segment is a struct FIELD whose
        # recorded type advances the receiver (writermem -> responseWriter), and the
        # final segment is a method on the last field's type. Resolves ONLY when every
        # middle segment is a known field and the method exists (never a name-only
        # fallback), so it can revive a dropped edge but never mis-bind. Distinct
        # from the stored-local field-hop (`root := e.field.get(); root.m()`) which is
        # already typed via _enrich_go_call_locals; this is the no-local direct form.
        if len(parts) < 3 or not local_var_types:
            return None
        current_type = local_var_types.get(parts[0])
        if not current_type:
            return None
        class_qn = self._resolve_class_qn_from_type(current_type, import_map, module_qn)
        for field in parts[1:-1]:
            if not class_qn:
                return None
            field_type = self.type_inference.class_field_types.get(class_qn, {}).get(
                field
            )
            if not field_type:
                return None
            class_qn = self._chain_class_qn(field_type, module_qn)
        if not class_qn:
            return None
        method_name = parts[-1]
        method_qn = f"{class_qn}.{method_name}"
        if method_qn in self.function_registry:
            logger.debug(
                ls.CALL_INSTANCE_QUALIFIED,
                call_name=call_name,
                method_qn=method_qn,
                class_name=parts[0],
                var_type=current_type,
            )
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

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
        class_qn = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
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
            if resolved := self._try_resolve_method(candidate, dunder):
                targets.add(resolved)
        return targets

    def _protocol_structural_implementers(self, protocol_qn: str) -> set[str]:
        # Classes that define every method declared on the Protocol (own or
        # inherited). Used to dispatch operator dunders to the concrete type when the
        # Protocol/implementer names don't follow the XxxProtocol convention.
        if protocol_qn in self._struct_impl_cache:
            return self._struct_impl_cache[protocol_qn]
        sep = cs.SEPARATOR_DOT
        protocol_methods = {
            qn.rsplit(sep, 1)[-1]
            for qn, node_type in self.function_registry.find_with_prefix(protocol_qn)
            if node_type == NodeType.METHOD and qn.rsplit(sep, 1)[0] == protocol_qn
        }
        result: set[str] = set()
        if protocol_methods:
            protocols = self._protocol_classes()
            for candidate in self.class_inheritance:
                if candidate in protocols:
                    continue
                if all(
                    self._try_resolve_method(candidate, method)
                    for method in protocol_methods
                ):
                    result.add(candidate)
        self._struct_impl_cache[protocol_qn] = result
        return result

    def resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        if call_name in cs.JS_BUILTIN_PATTERNS:
            return (cs.NodeLabel.FUNCTION, f"{cs.BUILTIN_PREFIX}.{call_name}")

        for suffix, method in cs.JS_FUNCTION_PROTOTYPE_SUFFIXES.items():
            if call_name.endswith(suffix):
                return (
                    cs.NodeLabel.FUNCTION,
                    f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}Function{cs.SEPARATOR_PROTOTYPE}{method}",
                )

        if cs.SEPARATOR_PROTOTYPE in call_name and (
            call_name.endswith(cs.JS_SUFFIX_CALL)
            or call_name.endswith(cs.JS_SUFFIX_APPLY)
        ):
            base_call = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return (cs.NodeLabel.FUNCTION, base_call)

        return None

    def cpp_operator_for_type(
        self, call_name: str, operand_type_qn: str
    ) -> tuple[str, str] | None:
        # Operand-type-directed operator binding: the overload is either a
        # member of the operand's own class or a free overload in that
        # class's module (the beside-the-class convention). A typed operand
        # with NEITHER is a builtin operation (enum/int comparison) with no
        # first-party callee; nlohmann's `token == token_type::x` must
        # not rebind to an unrelated class's operator== and fan out to all
        # its overload variants.
        # _try_resolve_method covers both the direct member and one
        # INHERITED from a base (Derived : Base with Base::operator==).
        if member := self._try_resolve_method(operand_type_qn, call_name):
            return member
        # ADL: a free overload may live in ANY enclosing namespace of the
        # operand's type, not only its immediate parent scope.
        parts = operand_type_qn.split(cs.SEPARATOR_DOT)
        for depth in range(len(parts) - 1, 0, -1):
            scope = cs.SEPARATOR_DOT.join(parts[:depth])
            free_qn = f"{scope}{cs.SEPARATOR_DOT}{call_name}"
            if free_qn in self.function_registry:
                return (self.function_registry[free_qn], free_qn)
        return None

    def cpp_operand_class_qn(
        self,
        operand_name: str | None,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> str | None:
        # A bare-identifier operand with a locally inferred type resolves
        # to a REGISTERED first-party type qn, or nothing: only a known
        # type may direct or suppress the operator binding; anything
        # uninferable keeps the caller on the legacy best-candidate path.
        if not operand_name or not local_var_types:
            return None
        var_type = local_var_types.get(operand_name)
        if not var_type:
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        resolved = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
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

    def _infer_chained_object_type(
        self,
        object_expr: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        # Type of a chained receiver expression like `c.Root()` using the shared
        # method_return_types map: the base is a typed local (`c` -> Command), and
        # each `.method()` hop advances the type by that method's return type
        # (Root() -> Command). Language-agnostic; returns the bare type name of the
        # final hop, or None if any hop is untyped/unknown (then the chain stays
        # unresolved). No early-out on an empty method_return_types map: a
        # constructor-temporary base (`Reader<T>(...).m()`) types itself from the
        # class registry alone.
        parts = _split_receiver_chain(object_expr)
        base = parts[0]
        if not base:
            return None
        if cs.CHAR_PAREN_OPEN in base:
            # A Rust chain rooted in an associated-function call
            # (`Ping::new(msg).into_frame()`): type the base from the assoc fn's
            # recorded return type. A bare-identifier factory call
            # (`parser(ia, cb).parse()`, C++): type it from the factory's recorded
            # return type. Other paren bases stay unresolved.
            current_type = self._infer_rust_assoc_base_type(
                base, module_qn
            ) or self._infer_call_base_type(
                base, module_qn, local_var_types, class_context, caller_qn, language
            )
        elif local_var_types:
            current_type = local_var_types.get(base)
        else:
            return None
        for part in parts[1:]:
            if not current_type or cs.CHAR_PAREN_OPEN not in part:
                return None
            method = part.split(cs.CHAR_PAREN_OPEN, 1)[0]
            class_qn = self._chain_class_qn(current_type, module_qn)
            current_type = self.type_inference.method_return_types.get(
                f"{class_qn}{cs.SEPARATOR_DOT}{method}"
            )
        return current_type

    def _chain_class_qn(self, type_name: str, module_qn: str) -> str:
        # Resolve a bare type name from a chained-call hop to its class qn, honoring
        # imports (a Rust `use` target is a raw `::`-path, not a registry qn), so a
        # method-return-type lookup keyed by the class qn hits.
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        return (
            self._resolve_class_qn_from_type(type_name, import_map, module_qn)
            or type_name
        )

    def _infer_call_base_type(
        self,
        base: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        # `parser(ia, cb).parse()`: the receiver is a bare-identifier factory call.
        # Resolve the callee to its function/method qn and return its recorded
        # return type. When none resolves, a C++ callee may instead be a
        # CONSTRUCTOR TEMPORARY (`Reader<decltype(ia)>(...)`, nlohmann's
        # from_cbor): the callee names the receiver's class itself. The callee is
        # cut at `<` or `(`, whichever comes first, since template args can carry
        # their own parens.
        cut = len(base)
        for bracket in (cs.CHAR_ANGLE_OPEN, cs.CHAR_PAREN_OPEN):
            idx = base.find(bracket)
            if idx != -1 and idx < cut:
                cut = idx
        callee = base[:cut]
        if not callee:
            return None
        if cs.SEPARATOR_DOUBLE_COLON in callee:
            # A `::`-qualified non-C++ callee is the Rust assoc path's job,
            # handled by the caller; C++ namespace paths normalize to dots.
            if language != cs.SupportedLanguage.CPP:
                return None
            callee = callee.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        resolved = self.resolve_function_call(
            callee, module_qn, local_var_types, class_context, caller_qn, language
        )
        if resolved is not None:
            return_type = self.type_inference.method_return_types.get(resolved[1])
            if return_type:
                return self._resolve_type_to_class_qn(return_type, module_qn)
        if language in (cs.SupportedLanguage.CPP, cs.SupportedLanguage.DART):
            # Constructor temporary: `X(...)` resolved to a ctor (never a
            # recorded return) or to nothing at all; if X names a registered
            # class, that class IS the receiver type (C++ `Reader<T>(...)`,
            # Dart `_Usage(...).generate()`).
            return self._resolve_type_to_class_qn(callee, module_qn)
        return None

    def _resolve_type_to_class_qn(self, type_path: str, module_qn: str) -> str | None:
        # Resolve a recorded return-type path to a registered CLASS qn. A factory
        # return type names a class, but the plain class-name resolver can return a
        # same-named factory METHOD (nlohmann's basic_json has both a `parser` class
        # and a `parser()` factory), so filter to class-labeled nodes. Try the
        # import-aware resolver first (same-file bare types, imports), then a
        # class-only suffix match on the qualified path, then on the bare name.
        candidate = self._chain_class_qn(type_path, module_qn)
        if candidate and self.function_registry.get(candidate) == cs.NodeLabel.CLASS:
            return candidate
        matches = [
            qn
            for qn in self.function_registry.find_ending_with(type_path)
            if self.function_registry.get(qn) == cs.NodeLabel.CLASS
        ]
        if not matches and cs.SEPARATOR_DOT in type_path:
            simple = type_path.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            matches = [
                qn
                for qn in self.function_registry.find_ending_with(simple)
                if self.function_registry.get(qn) == cs.NodeLabel.CLASS
            ]
        if not matches:
            return None
        matches.sort(key=lambda qn: (len(qn), qn))
        return matches[0]

    def _infer_rust_assoc_base_type(self, base: str, module_qn: str) -> str | None:
        # `Ping::new(msg)` -> the return type recorded for `Ping::new` (Ping).
        # The callee is the text before the first paren; only a `::`-rooted
        # associated call (`Type::assoc`) is handled.
        callee = base.split(cs.CHAR_PAREN_OPEN, 1)[0]
        if cs.SEPARATOR_DOUBLE_COLON not in callee:
            return None
        segments = callee.split(cs.SEPARATOR_DOUBLE_COLON)
        if len(segments) < 2:
            return None
        # Keep the full path prefix (`crate::parse::Parse`) so a qualified
        # associated call resolves; only the trailing segment is the method.
        type_name = cs.SEPARATOR_DOUBLE_COLON.join(segments[:-1])
        method = segments[-1]
        class_qn = self._chain_class_qn(type_name, module_qn)
        return self.type_inference.method_return_types.get(
            f"{class_qn}{cs.SEPARATOR_DOT}{method}"
        )

    def _is_method_chain(self, call_name: str) -> bool:
        if cs.CHAR_PAREN_OPEN not in call_name or cs.CHAR_PAREN_CLOSE not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        method_calls = sum(
            cs.CHAR_PAREN_OPEN in part and cs.CHAR_PAREN_CLOSE in part for part in parts
        )
        return method_calls >= 1 and len(parts) >= 2

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        match = _CHAINED_METHOD_PATTERN.search(call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        object_type = (
            self.type_inference.python_type_inference._infer_expression_return_type(
                object_expr, module_qn, local_var_types
            )
            or self._infer_chained_object_type(
                object_expr,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
            )
        )
        if object_type:
            full_object_type = object_type
            if cs.SEPARATOR_DOT not in object_type:
                # Honor imports (Rust `use` targets are raw `::`-paths) so an
                # imported chained type (`Get::new(k).into_frame()`) resolves.
                if resolved_class := self._chain_class_qn(object_type, module_qn):
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_CHAINED,
                    call_name=call_name,
                    method_qn=method_qn,
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return self.function_registry[method_qn], method_qn

            if inherited_method := self._resolve_inherited_method(
                full_object_type, final_method
            ):
                logger.debug(
                    ls.CALL_CHAINED_INHERITED,
                    call_name=call_name,
                    method_qn=inherited_method[1],
                    obj_expr=object_expr,
                    obj_type=object_type,
                )
                return inherited_method

        # C/C++ only, and ONLY when the receiver type was never inferred: its return
        # type is unrecordable (`auto`/trailing/decltype, e.g. fmt's
        # get_container(out).append). Before chained typing existed the bare method
        # name resolved via the trie, so fall back to it here rather than dropping an
        # edge that used to land. When the type WAS inferred but lacks the method, we
        # must NOT rebind to an unrelated same-named method; drop instead. C shares
        # the field_expression call shape but has no method dispatch, so it always
        # lands here = its exact prior behaviour. Go/Rust deliberately drop.
        if not object_type and language in (
            cs.SupportedLanguage.CPP,
            cs.SupportedLanguage.C,
        ):
            return self._resolve_function_call(
                final_method,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
            )

        return None

    def _resolve_super_call(
        self, call_name: str, class_context: str | None = None
    ) -> tuple[str, str] | None:
        match call_name:
            case _ if call_name == cs.KEYWORD_SUPER:
                method_name = cs.KEYWORD_CONSTRUCTOR
            case _ if cs.SEPARATOR_DOT in call_name:
                method_name = call_name.split(cs.SEPARATOR_DOT, 1)[1]
            case _:
                return None

        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(ls.CALL_SUPER_NO_CONTEXT, call_name=call_name)
            return None

        if current_class_qn not in self.class_inheritance:
            logger.debug(ls.CALL_SUPER_NO_INHERITANCE, class_qn=current_class_qn)
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(ls.CALL_SUPER_NO_PARENTS, class_qn=current_class_qn)
            return None

        if result := self._resolve_inherited_method(current_class_qn, method_name):
            callee_type, parent_method_qn = result
            logger.debug(
                ls.CALL_SUPER_RESOLVED,
                call_name=call_name,
                method_qn=parent_method_qn,
            )
            return callee_type, parent_method_qn

        logger.debug(
            ls.CALL_SUPER_UNRESOLVED,
            call_name=call_name,
            class_qn=current_class_qn,
        )
        return None

    def _resolve_self_sibling_method(
        self, call_name: str, class_context: str
    ) -> tuple[str, str] | None:
        # self.method() in a mixin may call a method defined on a SIBLING mixin
        # (neither is the other's base); both are combined into a concrete class.
        # Resolve through the concrete subclasses' MRO and accept the target only
        # when it is unambiguous, so an unrelated same-named method cannot win.
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2 or parts[0] != cs.KEYWORD_SELF:
            return None
        method_name = parts[1]
        candidates: set[str] = set()
        for subclass_qn in self._concrete_subclasses(class_context):
            candidates |= self._mro_method_qns(subclass_qn, method_name)
        if not candidates:
            return None
        # An @abstractmethod stub never runs when a concrete sibling implements the
        # method, so prefer concrete candidates; resolve only when unambiguous.
        chosen = {
            qn for qn in candidates if not self.function_registry.is_abstract(qn)
        } or candidates
        if len(chosen) != 1:
            return None
        method_qn = next(iter(chosen))
        logger.debug(
            ls.CALL_INSTANCE_ATTR_INHERITED,
            call_name=call_name,
            method_qn=method_qn,
            attr_ref=cs.KEYWORD_SELF,
            var_type=class_context,
        )
        return self.function_registry[method_qn], method_qn

    def _mro_method_qns(self, class_qn: str, method_name: str) -> set[str]:
        results: set[str] = set()
        visited: set[str] = set()
        queue: deque[str] = deque([class_qn])
        while queue:
            current = self._follow_reexports(queue.popleft())
            if current in visited:
                continue
            visited.add(current)
            method_qn = f"{current}.{method_name}"
            if method_qn in self.function_registry:
                results.add(method_qn)
            queue.extend(self.class_inheritance.get(current, ()))
        return results

    def _subclass_map(self) -> dict[str, set[str]]:
        if self._subclass_map_cache is None:
            mapping: dict[str, set[str]] = defaultdict(set)
            for subclass_qn, bases in self.class_inheritance.items():
                for base in bases:
                    mapping[self._follow_reexports(base)].add(subclass_qn)
            self._subclass_map_cache = mapping
        return self._subclass_map_cache

    def _concrete_subclasses(self, class_qn: str) -> set[str]:
        subclass_map = self._subclass_map()
        found: set[str] = set()
        stack = list(subclass_map.get(class_qn, ()))
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(subclass_map.get(current, ()))
        return found

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        if class_qn not in self.class_inheritance:
            return None

        bfs_queue = deque(self.class_inheritance.get(class_qn, []))
        visited = set(bfs_queue)

        while bfs_queue:
            # Base classes are recorded by the name the subclass imported, which
            # may be a package re-export (class_ingest.ClassIngestMixin) rather than
            # the real definition (class_ingest.mixin.ClassIngestMixin); follow the
            # re-export so the inherited method qn matches the registry.
            parent_class_qn = self._follow_reexports(bfs_queue.popleft())
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            if parent_class_qn in self.class_inheritance:
                for grandparent_qn in self.class_inheritance[parent_class_qn]:
                    if grandparent_qn not in visited:
                        visited.add(grandparent_qn)
                        bfs_queue.append(grandparent_qn)

        return None

    def _calculate_import_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        caller_parts = caller_module_qn.split(cs.SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)

        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if candidate_qn.startswith(
            cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
        ):
            base_distance -= 1

        return base_distance

    def _import_distance_fast(
        self,
        candidate_qn: str,
        caller_parts: list[str],
        caller_len: int,
        caller_parent_prefix: str,
    ) -> int:
        if candidate_qn in _QN_SPLIT_CACHE:
            candidate_parts, candidate_len = _QN_SPLIT_CACHE[candidate_qn]
        else:
            candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)
            candidate_len = len(candidate_parts)
            _QN_SPLIT_CACHE[candidate_qn] = (candidate_parts, candidate_len)
        common_prefix = 0
        for i in range(min(caller_len, candidate_len)):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break
        base_distance = max(caller_len, candidate_len) - common_prefix
        if caller_parent_prefix and candidate_qn.startswith(caller_parent_prefix):
            base_distance -= 1
        return base_distance

    def _dealias_type(self, type_name: str) -> str:
        # Follow C++ typedef/using aliases (`typedef Mutex MutexAlias;`) to the
        # underlying class name so an alias'd receiver resolves like the class it
        # names. Bounded against an alias cycle; a no-op when the name is not an
        # alias (and always, for languages with no aliases collected).
        seen: set[str] = set()
        while type_name in self.type_aliases and type_name not in seen:
            seen.add(type_name)
            type_name = self.type_aliases[type_name]
        return type_name

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        # Call resolution runs in Pass 3, after every definition pass, so a
        # class qn missing from the registry can never be a real node;
        # require registration so an import-map module entry (a C++ header
        # stem shadowing its class name) cannot mask the real class.
        return resolve_class_name(
            self._dealias_type(class_name),
            module_qn,
            self.import_processor,
            self.function_registry,
            require_registered=True,
        )

    def resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = self._redirect_protocol_method(
            java_engine.resolve_java_method_call(
                call_node, local_var_types, module_qn, caller_qn
            )
        )

        if result:
            call_text = (
                call_node.text.decode(cs.ENCODING_UTF8)
                if call_node.text
                else cs.TEXT_UNKNOWN
            )
            logger.debug(
                ls.CALL_JAVA_RESOLVED, call_text=call_text, method_qn=result[1]
            )

        return result

    def resolve_csharp_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        return self.type_inference.csharp_type_inference.resolve_csharp_method_call(
            call_node, local_var_types, module_qn, caller_qn
        )
