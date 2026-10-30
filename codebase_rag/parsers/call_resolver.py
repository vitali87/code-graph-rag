from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Iterable
from functools import lru_cache
from pathlib import PurePath
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import get_language_for_extension
from ..types_defs import FunctionRegistryTrieProtocol
from ..utils import qn_markers
from .call_site import CallSite, split_receiver_chain, trie_search_name
from .cpp.resolution import CppResolver
from .definition_facts import DefinitionFacts
from .go import utils as go_utils
from .go.link_state import GoLinkState
from .go.resolution import GoResolver
from .import_processor import ImportProcessor
from .java import resolution as java_resolution
from .js_ts.resolution import JsTsResolver, bind_receiver, class_value_of
from .js_ts.resolution import resolve_builtin_call as resolve_js_builtin_call
from .lua import utils as lua_utils
from .php import class_names as php_class_names
from .php.class_names import ClassPath as PhpClassPath
from .php.class_names import ascii_fold as _php_fold
from .python import resolution as python_resolution
from .python import resolve_class_name
from .rust.resolution import RustResolver, rust_caller
from .type_inference import TypeInferenceEngine

_CHAINED_METHOD_PATTERN = re.compile(r"\.([^.()]+)$")
# What a Python call site's semantic answer is when Jedi proves the callee
# leaves the repo; the call processor tests its answers against it.
PY_EXTERNAL_TARGET = python_resolution.PY_EXTERNAL_TARGET
# Registry kinds a CONSTRUCTION hop may name. Same members as the Rust
# receiver set in `parsers/rust/resolution.py`, kept separate because that one
# is about Rust receiver types while this gates the import-prefix fold for
# every language (issue #2033).
_CONSTRUCTIBLE_NODE_TYPES = frozenset(
    {cs.NodeLabel.CLASS, cs.NodeLabel.ENUM, cs.NodeLabel.TYPE, cs.NodeLabel.INTERFACE}
)
# A definition nested inside one of these is scoped to that body, so the
# simple-name fallback prefers candidates that are not (issue #945).
_SCOPING_PARENT_TYPES = frozenset({cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD})


class Resolved(tuple[str, str]):
    """A callee as `(label, qn)`, with how sure the binding is.

    Unpacks and compares as the pair, so a caller that needs only the callee
    reads it as one; `resolution` is the edge label the binding earns
    (issue #1526).
    """

    resolution: str

    def __new__(cls, label: str, qn: str, resolution: str) -> Resolved:
        resolved = super().__new__(cls, (label, qn))
        resolved.resolution = resolution
        return resolved


class _PhpCasedIndex(NamedTuple):
    """PHP definitions declared with capitals, by ASCII-folded name."""

    # (owner qn, folded method name) -> method qn
    methods: dict[tuple[str, str], str]
    # folded class name -> class qns
    classes: dict[str, list[str]]


@lru_cache(maxsize=cs.RESOLVER_NAME_CACHE_SIZE)
def _qn_parts(qualified_name: str) -> tuple[str, ...]:
    return tuple(qualified_name.split(cs.SEPARATOR_DOT))


def _split_csharp_qualified_path(path: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    generic_depth = 0
    for char in path:
        if char == cs.CHAR_ANGLE_OPEN:
            generic_depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE and generic_depth:
            generic_depth -= 1
        if char == cs.SEPARATOR_DOT and generic_depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _file_language(suffix: str) -> cs.SupportedLanguage | None:
    # `.ipynb` maps to no grammar, but the only notebooks that become modules
    # are Python ones, whose cells are parsed as Python (issue #2480).
    if suffix == cs.EXT_IPYNB:
        return cs.SupportedLanguage.PYTHON
    return get_language_for_extension(suffix)


class CallResolver:
    __slots__ = (
        "python_name_typed_params",
        "python_shadowed_imports",
        "go_local_scopes",
        "python_local_names",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
        "_interface_impl_cache",
        "_simple_resolution_cache",
        "_verdict",
        "_resolution_labels",
        "_wildcard_cache",
        "_field_bindings",
        "_field_to_classes",
        "_subclass_map_cache",
        "_ctor_params",
        "_ctor_param_attrs",
        "_pending_field_bindings",
        "_module_language_cache",
        "_php_class_namespaces",
        "_php_cased_index",
        "_rust",
        "_js_ts",
        "_go",
        "_cpp",
        "_python",
    )

    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
        go_links: GoLinkState | None = None,
    ) -> None:
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.facts = facts
        # caller qn -> untyped parameters typed only by their name (#2608);
        # filled alongside python_shadowed_imports.
        self.python_name_typed_params: dict[str, frozenset[str]] = {}
        # caller qn -> import-map names that caller binds as locals (#1907);
        # filled by the call processor before the caller's calls resolve.
        self.python_shadowed_imports: dict[str, frozenset[str]] = {}
        # Go caller qn -> the names it binds for itself (params, locals,
        # closures) and the byte spans each is in scope over, filled by the
        # call processor before the caller's calls resolve: a bare call to
        # one of them inside its span calls that value (#2571).
        self.go_local_scopes: dict[str, go_utils.GoLocalScopes] = {}
        # caller qn -> every name that Python function binds itself (#2666);
        # filled alongside python_shadowed_imports.
        self.python_local_names: dict[str, frozenset[str]] = {}
        self._interface_impl_cache: dict[str, str] | None = None
        self._simple_resolution_cache: dict[
            tuple[str, str, bool], tuple[str, str] | None
        ] = {}
        # How sure the answer being built is (issue #1526): the branch that
        # binds a callee marks it, and the entry point hands it back with the
        # answer. Memoised beside the cache so a hit reports the same
        # confidence as the miss.
        self._verdict: str = cs.EdgeResolution.EXACT
        self._resolution_labels: dict[tuple[str, str, bool], str] = {}
        self._wildcard_cache: dict[int, list[tuple[str, str]]] = {}
        self._field_bindings: dict[tuple[str, str], set[str]] = {}
        self._field_to_classes: dict[str, set[str]] = {}
        self._subclass_map_cache: dict[str, set[str]] | None = None
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
        # {PHP module qn: {class qn: folded namespace it is declared in}},
        # read off each file's declarations on first use (issue #2466).
        self._php_class_namespaces: dict[str, dict[str, PhpClassPath]] = {}
        self._php_cased_index: _PhpCasedIndex | None = None
        # A language's share of the resolution reads the same registry, maps
        # and facts, and calls back here for what every language shares.
        self._rust = RustResolver(
            self, function_registry, import_processor, type_inference, facts
        )
        self._js_ts = JsTsResolver(
            self, function_registry, import_processor, type_inference
        )
        self._go = GoResolver(
            self,
            function_registry,
            import_processor,
            type_inference,
            facts,
            go_links if go_links is not None else GoLinkState(),
            self.go_local_scopes,
        )
        self._cpp = CppResolver(
            self, function_registry, import_processor, type_inference, facts
        )
        self._python = python_resolution.PythonResolver(
            self,
            function_registry,
            import_processor,
            type_inference,
            facts,
            self.python_name_typed_params,
            self.python_shadowed_imports,
            self.python_local_names,
        )

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
            queue.extend(self.facts.class_inheritance.get(cur, []))
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
            return self._rust.resolve_rust_class_qn(var_type, module_qn)
        if cs.SEPARATOR_DOT in var_type:
            return self._follow_reexports(var_type)
        if var_type in import_map:
            # A Rust import target is a raw `::`-path (`crate::parse::Parse`) that
            # is not a registry qn; resolve it to the real type node so both
            # local-type dispatch and the external-receiver guard see it as
            # first-party (else its trie fallback is wrongly suppressed).
            target = import_map[var_type]
            if cs.SEPARATOR_DOUBLE_COLON in target:
                return self._rust.resolve_rust_class_qn(target, module_qn)
            return self._follow_reexports(target)
        if (
            scoped := self._rust.rust_glob_imported_type(var_type, module_qn)
        ) is not None:
            return scoped
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
        if class_qn in self.function_registry:
            return class_qn
        # A JS/TS barrel passes on only what it exports: a type it imports
        # for its own use must not type a receiver through it (#2560).
        followed = self.import_processor.follow_module_reexports(
            class_qn,
            self.type_inference.module_qn_to_file_path,
            self.function_registry,
        )
        if followed in self.function_registry:
            return followed
        return self._rust.rust_glob_reexported_type(followed) or followed

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None:
        # A class-body alias of the name (issue #2620) lists every method
        # the member may hold, its own `def` included when that survives.
        if alias := self._member_alias(class_qn, method_name):
            return alias
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

    def reset_resolution_caches(self) -> None:
        # The watch pass deletes EVERY CALLS edge and recomputes them
        # against re-parsed state; answers cached from the previous pass
        # (and wildcard entries keyed by dict id, which recycles) must
        # not survive into the recompute. The derived-structure memos
        # reset with them: a second trait implementer created mid-watch
        # must retire the sole-implementer companion edge, exactly as a
        # fresh full run would.
        self._simple_resolution_cache.clear()
        self._wildcard_cache.clear()
        self._interface_impl_cache = None
        self._subclass_map_cache = None
        self._cpp.reset_resolution_memos()
        self._python.reset_resolution_memos()
        # The qn -> language memo goes too: after a same-stem replacement
        # across languages (`util.rs` deleted, `util.py` added) the bare qn
        # `proj.util` now names a Python module, and a stale RUST answer
        # makes `_languages_can_call` drop the candidate a clean index
        # resolves (issue #1575).
        self._module_language_cache.clear()
        # A re-parsed PHP file may have moved a class between namespaces.
        self._php_class_namespaces.clear()
        self._php_cased_index = None
        self._js_ts.reset_resolution_memos()
        # The C# chained-receiver memo (issue #1800) is a resolution cache
        # like the rest and expires with them: it is keyed by byte span, and a
        # re-parsed file's spans name different code. Reached through the
        # PRIVATE attribute so a project with no C# does not build an engine
        # just to clear an empty dict.
        if (csharp := self.type_inference._csharp_type_inference) is not None:
            csharp._call_memo.clear()
        # The Java class headers (issue #2470) were read off the trees the
        # last pass saw; a re-parsed superclass must be walked again.
        if (java := self.type_inference._java_type_inference) is not None:
            java.reset_resolution_memos()

    def resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
        constructing: bool = False,
    ) -> Resolved | None:
        """`constructing`: the call is a Java/C# `new X(...)`, so `call_name`
        names a TYPE and the simple-name fallback must not offer a method or
        function that merely shares the name."""
        self._verdict = cs.EdgeResolution.EXACT
        result = self._reject_class_via_value_receiver(
            self._python.redirect_protocol_method(
                self._resolve_function_call(
                    call_name,
                    module_qn,
                    local_var_types,
                    class_context,
                    caller_qn,
                    language,
                    call_point,
                    constructing,
                )
            ),
            call_name,
            module_qn,
            class_context,
            local_var_types,
            language,
        )
        if result is not None and self._rust.rust_lands_outside_crate(
            call_name, module_qn, result[1], language
        ):
            return None
        if result is not None and self._python.receiver_typed_by_name(
            call_name, caller_qn
        ):
            # The receiver's class is only what its parameter's name spells
            # (`payload` -> `Payload`): a guess, never an exact binding (#2608).
            self._verdict = cs.EdgeResolution.HEURISTIC
        return None if result is None else Resolved(*result, self._verdict)

    def _reject_class_via_value_receiver(
        self,
        result: tuple[str, str] | None,
        call_name: str,
        module_qn: str,
        class_context: str | None = None,
        local_var_types: dict[str, str] | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        """Drop a CLASS answer for `value.Name()` -- a value never constructs.

        Two independent paths reach a class by discarding the receiver and
        looking the trailing name up on its own: `_try_resolve_module_method`
        (same module) and `_try_resolve_via_trie` (cross module). Both then look
        like construction to the emit site, which records INSTANTIATES for any
        callee that resolved to a Class.

        So `err.Error()` on a stdlib error became "constructs the first-party
        `Error` struct", and `buf.String()` on a `bytes.Buffer` became
        "constructs `render.text.String`". Go is worst hit because `Error()` and
        `String()` are its two most idiomatic interface methods, but Python
        reproduces it identically (issue #1641).

        The receiver decides. `Outer.Inner()` IS a genuine instantiation whose
        receiver is a class, and `pkg.Thing()` one whose receiver is a module,
        so this rejects only a receiver that resolves to neither -- an ordinary
        identifier holding a value. Guarding here rather than at either lookup
        keeps one rule for both paths; JS/TS and Dart already carry their own
        version of it further down (`_resolve_two_part_call`, the unique-member
        gate), which is why neither reproduced.
        """
        if result is None or result[0] != cs.NodeLabel.CLASS:
            return result
        # `::` and `:` are static paths (`Type::new`, `Class::method`), where a
        # type receiver is the normal spelling; only a `.` receiver can be a value.
        if cs.SEPARATOR_DOT not in call_name:
            return result
        receiver = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
        # `::` is a static path (`Type::new`), where a type receiver is the
        # normal spelling. A BARE `:` is not: in Python it is a slice or a dict
        # key inside the receiver expression (`xs[1:2].Error()`), and exempting
        # on it let the defect straight through.
        if cs.SEPARATOR_DOUBLE_COLON in receiver:
            return result
        if (
            language == cs.SupportedLanguage.CSHARP
            and self._try_resolve_csharp_qualified_call(
                call_name, module_qn, local_var_types
            )
        ):
            return result
        if self._receiver_names_a_type_or_module(
            receiver, module_qn, result[1], class_context, local_var_types
        ):
            return result
        # The member lookup that produced `result` searches by bare name and
        # can land on a same-named class nested in ANOTHER type (`Other.Inner`
        # for `Outer.Inner()`). Rejecting that answer must not lose the
        # construction the call spells out, so resolve the member under the
        # receiver class itself, walking its bases (#1759).
        member = call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if redirected := self._nested_class_under_receiver(receiver, module_qn, member):
            return cs.NodeLabel.CLASS, redirected
        logger.debug(ls.CALL_UNRESOLVED, call_name=call_name)
        return None

    def _nested_class_under_receiver(
        self, receiver: str, module_qn: str, member: str
    ) -> str | None:
        """`<receiver class>.<member>` when the receiver names a class that
        declares (or inherits) a nested class called `member`."""
        head, _, rest = receiver.partition(cs.SEPARATOR_DOT)
        if not head or not head.isidentifier():
            return None
        base = self._receiver_base_qn(head, module_qn)
        if base is None:
            return None
        owner = base if not rest else f"{base}{cs.SEPARATOR_DOT}{rest}"
        if self.function_registry.get(owner) != cs.NodeLabel.CLASS:
            return None
        for ancestor in self._mro(owner):
            candidate = f"{ancestor}{cs.SEPARATOR_DOT}{member}"
            if self.function_registry.get(candidate) == cs.NodeLabel.CLASS:
                return candidate
        return None

    def _receiver_names_a_type_or_module(
        self,
        receiver: str,
        module_qn: str,
        # Required rather than defaulted: an omitted resolved qn would make the
        # nesting check answer False and silently reject every `self.Inner()`
        # and typed-local construction, with no error to notice.
        resolved_qn: str,
        class_context: str | None = None,
        local_var_types: dict[str, str] | None = None,
    ) -> bool:
        """Whether a receiver expression names something constructible-through.

        A class (nested-class construction), or a module/package the caller
        imported. Anything else -- a parameter, a field, a chained call -- holds
        a VALUE at runtime.

        `self`/`cls`/`this` and a local typed to a class are the awkward cases:
        the receiver is spelled as a value but `self.Inner()` and `o.Inner()`
        really do construct a nested class, and the receiver-aware paths upstream
        resolve them correctly. Dropping them would delete edges `origin/main`
        gets right, so both are consulted here rather than judged by name.
        """
        head, _, rest = receiver.partition(cs.SEPARATOR_DOT)
        if not head or not head.isidentifier():
            return False
        # Single-segment only: `self.err.Error()` reaches a FIELD, an ordinary
        # value, so a chain falls through to the namespace resolution below.
        if not rest and self._receiver_owns_nested_class(
            head, module_qn, resolved_qn, class_context, local_var_types
        ):
            return True
        base = self._receiver_base_qn(head, module_qn)
        if base is None:
            return False
        # Resolve the WHOLE receiver, not just its head. `mod_a.instance` has a
        # module for a head and a value for a tail, and judging it by the head
        # alone accepted `mod_a.instance.Error()` -- the original defect reached
        # through one more segment. Being imported also says nothing about KIND:
        # `from m import instance` and `import m` both land in the map, and only
        # the second is a namespace.
        full = base if not rest else f"{base}{cs.SEPARATOR_DOT}{rest}"
        # A CLASS receiver constructs only its own nested classes: the member
        # lookup that produced `resolved_qn` falls back to a search by the
        # bare member name, which can land on a same-named class nested in
        # another type, and an alias receiver has no precise member path at
        # all (#1759). A module receiver keeps the namespace
        # check.
        if self.function_registry.get(full) == cs.NodeLabel.CLASS:
            return self._class_is_nested_in(resolved_qn, full)
        return self._import_target_is_a_namespace(full)

    def _receiver_owns_nested_class(
        self,
        head: str,
        module_qn: str,
        resolved_qn: str,
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        """Whether `head` names a type whose OWN nested class is being built.

        `self`/`cls`/`this` and a local typed to a class are spelled as values
        but really do construct: `self.Inner()` is a nested-class construction,
        while `self.Error()` naming a module-level `Error` is the #1641 defect
        wearing a new receiver. So each admits only a class nested in its type.
        """
        if head in cs.SELF_RECEIVER_KEYWORDS and class_context:
            if self._class_is_nested_in(resolved_qn, class_context):
                return True
        if not local_var_types:
            return False
        var_type = local_var_types.get(head)
        if not var_type:
            return False
        typed_qn = self._resolve_class_name(self._strip_optional(var_type), module_qn)
        return bool(
            typed_qn
            and self.function_registry.get(typed_qn) == cs.NodeLabel.CLASS
            and self._class_is_nested_in(resolved_qn, typed_qn)
        )

    def _receiver_base_qn(self, head: str, module_qn: str) -> str | None:
        """The qn the receiver's first segment names, if it names a namespace.

        `_resolve_class_name` follows the import map without checking what it
        landed on, so it answers `mod_a.helper` for an imported FUNCTION named
        `helper`. Confirm the qn it returns really is a Class before trusting
        it; the name alone is not evidence of classness.
        """
        class_qn = self._resolve_class_name(head, module_qn)
        if class_qn and self.function_registry.get(class_qn) == cs.NodeLabel.CLASS:
            return class_qn
        import_map = self.import_processor.import_mapping.get(module_qn) or {}
        if (imported := import_map.get(head)) is not None:
            if self.function_registry.get(
                imported
            ) == cs.NodeLabel.CLASS or self._import_target_is_a_namespace(imported):
                return imported
            # An imported name that is neither a class nor a namespace may be
            # an alias defined in the module it was imported FROM
            # (`from mod_b import Alias` where mod_b says `Alias = Outer`);
            # look it up there (#1672).
            owner_qn, _, alias_name = imported.rpartition(cs.SEPARATOR_DOT)
            return self._class_bound_by_module_alias(alias_name, owner_qn)
        # A module-level `Alias = Outer` names the class it binds: it is an
        # assignment rather than an import, so it is in neither the class
        # lookup nor the import map, and `Alias.Inner()` lost the INSTANTIATES
        # edge `Outer.Inner()` keeps (issue #1672). Only a right-hand side
        # that itself resolves to a Class counts; `inst = make()` still holds
        # a value.
        return self._class_bound_by_module_alias(head, module_qn)

    def _class_bound_by_module_alias(self, name: str, module_qn: str) -> str | None:
        if not name or not module_qn:
            return None
        target = self._python.module_level_alias_target(name, module_qn)
        if target is None:
            return None
        alias_qn = self._resolve_class_name(target, module_qn)
        if alias_qn and self.function_registry.get(alias_qn) == cs.NodeLabel.CLASS:
            return alias_qn
        return None

    def _cached_module_root(self, module_qn: str) -> Node | None:
        file_path = self.type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None:
            return None
        entry = self.type_inference.ast_cache.load(file_path)
        # A guard, not a contract: resolver tests drive this with a stub cache
        # whose `load` answers something that is not a (root, language) pair.
        if not isinstance(entry, tuple) or len(entry) != 2:
            return None
        root_node = entry[0]
        return root_node if isinstance(root_node, Node) else None

    def _class_is_nested_in(self, class_qn: str, owner_qn: str) -> bool:
        """Whether `class_qn` is declared inside `owner_qn` or one of its bases.

        `self.Inner()` inside a `Derived(Base)` resolves to `Base.Inner`, which
        is a real construction, so a prefix test against the enclosing type
        alone rejects an edge that is correct. Walking the MRO makes this a
        question about the TYPE rather than about qualified-name spelling.

        The trailing separator is load-bearing: without it `Outer2.Inner` would
        match owner `Outer` and a sibling class would read as a nested one.
        """
        if not class_qn or not owner_qn:
            return False
        return any(
            class_qn.startswith(f"{ancestor}{cs.SEPARATOR_DOT}")
            for ancestor in self._mro(owner_qn)
        )

    def _import_target_is_a_namespace(self, target: str) -> bool:
        """Whether an imported name refers to something you construct THROUGH.

        A class (`from m import Outer` then `Outer.Inner()`) or a module
        (`import m` then `m.Thing()`). A function, a method or a module-level
        instance is a VALUE, and `value.Name()` is a method call however it
        arrived in scope.
        """
        kind = self.function_registry.get(target)
        if kind is not None:
            return kind == cs.NodeLabel.CLASS
        # Unregistered, so it is a module IFF the registry holds anything under
        # its qn. A module node itself is not in the function registry, but its
        # contents are; an imported instance has an empty subtree.
        #
        # The label check above must stay FIRST: `find_with_prefix` returns the
        # subtree INCLUDING the target, so an imported function reports one
        # descendant (itself) and a prefix-only test would call it a namespace.
        return bool(self.function_registry.find_with_prefix(target))

    def _resolve_enclosing_scope(
        self,
        call_name: str,
        caller_qn: str | None,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
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
            if hit := self._probe_scope(scope, call_name, language):
                return hit
            if cs.SEPARATOR_DOT not in scope:
                return None
            parent = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if parent == module_qn or parent not in self.function_registry:
                return None
            # A class is in the registry too, so the check above let the walk
            # step from a method into its class and answer the method's own
            # parameter `run` with the sibling method `run` (issue #2666).
            if self._local_stops_at_class(call_name, caller_qn, parent):
                return None
            scope = parent

    def _probe_scope(
        self, scope: str, call_name: str, language: cs.SupportedLanguage | None
    ) -> tuple[str, str] | None:
        # One step of the scope-chain walk: the name defined directly in
        # `scope`, then in its variant-stripped form, then `scope`'s own
        # body-scoped name.
        return (
            self._scope_candidate(scope, call_name, language)
            or self._dup_variant_scope_candidate(scope, call_name, language)
            or self._own_body_scoped_name(scope, call_name)
        )

    def _own_body_scoped_name(
        self, scope: str, call_name: str
    ) -> tuple[str, str] | None:
        # A JS/TS named function expression's name is in scope inside its own
        # body, the one place a bare call can reach it by that name (issue
        # #2402). Answered from the caller's scope chain, ahead of the
        # module-keyed cache, since callers elsewhere must not see it. The
        # natural qn is what the same-module probe used to return, so the
        # `@line` variants fan out exactly as they did.
        if not self.function_registry.is_body_scoped_name(scope):
            return None
        natural = qn_markers.natural_qn(scope)
        if natural.rsplit(cs.SEPARATOR_DOT, 1)[-1] != call_name:
            return None
        target = natural if natural in self.function_registry else scope
        return self.function_registry[target], target

    def _name_hidden_in_body(self, qualified_name: str) -> bool:
        # A JS/TS function expression's own name, which only its body can call
        # (issue #2402). A same-named definition beside it (an `@line`
        # variant) keeps the name bound, so the variant group stays nameable
        # and fans out as before.
        registry = self.function_registry
        if not registry.is_body_scoped_name(qualified_name):
            return False
        return all(
            registry.is_body_scoped_name(variant)
            for variant in registry.variants(qn_markers.natural_qn(qualified_name))
        )

    def _local_stops_at_class(self, call_name: str, caller_qn: str, scope: str) -> bool:
        return (
            call_name in self.python_local_names.get(caller_qn, frozenset())
            and self.function_registry[scope] == cs.NodeLabel.CLASS.value
        )

    def _scope_candidate(
        self, scope: str, call_name: str, language: cs.SupportedLanguage | None
    ) -> tuple[str, str] | None:
        candidate = f"{scope}{cs.SEPARATOR_DOT}{call_name}"
        if (
            candidate in self.function_registry
            and self._bare_call_allowed(language, candidate)
            and not self._name_hidden_in_body(candidate)
        ):
            return self.function_registry[candidate], candidate
        return None

    def _dup_variant_scope_candidate(
        self, scope: str, call_name: str, language: cs.SupportedLanguage | None
    ) -> tuple[str, str] | None:
        # A duplicate-variant caller (a def rebinding a name an earlier def
        # already holds registers as `command@168`) owns nested defs the
        # def pass registers under the NATURAL qn (`command.decorator`);
        # probe the variant-stripped scope too, or the call falls to the
        # module trie and mis-binds to a sibling's same-named nested.
        # The stripped form, not the presence of the marker character: a C#
        # verbatim identifier (`@event`) carries the character without a
        # marker, and re-probing the identical scope is wasted work that can
        # only re-bind what the caller already tried (issue #2017).
        natural_scope = qn_markers.natural_qn(scope)
        if natural_scope == scope:
            return None
        return self._scope_candidate(natural_scope, call_name, language)

    def _bare_call_allowed(
        self, language: cs.SupportedLanguage | None, qn: str
    ) -> bool:
        # A bare Rust path NEVER names a method: inherent methods are
        # reachable only via self./Self::/Type:: (rustc-verified; the bare
        # spelling calls the module item, issue #1011). Other languages
        # keep their scope-chain semantics, except that no bare name
        # reaches a JS/TS object literal's function value (issue #2435).
        if self._only_object_members(qn):
            return False
        return (
            language != cs.SupportedLanguage.RUST
            or self.function_registry[qn] != cs.NodeLabel.METHOD.value
        )

    def _only_object_members(self, qualified_name: str) -> bool:
        # A JS/TS object literal's function value is reached through its
        # object (`options.retry.delay()`), never by its key alone, although
        # its qn reads like a name the enclosing scope declares (issue
        # #2435). A real binding of the same name in its `@line` group keeps
        # the group nameable; `lexical_call_targets` then drops the members
        # from the call's fan-out.
        registry = self.function_registry
        if not registry.is_object_member(qualified_name):
            return False
        return all(
            registry.is_object_member(variant)
            for variant in registry.variants(qn_markers.natural_qn(qualified_name))
        )

    def lexical_call_targets(
        self,
        call_name: str,
        module_qn: str,
        targets: list[str],
        caller_qn: str | None = None,
    ) -> list[str]:
        """The members of a bare call's `@line` group its name can reach.

        `function delay` and `{delay: () => 0}` in one file share a group, and
        the bare name means the declaration, not the object's value (issue
        #2435). A name the module imports is left whole: the import may bind
        a module's exported object member, which it does reach by name.
        Inside a JS/TS named function expression's own body, its name is that
        expression and shadows every same-named declaration (issue #2402).
        """
        if len(targets) < 2 or cs.SEPARATOR_DOT in call_name:
            return targets
        registry = self.function_registry
        if self._calls_own_body_name(call_name, caller_qn, targets):
            # Same-named function expressions keep their spread (#2403).
            return [qn for qn in targets if registry.is_body_scoped_name(qn)]
        if call_name in self.import_processor.import_mapping.get(module_qn, {}):
            return targets
        bound = [qn for qn in targets if not registry.is_object_member(qn)]
        return bound or targets

    def _calls_own_body_name(
        self, call_name: str, caller_qn: str | None, targets: list[str]
    ) -> bool:
        # The caller is a body-scoped function expression in the call's
        # `@line` group, calling the group's name: its own name, which inside
        # its body binds to it alone, whatever else shares the group.
        if caller_qn is None or caller_qn not in targets:
            return False
        if not self.function_registry.is_body_scoped_name(caller_qn):
            return False
        natural = qn_markers.natural_qn(caller_qn)
        return natural.rsplit(cs.SEPARATOR_DOT, 1)[-1] == call_name

    def protocol_dispatch_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        return self._python.protocol_dispatch_targets(callee_qn)

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
        # `self.Inner()` where `Inner` is a NESTED CLASS is a construction, not a
        # virtual method call: the name resolves to `Outer.Inner`, which is a
        # Class node, and CALLS may only target a Function/Method/Enum/Type. The
        # construction already reaches the graph as INSTANTIATES plus a CALLS to
        # `__init__`, so a class here is a duplicate the schema rejects outright
        # (issue #1650).
        return {target for target in targets if target[0] != cs.NodeLabel.CLASS.value}

    def js_member_twin_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        return self._js_ts.js_member_twin_targets(callee_qn)

    def go_package_sibling_targets(self, callee_qn: str) -> set[tuple[str, str]]:
        return self._go.go_package_sibling_targets(callee_qn)

    def go_declaration_is_visible(self, declaration_qn: str, module_qn: str) -> bool:
        return self._go.go_declaration_is_visible(declaration_qn, module_qn)

    def java_constructor_targets(self, class_qn: str) -> set[tuple[str, str]]:
        return java_resolution.java_constructor_targets(
            self.function_registry, class_qn
        )

    def cpp_destructor_targets(self, class_qn: str) -> set[tuple[str, str]]:
        return self._cpp.cpp_destructor_targets(class_qn)

    def cpp_braced_return_class(self, caller_qn: str, module_qn: str) -> str | None:
        return self._cpp.cpp_braced_return_class(caller_qn, module_qn)

    def cpp_dispatch_targets(
        self,
        call_name: str,
        local_var_types: dict[str, str] | None,
        template_params: frozenset[str],
    ) -> set[tuple[str, str]]:
        return self._cpp.cpp_dispatch_targets(
            call_name, local_var_types, template_params
        )

    def _interface_impl_map(self) -> dict[str, str]:
        # Map an interface to its SOLE first-party implementer. A call typed to an
        # interface resolves to the interface's own method declaration (the static
        # callee); when the interface has exactly one implementer the concrete
        # method is the one that runs, so ALSO edge it (call-graph accuracy).
        # >1 implementer is ambiguous -> not mapped -> the call stays on the
        # interface method alone (no precision risk, recall preserved).
        if self._interface_impl_cache is None:
            implementers_of: dict[str, set[str]] = {}
            for source in (
                self.facts.interface_implementers,
                self.facts.rehydrated_interface_implementers,
            ):
                for interface_qn, implementers in source.items():
                    implementers_of.setdefault(interface_qn, set()).update(implementers)
            self._interface_impl_cache = {
                interface_qn: next(iter(implementers))
                for interface_qn, implementers in implementers_of.items()
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

    def interface_dispatch_targets(
        self, callee_qn: str
    ) -> dict[tuple[str, str], dict[str, bool]]:
        return self._go.interface_dispatch_targets(callee_qn)

    def go_declaration_satisfies(
        self, method_qn: str, interface_method_qn: str
    ) -> bool:
        return self._go.go_declaration_satisfies(method_qn, interface_method_qn)

    def go_declaration_read(self, method_qn: str) -> bool:
        return self._go.go_declaration_read(method_qn)

    def go_variant_callees(
        self, callee_qn: str, module_qn: str
    ) -> list[tuple[str, bool]] | None:
        return self._go.go_variant_callees(callee_qn, module_qn)

    def go_reaches_everywhere(
        self, module_qn: str, declarations: Iterable[str]
    ) -> bool:
        return self._go.go_reaches_everywhere(module_qn, declarations)

    def go_dispatch_builds(
        self, module_qn: str, interface_qn: str, implementer: str, declaration: str
    ) -> tuple[bool, bool]:
        return self._go.go_dispatch_builds(
            module_qn, interface_qn, implementer, declaration
        )

    def go_apart_from_caller(self, module_qn: str, declaration: str) -> bool:
        return self._go.go_apart_from_caller(module_qn, declaration)

    def go_caller_rel(self, module_qn: str) -> str:
        return self._go.go_caller_rel(module_qn)

    def go_declaring_rel(self, qn: str) -> str:
        return self._go.go_declaring_rel(qn)

    def _resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
        constructing: bool = False,
    ) -> tuple[str, str] | None:
        call = CallSite(
            call_name,
            module_qn,
            self._rust.rust_scoped_local_types(
                local_var_types, module_qn, caller_qn, language
            ),
            class_context,
            caller_qn,
            language,
            call_point,
            constructing,
        )
        handled, result = self._resolve_caller_scoped_stages(call)
        if handled:
            return result
        # After the scope walk, which answers a nested def or class of the
        # name: any other local is a value the graph cannot follow, and the
        # same-module, class, import and trie stages below would bind it to
        # the definition it shadows (issue #2666). Before the cache, whose
        # answers are caller-independent.
        if self._python.is_python_local_name(call):
            return None

        cache_key = self._resolution_cache_key(call)
        if cache_key is not None and cache_key in self._simple_resolution_cache:
            self._verdict = self._resolution_labels.get(
                cache_key, cs.EdgeResolution.EXACT
            )
            return self._simple_resolution_cache[cache_key]

        handled, result = self._resolve_structural_call(call)
        if handled:
            return result
        handled, result = self._rust.resolve_rust_prefixed_or_local(call, cache_key)
        if handled:
            return result
        handled, result = self._rust.resolve_rust_external_path(call, cache_key)
        if handled:
            return result
        handled, result = self._resolve_typed_receiver_or_csharp(call)
        if handled:
            return result
        # Ahead of the import probe: for a receiver it cannot place, that probe
        # binds `subprocess.run` to a same-module function named `run`.
        if self._python.receiver_is_external_python_module(call):
            self._remember_cacheable(cache_key, None)
            return None
        handled, result = self._resolve_imported_or_module_member(call, cache_key)
        if handled:
            return result
        handled, result = self._resolve_external_target(call, cache_key)
        if handled:
            return result
        handled, result = self._resolve_untyped_member_or_unresolvable(call, cache_key)
        if handled:
            return result
        handled, result = self._rust.resolve_rust_named_owner(call, cache_key)
        if handled:
            return result
        if self._python.untyped_receiver_guess_is_ambiguous(call):
            logger.debug(ls.CALL_AMBIGUOUS_UNTYPED_RECEIVER, call_name=call_name)
            self._remember_cacheable(cache_key, None)
            return None

        result = self._try_resolve_via_trie(
            call_name, module_qn, language, call_point, constructing
        )
        self._remember_cacheable(cache_key, result)
        return result

    def _resolve_caller_scoped_stages(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # The stages whose answers depend on the caller, in order, ahead of
        # the caller-independent resolution cache.
        for stage in (
            self._resolve_receiver_shadow,
            self._rust.resolve_rust_block_scope,
            self._resolve_caller_scope,
        ):
            handled, result = stage(call)
            if handled:
                return True, result
        return False, None

    def _resolve_receiver_shadow(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        handled, result = self._python.resolve_python_receiver_shadow(call)
        if handled:
            return True, result
        # Dart's counterpart of Python's untyped shadow: a local or parameter named like an import
        # prefix makes `p.Box(1)` a call on that value, so the import map
        # must not answer for it (issue #2088). The binding spans are
        # syntactic, so `var p = 1`, which inference leaves untyped, counts.
        # A shadow the type map holds resolves through its type as usual.
        if call.language == cs.SupportedLanguage.DART and self._dart_call_on_shadow(
            call.call_name, call.module_qn, call.local_var_types, call.call_point
        ):
            return True, None
        return False, None

    def _resolve_caller_scope(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # `self.run()` where a class body aliases `run` (issue #2620) is a
        # member of the enclosing class, which the module-keyed cache does
        # not know about.
        if call.language == cs.SupportedLanguage.PYTHON and call.class_context:
            handled, result = self._resolve_self_member_alias(
                call.call_name, call.class_context, call.local_var_types
            )
            if handled:
                return True, result

        # Enclosing-scope (nested def) lookup is caller-specific, so it must run
        # before the module-keyed cache/trie, which would otherwise return a sibling
        # scope's same-named nested function.
        # `new X(...)` names a type, so an enclosing scope answers it only
        # with a class: a static factory named like the class it constructs
        # (`Some.LogEventProperty()` building `new LogEventProperty()`) is the
        # enclosing member, and taking it bound nothing at all (issue #1997).
        if (
            result := self._resolve_enclosing_scope(
                call.call_name, call.caller_qn, call.module_qn, call.language
            )
        ) and (not call.constructing or result[0] == cs.NodeLabel.CLASS.value):
            return True, result

        # `this.m()` inside a prototype-assigned function dispatches to a sibling
        # method of the same prototype target (Date.prototype.strftime calling
        # this.getTwoDigitMonth()); the caller's parent scope names that target.
        # Caller-specific, so it too must run before the module-keyed cache; the
        # CommonJS module-receiver fallback still applies when no sibling exists.
        if result := self._js_ts.resolve_js_prototype_sibling(
            call.call_name, call.caller_qn, call.language
        ):
            return True, result
        return self._go.resolve_go_local_value(call)

    def _resolution_cache_key(self, call: CallSite) -> tuple[str, str, bool] | None:
        module_qn, caller_qn = call.module_qn, call.caller_qn
        # The cache is keyed by (call_name, module_qn) only, so a caller whose
        # class carries extends-clause type arguments must bypass it: its
        # member resolution is class-context-dependent (#875) and a sibling
        # class's cached answer for the same name would be wrong.
        use_cache = not call.local_var_types and not (
            call.language == cs.SupportedLanguage.DART
            and call.class_context in self.type_inference.facts.dart_extends_type_args
        )
        # A Rust caller whose inner scopes or initializer blocks bind its
        # names, and a Julia caller inside a nested `module` block (the
        # block's own `using` shadows the file-level bindings), resolve
        # caller-dependently: the file-keyed cache would serve them another
        # caller's answer.
        nested_caller = (
            caller_qn is not None
            and caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
            and cs.SEPARATOR_DOT in caller_qn[len(module_qn) + 1 :]
        )
        if self._rust.rust_caller_dependent(call) or (
            call.language == cs.SupportedLanguage.JULIA and nested_caller
        ):
            use_cache = False
        if not use_cache:
            return None
        # `new X()` and a bare `X()` in one module are different
        # questions with different answers, so they never share a slot.
        return (call.call_name, module_qn, call.constructing)

    def _resolve_structural_call(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn = call.call_name, call.module_qn
        if result := self._try_resolve_iife(call_name, module_qn):
            return True, result

        # A JS/TS chain off `super.m()` is typed hop by hop like one off
        # `this.m()`; the super lookup reads only a single member name.
        if self._is_super_call(call_name) and not (
            call.language in cs.JS_TS_LANGUAGES
            and self._is_method_chain(call_name, call.language)
        ):
            return True, self._resolve_super_call(call_name, call.class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(
            call_name, call.language
        ):
            # A chained call resolves via return-type inference only; it does NOT
            # fall through to the trie fallback, because a hop returning a container
            # (`Kids() []Command`) or an unknown type must drop the edge rather than
            # rebind the final method by bare name (a false `Command.Run`). C++ is the
            # exception: a `foo().bar()` receiver with an unrecordable return type
            # (`auto`/trailing/decltype, e.g. fmt's get_container(out).append) fell to
            # the bare-method trie before chained typing existed, so preserve that
            # fallback for C++ to avoid dropping edges the typing can't yet recover.
            return True, self._resolve_chained_call(
                call_name,
                module_qn,
                call.local_var_types,
                call.class_context,
                call.caller_qn,
                call.language,
                call.call_point,
            )

        return self._rust.resolve_rust_inline_scope(call)

    def _resolve_typed_receiver_or_csharp(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn = call.call_name, call.module_qn
        # A member call whose receiver has a known FIRST-PARTY class type that
        # defines no such method (own or inherited) is a known non-edge, not an
        # unknown one. The chained and inline receiver paths already drop this
        # shape; the plain-variable path let it reach the probes below, where
        # the same-module function fallback inside the import probe, or the
        # bare-name trie at the end, bound `p.go()` on a `Product` without `go`
        # to an unrelated `go` (issue #1897). Ahead of the import probe for that
        # reason; never cached, because it can only fire with a non-empty local
        # type map and `use_cache` is already False then.
        if self._python.typed_receiver_lacks_method(
            call_name, module_qn, call.local_var_types, call.language
        ):
            return True, None

        if call.language == cs.SupportedLanguage.CSHARP and (
            cs.SEPARATOR_DOUBLE_COLON in call_name or cs.SEPARATOR_DOT in call_name
        ):
            result, decided = self._resolve_csharp_qualified_call(
                call_name, module_qn, call.local_var_types
            )
            if result is not None:
                return True, result
            if decided or cs.SEPARATOR_DOUBLE_COLON in call_name:
                return True, None
        # `new W(1)` under `using W = Zeta.Widget;` names no registered type
        # by its written name, so the trie has no `W` to offer; the type
        # lookup expands the alias to its target (issue #2002).
        if (
            call.language == cs.SupportedLanguage.CSHARP
            and call.constructing
            and (
                type_qn := self.type_inference.csharp_type_inference._type_name_to_qn(
                    call_name, module_qn
                )
            )
        ):
            return True, (self.function_registry[type_qn], type_qn)
        return False, None

    def _resolve_imported_or_module_member(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn = call.call_name, call.module_qn
        if result := self._try_resolve_via_imports(
            call_name, module_qn, call.local_var_types, call.language, call.caller_qn
        ):
            self._remember_cacheable(cache_key, result)
            return True, result

        if result := self._try_resolve_same_module(
            call_name, module_qn, call.call_point
        ):
            self._remember_cacheable(cache_key, result)
            return True, result

        handled, result = self._go.resolve_go_same_package(call, cache_key)
        if handled:
            return True, result

        # The same probe for a Lua table member spelled with the other
        # separator than its definition (`Account.deposit` for
        # `function Account:deposit`), in a file the import probe skipped.
        if call.language == cs.SupportedLanguage.LUA and (
            result := self._try_resolve_lua_same_module_member(call_name, module_qn)
        ):
            self._remember_cacheable(cache_key, result)
            return True, result

        handled, result = self._rust.resolve_rust_module_qualified(call, cache_key)
        if handled:
            return True, result

        if call.class_context and (
            result := self._resolve_self_sibling_method(call_name, call.class_context)
        ):
            return True, result
        return False, None

    def _resolve_external_target(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn = call.call_name, call.module_qn
        # A bare name explicitly imported from outside the project binds to that
        # external symbol. Since precise import / same-module resolution above
        # already failed, the symbol is unindexed; do NOT let the simple-name
        # trie fallback rebind it to an unrelated first-party symbol of the same
        # name. (The instantiation eval caught `from evals import GraphData;
        # GraphData()` being resolved to codebase_rag's own GraphData class.)
        if cs.SEPARATOR_DOT not in call_name and self._is_external_import(
            call_name, module_qn, call.language
        ):
            self._remember_cacheable(cache_key, None)
            return True, None

        # A dotted call on an import whose target still holds a raw
        # slash-separated module path (github.com/x/y) is a call into an
        # EXTERNAL module: local Go paths were rewritten to project qns at
        # import time, so a surviving slash path is definitionally outside
        # the repo. The symbol is unindexed; do not let the last-segment trie
        # fallback rebind it to an unrelated first-party function.
        if self._is_external_path_import(call_name, module_qn):
            self._remember_cacheable(cache_key, None)
            return True, None

        # A member call `obj.method` whose receiver has a KNOWN inferred type that is
        # not a first-party class is a call on an external object (e.g. a
        # `std::string`). Precise local-type resolution above already failed, so the
        # method lives on the external type; do NOT let the simple-name trie fallback
        # rebind it to an unrelated first-party method of the same name. Untyped
        # receivers keep the fallback (their type is unknown, not known-external).
        if self._receiver_type_is_external(call_name, module_qn, call.local_var_types):
            self._remember_cacheable(cache_key, None)
            return True, None
        return False, None

    def _resolve_untyped_member_or_unresolvable(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn, language = call.call_name, call.module_qn, call.language
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
                    call_name, call.class_context, call.local_var_types
                )
            ):
                return True, result
            result = self._js_ts.resolve_js_member_call_unique(call_name, module_qn)
            if result is not None and language in cs.JS_TS_LANGUAGES:
                # Chosen because no other visible class defines the name, not
                # because anything says the receiver is that class (issue
                # #2609). Only the call site's AST can say that, so the call
                # processor confirms or drops the pick from the receiver's
                # declaration; this caller-independent verdict is what caches.
                self._verdict = cs.EdgeResolution.HEURISTIC
            self._remember_cacheable(cache_key, result)
            return True, result
        return self._rust.resolve_rust_unresolvable_import(call, cache_key)

    def _remember_cacheable(
        self, cache_key: tuple[str, str, bool] | None, result: tuple[str, str] | None
    ) -> None:
        if cache_key is not None:
            self._remember(cache_key, result)

    def _remember(
        self, cache_key: tuple[str, str, bool], result: tuple[str, str] | None
    ) -> None:
        self._simple_resolution_cache[cache_key] = result
        self._resolution_labels[cache_key] = self._verdict

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
        type_args = self.type_inference.facts.dart_extends_type_args.get(class_context)
        if not type_args:
            return None
        receiver, sep, member = call_name.partition(cs.SEPARATOR_DOT)
        if not sep or cs.SEPARATOR_DOT in member:
            return None
        if local_var_types and receiver in local_var_types:
            return None
        if self._dart_registered_member_named(class_context, receiver):
            return None
        bases = self.facts.class_inheritance.get(class_context)
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
            stack.extend(self.facts.class_inheritance.get(current, ()))
        return False

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
        bare_imports = self.import_processor.js_ts.js_ts_bare_imports.get(module_qn)
        return not (bare_imports and object_name in bare_imports)

    def _is_external_import(
        self,
        call_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> bool:
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
        # NARROWED (#1185 stage 1): the `namespace` declaration IS now tracked,
        # in `import_processor.php.php_module_namespaces`, and
        # `_php_target_for_namespace_import` uses it to bind a `use function`
        # import to the module declaring that namespace. So a first-party
        # target whose namespace is declared somewhere in the repo resolves
        # rather than deferring.
        #
        # What remains: a target whose namespace NO indexed module declares is
        # still indistinguishable here from a first-party one the resolver
        # could not place, and both defer to the trie exactly as a bare
        # `helper()` call does. That is a smaller gap than the original
        # comment described, and deferring stays the right behaviour for it --
        # treating it as external would suppress the trie fallback and drop
        # the call.
        php_imports = self.import_processor.php.php_function_imports.get(module_qn)
        if php_imports and call_name in php_imports:
            return False
        # A JS/TS import with a non-standard scheme (deno `ext:deno_node/x`) does
        # not resolve to a file-path module qn, so its target is unregistered and
        # looks external even though it aliases first-party code. Defer to the
        # simple-name trie (like a relative import that misses) instead of
        # suppressing. Ordinary package specifiers (bare, scoped, node:/npm:) are
        # NOT recorded here, so genuine external calls stay suppressed.
        bare_imports = self.import_processor.js_ts.js_ts_bare_imports.get(module_qn)
        if bare_imports and call_name in bare_imports:
            return False
        # Only dotted absolute-path imports (Python/Java `pkg.mod.Name`) are
        # judged here. Rust/C++ record relative or `::`-separated targets
        # (`super::b::helper`) that never carry the project prefix and rely on
        # the trie fallback to resolve, so they must not be mistaken external.
        # A JS/TS target is judged dot-free too: every relative specifier was
        # rewritten to a project qn at import time, so a bare package name
        # left as written (`require('http-errors')`) is external (#2402).
        if cs.SEPARATOR_DOUBLE_COLON in target or (
            cs.SEPARATOR_DOT not in target and language not in cs.JS_TS_LANGUAGES
        ):
            return False
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if target.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return f"{project_root}{cs.SEPARATOR_DOT}{target}" not in self.function_registry

    def js_member_head_is_external(self, member_name: str, module_qn: str) -> bool:
        return self._js_ts.js_member_head_is_external(member_name, module_qn)

    def _two_part_receiver_type(
        self, call_name: str, local_var_types: dict[str, str] | None
    ) -> str | None:
        """The inferred local type of `obj` in a two-part `obj.method` call, or None."""
        if not local_var_types or cs.SEPARATOR_DOT not in call_name:
            return None
        parts = call_name.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return None
        return local_var_types.get(parts[0])

    def _registered_class_qn(self, class_qn: str, module_qn: str) -> str | None:
        """The registry's spelling of a first-party class qn, or None if unindexed.

        A first-party qn may be written without the project prefix (a bare
        `from models.user import User` gives `models.user.User` while the
        registry stores `proj.models.user.User`), so both spellings are tried.
        """
        if class_qn in self.function_registry:
            return class_qn
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        prefixed = f"{project_root}{cs.SEPARATOR_DOT}{class_qn}"
        if prefixed in self.function_registry:
            return prefixed
        return None

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
        var_type = self._two_part_receiver_type(call_name, local_var_types)
        if var_type is None:
            return False
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        class_qn = self._resolve_class_qn_from_type(var_type, import_map, module_qn)
        if not class_qn:
            return True
        # A project-rooted qn is always treated as first-party; otherwise the
        # registry decides, under either spelling (see `_registered_class_qn`).
        project_root = module_qn.split(cs.SEPARATOR_DOT, 1)[0]
        if class_qn.split(cs.SEPARATOR_DOT, 1)[0] == project_root:
            return False
        return self._registered_class_qn(class_qn, module_qn) is None

    def _try_resolve_iife(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name:
            return None
        if not (
            call_name.startswith(cs.IIFE_FUNC_PREFIX)
            or call_name.startswith(cs.IIFE_ARROW_PREFIX)
            or call_name.startswith(cs.PREFIX_IIFE_DIRECT)
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
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        if language == cs.SupportedLanguage.JULIA and caller_qn:
            # A `using` inside a Julia `module` block binds only that block,
            # so a nested module's imports shadow the file-level ones: walk
            # the caller's module scopes innermost first.
            for scope in self._julia_caller_scopes(caller_qn, module_qn):
                import_map = self.import_processor.import_mapping.get(scope)
                if not import_map:
                    continue
                if result := self._try_resolve_direct_import(
                    call_name, import_map, language, scope
                ):
                    return result
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

        if result := self._try_resolve_direct_import(
            call_name, import_map, language, module_qn
        ):
            return result

        if result := self._js_ts.try_resolve_js_reexport(
            call_name, import_map, language, module_qn
        ):
            return result

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types, language
        ):
            return result

        return self._try_resolve_wildcard_imports(call_name, import_map)

    def _php_target_for_namespace_import(
        self, imported_path: str, kind: cs.NodeLabel | None = None
    ) -> str | None:
        """Map a PHP `use function A\\B\\c` target onto the qn that registers it.

        CGR qualifies PHP by file path, so the import target `App.Text.format`
        never equals the registered `project.text.format`. Previously that miss
        dropped through to the simple-name trie, which binds to whichever
        same-named function it reaches first -- so `use function App\\Text\\format`
        could resolve to `App\\Money\\format` (issue #1185). A wrong edge, not a
        missing one.

        Splits the target into the namespace it names and the symbol within it,
        then finds the module that DECLARED that namespace and looks the symbol
        up there. PSR-4 maps a namespace prefix to a directory root, so the two
        cannot be derived from one another -- the declaration is the only link.

        Returns None rather than guessing whenever the answer is not unique:
        no module declares the namespace, or several do (PHP permits one
        namespace across many files, and two files may both define `format`).
        The trie fallback then applies exactly as before, so this can only
        replace an arbitrary binding with a determined one, never introduce a
        new arbitrary one.

        Matching is ASCII CASE-INSENSITIVE, because namespaces and function
        names are in PHP: `use function app\\text\\FORMAT` binds to
        `App\\Text\\format`, verified by executing it under PHP 8.5. Comparing
        with exact casing sent such imports to the trie, which is the same
        wrong-edge path this function exists to avoid.

        ASCII-only rather than `str.casefold()`: PHP folds A-Z only, so
        folding non-ASCII identifiers would match names the language treats as
        distinct -- trading a missed binding for a wrong one. The registered
        qualified name is returned unchanged; folding is for COMPARISON, never
        for the value handed back, which must stay the graph's real key.
        """
        # A fully-qualified import (`use function \App\Text\format`) is
        # idiomatic PHP and runs identically to the unqualified spelling,
        # verified under PHP 8.5. The leading backslash becomes a leading
        # separator when the path is dotted, which would never match a
        # declared `App.Text`. Stripping it is a spelling normalisation, not a
        # semantic change -- in PHP a `use` path is ALWAYS resolved from the
        # global namespace whether or not the backslash is written.
        imported_path = imported_path.lstrip(cs.SEPARATOR_DOT)
        namespace, separator, symbol = imported_path.rpartition(cs.SEPARATOR_DOT)
        if not separator or not namespace or not symbol:
            return None
        wanted = _php_fold(namespace)
        namespaces = self.import_processor.php.php_module_namespaces
        matches = [
            (module_qn, f"{module_qn}{cs.SEPARATOR_DOT}{symbol}")
            for module_qn, declared in namespaces.items()
            if _php_fold(declared) == wanted
        ]
        found: list[str] = []
        for module_qn, exact_qn in matches:
            # `kind` keeps PHP's separate symbol tables apart: a class and a
            # function may share a name in one namespace (issue #2466).
            if exact_qn in self.function_registry and (
                kind is None or self.function_registry[exact_qn] == kind
            ):
                found.append(exact_qn)
                continue
            # The symbol's casing may differ from the registration too, so a
            # direct lookup can miss where PHP would bind. `find_with_prefix`
            # is the registry's own scoped query -- iterating the whole
            # registry is not part of the trie protocol at all, and would be
            # a full scan per import even where it happened to work.
            #
            # NO trailing separator. The trie splits the prefix on dots and
            # walks a node per part, so `"proj.text."` yields a final EMPTY
            # part that is never a key and the lookup always returns []. An
            # earlier version passed the dot and the whole case-insensitive
            # fallback was silently dead (#1484). The
            # boundary is re-established below by requiring the remainder to
            # start with the separator.
            prefix = module_qn
            wanted_symbol = _php_fold(symbol)
            found.extend(
                qn
                for qn, qn_kind in self.function_registry.find_with_prefix(prefix)
                if (kind is None or qn_kind == kind)
                # `find_with_prefix` walks whole dot-separated parts, so the
                # remainder always starts with the separator for a real
                # descendant. Requiring it re-establishes the boundary the
                # trailing dot used to provide, and rejects a sibling module
                # whose name merely starts with this one (`proj.textutil`).
                and (remainder := qn[len(prefix) :]).startswith(cs.SEPARATOR_DOT)
                # Direct children only: a nested `module.Class.method` is not
                # the free function this import names.
                and cs.SEPARATOR_DOT not in remainder[1:]
                and _php_fold(remainder[1:]) == wanted_symbol
            )
        if len(found) != 1:
            return None
        return found[0]

    def _is_php_function_import(self, call_name: str, module_qn: str | None) -> bool:
        """Whether `call_name` arrived via `use function`, not a class `use`.

        Returns False when `module_qn` is unknown rather than defaulting to
        True: an unidentified module cannot demonstrate the import was a
        function binding, and the fallback's whole purpose is to avoid
        asserting a target it cannot justify. The trie fallback still applies,
        so this can only decline to add an edge, never remove a correct one.
        """
        if module_qn is None:
            return False
        imported = self.import_processor.php.php_function_imports.get(
            module_qn, frozenset()
        )
        if call_name in imported:
            return True
        # PHP function names are case-insensitive at the CALL SITE too, so
        # `use function App\Text\format` followed by `FORMAT()` is valid and
        # runs. Verified by executing it under PHP 8.5. An exact-case lookup
        # here recognised the import but not the call, so the resolver
        # returned None and the call fell to the trie -- the same wrong-edge
        # path, reached through the alias rather than the target.
        folded = _php_fold(call_name)
        return any(_php_fold(name) == folded for name in imported)

    def resolve_php_class_path(
        self, path: PhpClassPath, module_qn: str
    ) -> Resolved | None:
        """The class a fully qualified PHP class path names, and how surely."""
        self._verdict = cs.EdgeResolution.EXACT
        found = self._php_class_path(path, module_qn)
        return None if found is None else Resolved(*found, self._verdict)

    def _php_class_path(
        self, path: PhpClassPath, module_qn: str
    ) -> tuple[str, str] | None:
        """The class a fully qualified PHP class path names (issue #2466).

        A class is a candidate only where PHP declares it in the namespace the
        path spells. A file with one statement-form namespace records it per
        module, which also matches a class written in another case; a class
        in a braced block is registered under that block (`mod.Vendor.Box`)
        and the file records nothing, so its declaration says where it lives.
        A namespaced path binds exactly or not at all. A global path binds
        the caller's own class exactly and another file's by name only.
        """
        *namespace, symbol = path
        if namespace and (
            class_qn := self._php_target_for_namespace_import(
                cs.SEPARATOR_DOT.join(path), kind=cs.NodeLabel.CLASS
            )
        ):
            return self.function_registry[class_qn], class_qn
        wanted = tuple(_php_fold(part) for part in namespace)
        candidates = [
            qn
            for qn in self._php_class_candidates(symbol)
            if self._php_declared_namespace(qn) == wanted
        ]
        if namespace:
            # Several files declaring one namespaced class is a conditional
            # declaration this site cannot choose between.
            if len(candidates) != 1:
                return None
            return self.function_registry[candidates[0]], candidates[0]
        if own := next(
            (qn for qn in candidates if self._parent_qn(qn) == module_qn), None
        ):
            return self.function_registry[own], own
        if not candidates:
            return None
        best = self._best_trie_candidate(candidates, module_qn)
        self._verdict = cs.EdgeResolution.HEURISTIC
        return self.function_registry[best], best

    def _php_class_candidates(self, symbol: str) -> list[str]:
        # Every class whose name PHP would match to `symbol`, which compares
        # class names ASCII case-insensitively: as written, declared in lower
        # case, or declared with capitals (`new box()` builds `Box`). Each is
        # named as declared, which is what the edge must target.
        folded = _php_fold(symbol)
        found = dict.fromkeys(
            [
                *self.function_registry.find_ending_with(symbol),
                *self.function_registry.find_ending_with(folded),
                *self._php_cased_definitions().classes.get(folded, ()),
            ]
        )
        return [qn for qn in found if self.function_registry[qn] == cs.NodeLabel.CLASS]

    def _php_declared_namespace(self, class_qn: str) -> PhpClassPath | None:
        # The ASCII-folded namespace PHP declares `class_qn` in; None when its
        # file is not PHP or its declaration cannot be read this run, which
        # never passes for the global namespace.
        if self._module_language(class_qn) != cs.SupportedLanguage.PHP:
            return None
        module_qn = self._php_module_of(class_qn)
        if module_qn is None:
            return None
        declared = self._php_class_namespaces.get(module_qn)
        if declared is None:
            root = self._cached_module_root(module_qn)
            declared = (
                {
                    qn: tuple(_php_fold(part) for part in namespace)
                    for qn, namespace in php_class_names.declared_class_namespaces(
                        root, module_qn
                    ).items()
                }
                if root is not None
                else {}
            )
            self._php_class_namespaces[module_qn] = declared
        return declared.get(class_qn)

    def _php_module_of(self, qualified_name: str) -> str | None:
        modules = self.type_inference.module_qn_to_file_path
        probe = qualified_name
        while cs.SEPARATOR_DOT in probe:
            probe = self._parent_qn(probe)
            if probe in modules:
                return probe
        return None

    def resolve_php_method_on_class_path(
        self, path: PhpClassPath, method_name: str, module_qn: str
    ) -> Resolved | None:
        """`method_name` on the class `path` names, declared or inherited,
        bound as surely as the class is."""
        if (owner := self.resolve_php_class_path(path, module_qn)) is None:
            return None
        method = self._php_method(owner[1], method_name)
        return None if method is None else Resolved(*method, owner.resolution)

    def php_constructor(self, class_qn: str) -> tuple[str, str] | None:
        """The `__construct` a `new` of `class_qn` runs: the class's own, else
        the nearest one it inherits, which PHP runs in its place."""
        return self._php_method(class_qn, cs.PHP_METHOD_CONSTRUCT)

    def _php_method(self, class_qn: str, method_name: str) -> tuple[str, str] | None:
        # PHP method names are ASCII case-insensitive, so a child's
        # `__CONSTRUCT` overrides its parent's `__construct`: each class is
        # searched in any casing before its ancestors are, and the edge names
        # the method as declared.
        wanted = _php_fold(method_name)
        queue = deque([class_qn])
        seen = {class_qn}
        while queue:
            owner = self._follow_reexports(queue.popleft())
            if (
                method_qn := self._php_declared_method(owner, method_name, wanted)
            ) is not None:
                return self.function_registry[method_qn], method_qn
            for parent in self.facts.class_inheritance.get(owner, []):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return None

    def _php_declared_method(
        self, owner_qn: str, method_name: str, folded: str
    ) -> str | None:
        for spelling in (method_name, folded):
            method_qn = f"{owner_qn}{cs.SEPARATOR_DOT}{spelling}"
            if self.function_registry.get(method_qn) == cs.NodeLabel.METHOD:
                return method_qn
        return self._php_cased_definitions().methods.get((owner_qn, folded))

    def _php_cased_definitions(self) -> _PhpCasedIndex:
        # PHP classes and methods declared with an upper-case letter, by
        # folded name. A lookup by the written or the folded spelling misses
        # them (`box` for `Box`, `__construct` for `__CONSTRUCT`), and the
        # registry's prefix walk cannot list a method like `__CONSTRUCT`,
        # since the trie skips every `__`-prefixed key as one of its markers.
        if self._php_cased_index is None:
            index = _PhpCasedIndex({}, {})
            for qn, kind in self.function_registry.items():
                if kind not in (cs.NodeLabel.CLASS, cs.NodeLabel.METHOD):
                    continue
                owner_qn, _, leaf = qn.rpartition(cs.SEPARATOR_DOT)
                if (folded := _php_fold(leaf)) == leaf or (
                    self._module_language(owner_qn) != cs.SupportedLanguage.PHP
                ):
                    continue
                if kind == cs.NodeLabel.METHOD:
                    index.methods.setdefault((owner_qn, folded), qn)
                else:
                    index.classes.setdefault(folded, []).append(qn)
            self._php_cased_index = index
        return self._php_cased_index

    def _php_import_key(self, call_name: str, import_map: dict[str, str]) -> str | None:
        """The import-map key matching `call_name` under PHP case folding.

        Returns None unless EXACTLY ONE key folds to the same name. Two keys
        differing only in case cannot both be the intended target, and PHP
        would reject the duplicate `use` at compile time anyway -- so
        declining is correct rather than cautious, and the trie fallback still
        applies.
        """
        folded = _php_fold(call_name)
        matches = [key for key in import_map if _php_fold(key) == folded]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _julia_caller_scopes(caller_qn: str, module_qn: str) -> list[str]:
        # The caller's module scopes innermost first (the file module last).
        if not caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
            return [module_qn]
        parts = caller_qn[len(module_qn) + 1 :].split(cs.SEPARATOR_DOT)
        scopes = [module_qn]
        prefix = module_qn
        for part in parts[:-1]:
            prefix = f"{prefix}{cs.SEPARATOR_DOT}{part}"
            scopes.insert(0, prefix)
        return scopes

    def _try_resolve_direct_import(
        self,
        call_name: str,
        import_map: dict[str, str],
        language: cs.SupportedLanguage | None = None,
        module_qn: str | None = None,
    ) -> tuple[str, str] | None:
        if call_name in import_map:
            imported_qn = import_map[call_name]
        elif (
            language == cs.SupportedLanguage.PHP
            and (folded_key := self._php_import_key(call_name, import_map)) is not None
        ):
            # PHP records `import_map` under the DECLARATION spelling, so a
            # call written `FORMAT()` against `use function App\Text\format`
            # misses this lookup entirely and never reaches the folded
            # binding-kind gate below. Reported on #1484 after an earlier fix
            # addressed only the gate: the exact-case map lookup in FRONT of
            # it is where the miss actually happens.
            imported_qn = import_map[folded_key]
            call_name = folded_key
        else:
            return None
        # Gated on the CALLER's language AND on the import being a
        # `use function` binding.
        #
        # The language gate: the namespace map is keyed by module qn and holds
        # only PHP modules, but the import targets it is matched against are
        # dotted strings that any language can produce -- a JS
        # `import { format } from ...` recorded as `App.Text.format` would
        # resolve straight into a PHP function. Reproduced in review on #1484:
        # a JavaScript caller bound to a PHP target, an edge across languages
        # that no source expressed.
        #
        # The `use function` gate: PHP keeps classes and functions in SEPARATE
        # symbol tables, so `use App\Text\format` imports a CLASS while
        # `use function App\Text\format` imports a function. Without this, a
        # class or constant import whose name happens to match a registered
        # PHP function resolved to that function -- also reproduced in review,
        # and it SURVIVED the language gate because the caller is PHP. The two
        # are independent axes. `php_function_imports` already records which
        # local names arrived via `use function`, so this needs no new parsing.
        if (
            language == cs.SupportedLanguage.PHP
            and self._is_php_function_import(call_name, module_qn)
            and imported_qn not in self.function_registry
            and (php_qn := self._php_target_for_namespace_import(imported_qn))
        ):
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=php_qn)
            return self.function_registry[php_qn], php_qn
        if imported_qn in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=imported_qn)
            return self.function_registry[imported_qn], imported_qn
        if language == cs.SupportedLanguage.JULIA:
            # A Julia selected import (`using .api: alpha`) binds the local
            # name to the source MODULE; resolve the member exactly before
            # the name trie, or a same-named function soaks the edge.
            for member in self.import_processor.julia.julia_member_qn_candidates(
                imported_qn, call_name
            ):
                if member in self.function_registry:
                    logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=member)
                    return self.function_registry[member], member
        # A whole-module require alias (`const f = require('./m'); f(x)`)
        # maps to the MODULE qn; when that module's entire export is one
        # function (`module.exports = function ... `), the call is a call to
        # that function (issue #991, fastify's error-serializer).
        direct = self.import_processor.commonjs_direct_exports.get(imported_qn)
        if direct is not None and direct in self.function_registry:
            logger.debug(ls.CALL_DIRECT_IMPORT, call_name=call_name, qn=direct)
            return self.function_registry[direct], direct
        return None

    def _try_resolve_qualified_call(
        self,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        if language == cs.SupportedLanguage.CSHARP:
            result, decided = self._resolve_csharp_qualified_call(
                call_name, module_qn, local_var_types
            )
            if result is not None:
                return result
            if decided:
                return None
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

    def _try_resolve_csharp_qualified_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        result, _ = self._resolve_csharp_qualified_call(
            call_name, module_qn, local_var_types
        )
        return result

    def _csharp_dotted_path_shadowed(
        self, call_name: str, local_var_types: dict[str, str] | None
    ) -> bool:
        return bool(
            local_var_types
            and cs.SEPARATOR_DOT in call_name
            and cs.SEPARATOR_DOUBLE_COLON not in call_name
            and not call_name.startswith(f"global{cs.SEPARATOR_DOT}")
            and call_name.split(cs.SEPARATOR_DOT, 1)[0] in local_var_types
        )

    def _resolve_csharp_qualified_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[tuple[str, str] | None, bool]:
        if self._csharp_dotted_path_shadowed(call_name, local_var_types):
            return None, False
        path = call_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        global_prefix = f"global{cs.SEPARATOR_DOT}"
        is_global = path.startswith(global_prefix)
        if path.startswith(global_prefix):
            path = path[len(global_prefix) :]
        parts = _split_csharp_qualified_path(path)
        if len(parts) < 2 or any(not part for part in parts):
            return None, False

        type_inference = self.type_inference.csharp_type_inference
        for cut in range(len(parts), 0, -1):
            type_path = cs.SEPARATOR_DOT.join(parts[:cut])
            if is_global:
                type_path = f"global{cs.SEPARATOR_DOUBLE_COLON}{type_path}"
            class_qn = type_inference._qualified_type_name_to_qn(type_path, module_qn)
            if class_qn is None:
                continue
            if cut == len(parts):
                return (cs.NodeLabel.CLASS, class_qn), True
            # A namespace-qualified type owns the path even when the member is
            # missing; an unqualified value receiver may still be a field chain.
            return self._csharp_type_member(class_qn, parts[cut:]), True
        if (
            not is_global
            and cs.SEPARATOR_DOUBLE_COLON not in call_name
            and self._csharp_path_starts_with_imported_namespace(parts, module_qn)
        ):
            return None, True
        return None, False

    def _csharp_type_member(
        self, class_qn: str, member_parts: list[str]
    ) -> tuple[str, str] | None:
        # The member a qualified path names on a resolved type: a registered
        # method directly, else a single-segment name inherited from a base.
        member_name = cs.SEPARATOR_DOT.join(member_parts).split(cs.CHAR_ANGLE_OPEN, 1)[
            0
        ]
        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{member_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        if cs.SEPARATOR_DOT not in member_name:
            return self._resolve_inherited_method(class_qn, member_name)
        return None

    def _csharp_path_starts_with_imported_namespace(
        self, parts: list[str], module_qn: str
    ) -> bool:
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        namespaces = {
            namespace
            for module_namespaces in self.import_processor.csharp.csharp_module_namespaces.values()
            for namespace in module_namespaces
        }
        for cut in range(len(parts), 0, -1):
            prefix = cs.SEPARATOR_DOT.join(parts[:cut])
            local_name, separator, remainder = prefix.partition(cs.SEPARATOR_DOT)
            imported = import_map.get(local_name)
            if imported is None:
                continue
            expanded = f"{imported}{separator}{remainder}" if separator else imported
            if expanded in namespaces:
                return True
        return False

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
                self._verdict = cs.EdgeResolution.HEURISTIC
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
        self, call_name: str, module_qn: str, call_point: int | None = None
    ) -> tuple[str, str] | None:
        same_module_func_qn = f"{module_qn}.{call_name}"
        if self._rust.rust_block_item_hidden(
            same_module_func_qn, module_qn, call_point
        ):
            # A Rust block-local item that took the module's own flat qn.
            # It is in scope for its block alone (issue #1061).
            return None
        if self._name_hidden_in_body(same_module_func_qn):
            # A JS/TS function expression flattened to the module's qn; its
            # name is in scope inside its own body alone (issue #2402).
            return None
        if same_module_func_qn in self.function_registry:
            if self._only_object_members(same_module_func_qn):
                # An object literal's function value flattened to the
                # module's qn; the module never binds its key (issue #2435).
                return None
            logger.debug(
                ls.CALL_SAME_MODULE, call_name=call_name, qn=same_module_func_qn
            )
            return self.function_registry[same_module_func_qn], same_module_func_qn
        return None

    def _nameable_candidates(
        self, candidates: list[str], module_qn: str, call_point: int | None = None
    ) -> list[str]:
        # The simple-name fallback matches on the last name segment alone, so
        # it can offer a definition written in a language the caller's cannot
        # call into (a Python `asyncio.sleep` binding to a `sleep` closure in
        # a generated TypeScript client, issue #945). That one is impossible,
        # so drop it; a candidate whose language cannot be determined is kept,
        # so the fallback still answers wherever this evidence is missing.
        caller_language = self._module_language(module_qn)
        reachable = [
            qn
            for qn in candidates
            if self._languages_can_call(caller_language, self._module_language(qn))
            # A Rust block-local item is nameable inside its own block
            # alone, and the site-gated probe answered there already
            # (issue #1061); by simple name it is never a candidate.
            and not self._rust.rust_block_item_hidden(qn, module_qn, call_point)
            and self._rust.rust_crate_reaches(module_qn, qn)
        ]
        # A definition scoped inside another function's body is named from
        # elsewhere only when it escapes (a factory returns it, CommonJS
        # exports it), which is real but rarer than the plain module-level
        # definition it competes with. So prefer the unscoped candidates and
        # fall back to the scoped ones only when they are all there is.
        unscoped = [
            qn
            for qn in reachable
            if self.function_registry.get(self._parent_qn(qn))
            not in _SCOPING_PARENT_TYPES
        ]
        return unscoped or reachable

    @staticmethod
    def _parent_qn(qualified_name: str) -> str:
        return qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[0]

    @staticmethod
    def _languages_can_call(
        caller: cs.SupportedLanguage | None, candidate: cs.SupportedLanguage | None
    ) -> bool:
        if caller is None or candidate is None or caller == candidate:
            return True
        # A candidate written in a sibling language of the caller's family is
        # a legitimate target for the simple-name fallback.
        return any(
            caller in family and candidate in family for family in cs.LANGUAGE_FAMILIES
        )

    def sql_routine_targets(self, call_name: str) -> list[str]:
        """The SQL routines a PostgreSQL invocation of `call_name` can run.

        `call_name` is already normalized (`normalize_sql_reference`). SQL has
        no imports, so every routine indexed from a .sql file is in scope and
        nothing written in another language is: the database cannot run it. A
        schema-qualified name (`billing.fee`) reaches only the routine defined
        under that schema; an unqualified one reaches each schema's routine of
        that name, because the runtime search_path picks among them. The
        string-call side applies the same rule to `callSp('billing.fee')`.
        Overloads come back as their duplicate variants (`tax`, `tax@2`):
        the call site does not spell the argument types that pick one.
        """
        qualified = cs.SEPARATOR_DOT in call_name
        registry = self.function_registry
        # dict as an ordered set: the simple-name index already holds a
        # variant that the dotted-suffix scan cannot see (`fee@2` does not
        # end in `.billing.fee`), so both routes feed one deduplicated list.
        targets: dict[str, None] = {}
        for qn in registry.find_ending_with(call_name):
            for variant in registry.variants(qn):
                if variant in targets or registry.get(variant) != cs.NodeLabel.FUNCTION:
                    continue
                module_qn = self._sql_module_of(variant)
                if module_qn is None:
                    continue
                # The suffix match also accepts a qualifier that is really the
                # FILE name (`fee` defined in billing.sql), which is no schema.
                own_name = qn_markers.natural_qn(variant)[len(module_qn) + 1 :]
                if qualified and own_name != call_name:
                    continue
                targets[variant] = None
        return list(targets)

    def _sql_module_of(self, qualified_name: str) -> str | None:
        # The longest prefix naming an indexed module is the file the routine
        # was defined in; what follows it is the routine's own (possibly
        # schema-qualified) name. Incremental runs seed this map from the
        # graph, so an unchanged file's module is found too.
        modules = self.type_inference.module_qn_to_file_path
        probe = qualified_name
        while cs.SEPARATOR_DOT in probe:
            probe = self._parent_qn(probe)
            if (path := modules.get(probe)) is not None:
                if get_language_for_extension(path.suffix) == cs.SupportedLanguage.SQL:
                    return probe
                return None
        return None

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
                language = _file_language(path.suffix)
                break
            # An incremental run only re-parses changed files, so an
            # unchanged module is absent above; its definitions carry the
            # file path recorded on their graph nodes instead.
            if (
                rehydrated := self.facts.rehydrated_definition_paths.get(probe)
            ) is not None:
                language = _file_language(PurePath(rehydrated).suffix)
                break
            if cs.SEPARATOR_DOT not in probe:
                break
            probe = self._parent_qn(probe)
        self._module_language_cache[qualified_name] = language
        return language

    def _trie_candidates(
        self, call_name: str, module_qn: str, call_point: int | None = None
    ) -> list[str]:
        return self._nameable_candidates(
            self.function_registry.find_ending_with(trie_search_name(call_name)),
            module_qn,
            call_point,
        )

    def _try_resolve_via_trie(
        self,
        call_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
        constructing: bool = False,
    ) -> tuple[str, str] | None:
        search_name = trie_search_name(call_name)
        possible_matches = self._trie_candidates(call_name, module_qn, call_point)
        if search_name == call_name:
            # A bare name never reaches an object literal's function value;
            # a member call (`opts.retry.delay()`) still does (issue #2435).
            possible_matches = [
                qn for qn in possible_matches if not self._only_object_members(qn)
            ]
        if constructing:
            # `new X(...)` names a TYPE: a method or function that merely
            # shares the name is never its target, however close by import
            # distance it sits. A C# static factory `Some.LogEventProperty()`
            # beside `class LogEventProperty` outranked the class once the
            # class qn stopped repeating its namespace (issue #1629).
            possible_matches = [
                qn
                for qn in possible_matches
                if self.function_registry[qn] == cs.NodeLabel.CLASS.value
            ]
        if search_name == call_name:
            # A bare name never reaches a function expression whose own name
            # only its body sees (issue #2402). A member access keeps the
            # match: `req.host` reads the getter express defines as
            # `defineGetter(req, 'host', function host () {...})`.
            possible_matches = [
                qn for qn in possible_matches if not self._name_hidden_in_body(qn)
            ]
        if language == cs.SupportedLanguage.RUST and search_name == call_name:
            # A bare Rust path NEVER names a method (inherent methods need
            # self./Self::/Type::), so a same-named method must not soak
            # up the edge when the real target is external, prelude, or a
            # shadowing closure the graph cannot see (issue #1011).
            possible_matches = [
                qn
                for qn in possible_matches
                if self.function_registry[qn] != cs.NodeLabel.METHOD.value
            ]
        elif (
            search_name == call_name
            and (language or self._module_language(module_qn))
            == cs.SupportedLanguage.PYTHON
        ):
            # A bare Python name is looked up in local, enclosing, global and
            # builtin scope; a method answers to its bare name only inside
            # its own class body, which lives in the caller's module. So a
            # local `run` handed on as a callback, or a variable named like
            # some property, never names another module's method (#2360).
            own_module = f"{module_qn}{cs.SEPARATOR_DOT}"
            possible_matches = [
                qn
                for qn in possible_matches
                if self.function_registry[qn] != cs.NodeLabel.METHOD.value
                or qn.startswith(own_module)
            ]
        if not possible_matches:
            logger.debug(ls.CALL_UNRESOLVED, call_name=call_name)
            return None
        if language == cs.SupportedLanguage.CSHARP and len(possible_matches) > 1:
            possible_matches = self._csharp_prefer_imported(
                possible_matches, module_qn, call_name
            )

        best_candidate_qn = self._best_trie_candidate(possible_matches, module_qn)
        logger.debug(ls.CALL_TRIE_FALLBACK, call_name=call_name, qn=best_candidate_qn)
        self._verdict = cs.EdgeResolution.HEURISTIC
        return self.function_registry[best_candidate_qn], best_candidate_qn

    def _best_trie_candidate(self, possible_matches: list[str], module_qn: str) -> str:
        if len(possible_matches) == 1:
            return possible_matches[0]
        caller_parts = module_qn.split(cs.SEPARATOR_DOT)
        caller_len = len(caller_parts)
        caller_parent_prefix = (
            cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
            if caller_len > 1
            else ""
        )
        return min(
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

    def _csharp_prefer_imported(
        self, candidates: list[str], module_qn: str, call_name: str
    ) -> list[str]:
        # Same-named C# types in two namespaces tied on import distance and
        # broke on qn order, so `using Zeta;` + `new Widget()` bound
        # `Other.Widget` (issue #2001). C# name lookup searches the caller's
        # own namespace, innermost first, before any `using`, so a candidate
        # there wins outright; otherwise the candidates whose
        # owning type sits DIRECTLY in a namespace (or, for `using static`,
        # a type) the file imports: C# does not import nested namespaces.
        containers, import_keys, bare_members = self._csharp_import_keys(
            candidates, call_name
        )
        enclosing = self._csharp_enclosing_namespaces(module_qn)
        # A namespace holds types, not members: the caller's namespace makes
        # `App.Widget` nameable, never `App.Widget.S` callable bare. Only a
        # `using static` does that, which the import preference below checks
        # .
        local = [
            qn
            for qn in candidates
            if containers.get(qn) in enclosing and qn not in bare_members
        ]
        if local:
            innermost = max(len(containers[qn]) for qn in local)
            return [qn for qn in local if len(containers[qn]) == innermost]
        # Only a directive whose local name is its target's own last segment
        # makes names available unqualified (`using Zeta;`, `using static
        # Zeta.Widget;`); an alias (`using Alias = Other;`) does not.
        imported = {
            target
            for local_name, target in self.import_processor.import_mapping.get(
                module_qn, {}
            ).items()
            if local_name == target.rpartition(cs.SEPARATOR_DOT)[2]
        }
        preferred = [qn for qn in candidates if import_keys.get(qn) in imported]
        return preferred or candidates

    def _csharp_import_keys(
        self, candidates: list[str], call_name: str
    ) -> tuple[dict[str, str], dict[str, str], set[str]]:
        """(candidate -> namespace holding its type, candidate -> what an
         import must name to make it available, the bare-called members).

         A type, or a member reached through its type (`Widget.S()`), is
         imported by a namespace directive naming the type's namespace; a BARE
         member (`S()`) only by a `using static` naming its declaring type
        .
        """
        declared = self.type_inference.facts.csharp_class_namespaced
        bare = cs.SEPARATOR_DOT not in call_name
        containers: dict[str, str] = {}
        import_keys: dict[str, str] = {}
        bare_members: set[str] = set()
        for qn in candidates:
            owner = qn
            while owner and owner not in declared:
                owner = owner.rpartition(cs.SEPARATOR_DOT)[0]
            if not owner:
                continue
            containers[qn] = declared[owner].rpartition(cs.SEPARATOR_DOT)[0]
            if bare and owner != qn:
                bare_members.add(qn)
            import_keys[qn] = declared[owner] if qn in bare_members else containers[qn]
        return containers, import_keys, bare_members

    def _csharp_enclosing_namespaces(self, module_qn: str) -> set[str]:
        """Every namespace enclosing the file's call sites, innermost included.

         Only known when the file declares ONE nested chain; with sibling
         namespaces (`App` and `Other` in one file) neither is local to every
         call site, so no local precedence applies rather than a guessed one
        .
        """
        declared_here = sorted(
            self.import_processor.csharp.csharp_module_namespaces.get(module_qn, {}),
            key=len,
        )
        if not declared_here or not all(
            declared_here[-1] == ns
            or declared_here[-1].startswith(f"{ns}{cs.SEPARATOR_DOT}")
            for ns in declared_here
        ):
            return set()
        parts = declared_here[-1].split(cs.SEPARATOR_DOT)
        return {cs.SEPARATOR_DOT.join(parts[:cut]) for cut in range(1, len(parts) + 1)}

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
        # `Type::f()` with `Type` imported from an external crate calls that
        # crate's function; the import probe below would resolve the raw
        # path by its last segment to a same-named first-party type, and the
        # type probe after it by simple name (issue #2622).
        if separator == cs.SEPARATOR_DOUBLE_COLON and (
            (target := import_map.get(object_name)) is not None
            and self._rust.rust_path_is_external(target, module_qn)
        ):
            return None

        if result := self._try_resolve_via_local_type(
            object_name,
            method_name,
            separator,
            call_name,
            import_map,
            module_qn,
            local_var_types,
            language,
        ):
            return result

        if result := self._try_resolve_via_import(
            object_name, method_name, separator, call_name, import_map, language
        ):
            return result

        # A Lua table defined in this module (`Account.deposit(acc, 1)`,
        # `Account:new()`) names its member exactly, so it must answer before
        # the same-module free-function guess below: a `local function
        # deposit` is not `Account`'s member (issue #2481).
        if language == cs.SupportedLanguage.LUA and (
            result := self._try_resolve_lua_same_module_member(call_name, module_qn)
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
        # Rust has no such receiver: `Type::f()` names an associated item of
        # Type and `recv.f()` a method, never the caller module's free `f`
        # (`String::new()` bound the file's own `fn new`, issue #2543).
        if language == cs.SupportedLanguage.RUST:
            return None
        resolved = self._try_resolve_module_method(method_name, call_name, module_qn)
        if resolved is not None:
            self._verdict = cs.EdgeResolution.HEURISTIC
        return resolved

    def _try_resolve_static_type_method(
        self, object_name: str, method_name: str, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not (
            class_qn := self._rust.rust_glob_imported_type(object_name, module_qn)
            or self._resolve_class_name(object_name, module_qn)
        ):
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
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        if not local_var_types or object_name not in local_var_types:
            return None

        var_type = local_var_types[object_name]

        if class_qn := self._resolve_class_qn_from_type(
            var_type, import_map, module_qn
        ):
            if result := self._try_method_on_class(
                class_qn,
                method_name,
                separator,
                call_name,
                object_name,
                var_type,
                language,
            ):
                return result
            if language == cs.SupportedLanguage.DART and (
                extension := self._dart_extension_member(
                    class_qn, method_name, module_qn
                )
            ):
                return extension

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
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        # A Lua instance reaches its class's members under either spelling:
        # `acc:deposit()` is `Account:deposit`, `s.area(s)` and `s:twice()`
        # may name a member defined with the other separator.
        method_qns = (
            lua_utils.member_spellings(class_qn, method_name, separator)
            if language == cs.SupportedLanguage.LUA
            else (f"{class_qn}{separator}{method_name}",)
        )
        for method_qn in method_qns:
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
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, str] | None:
        if object_name not in import_map:
            return None

        if language == cs.SupportedLanguage.LUA:
            return self._try_resolve_lua_module_member(
                import_map[object_name], object_name, method_name, separator, call_name
            )

        target = import_map[object_name]
        if (
            language == cs.SupportedLanguage.RUST
            and cs.SEPARATOR_DOUBLE_COLON not in target
        ):
            # `use crate::cmd::Get;` names the module that re-exports `Get`.
            # Unfollowed, `Get::new()` missed here and the simple-name
            # search bound it to whichever `Get` it reached first, as an
            # exact edge (issue #2542).
            target = self._follow_reexports(target)
        class_qn = self._resolve_imported_class_qn(
            target, object_name, method_name, separator
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

    def _try_resolve_lua_module_member(
        self,
        imported_qn: str,
        alias: str,
        member: str,
        separator: str,
        call_name: str,
    ) -> tuple[str, str] | None:
        # `require` binds the table a module returns. The module usually names
        # it after the alias (`local Storage = {}` in storage/Storage.lua);
        # otherwise (`local M = {}`) the package-member search finds the one
        # table holding the member. Both spellings throughout: `util:method()`
        # reaches `M:method`, `util.add()` reaches `M.add` (issue #2481).
        for owner_qn in (f"{imported_qn}{cs.SEPARATOR_DOT}{alias}", imported_qn):
            if result := self._lua_table_member(owner_qn, member, separator, call_name):
                return result
        return self._try_resolve_package_member(
            imported_qn, member, cs.SupportedLanguage.LUA
        )

    def _try_resolve_lua_same_module_member(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if (split := lua_utils.split_member_call(call_name)) is None:
            return None
        table_path, separator, member = split
        return self._lua_table_member(
            f"{module_qn}{cs.SEPARATOR_DOT}{table_path}", member, separator, call_name
        )

    def _lua_table_member(
        self, owner_qn: str, member: str, separator: str, call_name: str
    ) -> tuple[str, str] | None:
        for qn in lua_utils.member_spellings(owner_qn, member, separator):
            if qn in self.function_registry:
                logger.debug(ls.CALL_LUA_TABLE_MEMBER, call_name=call_name, qn=qn)
                return self.function_registry[qn], qn
        return None

    def _try_resolve_package_member(
        self,
        package_qn: str,
        member_name: str,
        language: cs.SupportedLanguage | None = None,
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
        members = self.function_registry.find_with_prefix(package_qn)
        candidates = [
            qn
            for qn, _ in members
            if qn.count(cs.SEPARATOR_DOT) == member_depth
            and qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] == member_name
        ]
        if language == cs.SupportedLanguage.LUA:
            # A Lua module's returned table holds `M.add` one segment below
            # the file, like a Go file's function, but its colon-method
            # `M:method` is that one segment itself (issue #2481).
            candidates += [
                qn
                for qn, _ in members
                if qn.count(cs.SEPARATOR_DOT) == member_depth - 1
                and qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].partition(
                    cs.LUA_METHOD_SEPARATOR
                )[2]
                == member_name
            ]
        if not candidates:
            return None
        member_qn = min(candidates)
        if len(candidates) > 1:
            self._verdict = cs.EdgeResolution.HEURISTIC
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
            class_qn = self._rust.resolve_rust_class_qn(class_qn)

        potential_class_qn = f"{class_qn}.{object_name}"
        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
        if test_method_qn in self.function_registry:
            return potential_class_qn
        return class_qn

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
            # `import pkg.util` is recorded as `pkg -> proj.pkg.util`, so a
            # call spelled `pkg.util.helper` repeats the module's tail; the
            # import names the module exactly, so the call is exact too and
            # must not fall to the name-only trie (issue #1526).
            if module_qn_hit := self._import_tail_qualified(class_qn, parts, call_name):
                return module_qn_hit

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

    def _import_tail_qualified(
        self, class_qn: str, parts: list[str], call_name: str
    ) -> tuple[str, str] | None:
        """`parts` re-spells the tail of the imported module `class_qn`."""
        for cut in range(len(parts) - 1, 1, -1):
            tail = cs.SEPARATOR_DOT.join(parts[:cut])
            if class_qn != tail and not class_qn.endswith(cs.SEPARATOR_DOT + tail):
                continue
            method_qn = f"{class_qn}.{cs.SEPARATOR_DOT.join(parts[cut:])}"
            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_IMPORT_QUALIFIED, call_name=call_name, method_qn=method_qn
                )
                return self.function_registry[method_qn], method_qn
        return None

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
            field_type = self.type_inference.facts.class_field_types.get(
                class_qn, {}
            ).get(field)
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
        return self._python.operator_dunder_targets(
            operand_text, dunder, module_qn, local_var_types
        )

    def resolve_scala_selection(
        self,
        receiver_type: str,
        member: str,
        module_qn: str,
        class_context: str | None,
        position: int,
    ) -> tuple[str, str] | None:
        """The method a parameterless Scala selection `receiver.member` runs.

        A `val` read and a parameterless call are spelled alike (`c.size`), so
        only a receiver whose class is KNOWN decides: `this`, a local typed by
        its binding, or an object named outright. `receiver_type` is the type
        name written for it, or `this`. The member must be a method of that
        class or an ancestor; no name-wide fallback answers for it.
        """
        class_qn = (
            class_context
            if receiver_type == cs.SCALA_THIS
            else self.scala_class_qn(receiver_type, module_qn, position)
        )
        if class_qn is None:
            return None
        result = self._try_resolve_method(class_qn, member)
        if result is None or result[0] != cs.NodeLabel.METHOD:
            return None
        return result

    def scala_class_qn(
        self, written: str, module_qn: str, position: int | None = None
    ) -> str | None:
        """The registered class a type name written in a Scala file names."""
        class_qn = self.import_processor.scala.scala_type_qn(
            module_qn, written, position
        )
        if (
            class_qn is None
            or self.function_registry.get(class_qn) != cs.NodeLabel.CLASS
        ):
            return None
        return class_qn

    def resolve_scala_callee(
        self, call_name: str, module_qn: str, position: int, constructing: bool
    ) -> tuple[bool, tuple[str, str] | None]:
        """A Scala callee named through the imports in scope where it is called.

        Decided (True) for a `new C(...)` the compiler's lookup reaches, and
        for any name a package block around the call imports: that block's
        import is the only binding visible there, so the file-level map, which
        a sibling block may share a name with, must not answer. Undecided
        otherwise.
        """
        if constructing and (
            class_qn := self.scala_class_qn(call_name, module_qn, position)
        ):
            return True, (cs.NodeLabel.CLASS, class_qn)
        head, _, rest = call_name.partition(cs.SEPARATOR_DOT)
        imported = self.import_processor.scala.scala_block_import(
            module_qn, head, position
        )
        if imported is None:
            return False, None
        target = f"{imported}{cs.SEPARATOR_DOT}{rest}" if rest else imported
        kind = self.function_registry.get(target)
        if kind is None or (constructing and kind != cs.NodeLabel.CLASS):
            return True, None
        return True, (kind, target)

    def resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        return resolve_js_builtin_call(call_name)

    def cpp_member_operator(
        self, call_name: str, operand_type_qn: str
    ) -> tuple[str, str] | None:
        return self._cpp.cpp_member_operator(call_name, operand_type_qn)

    def cpp_free_operator_for_type(
        self, call_name: str, operand_type_qn: str, position: int, arity: int
    ) -> tuple[str, str] | None:
        return self._cpp.cpp_free_operator_for_type(
            call_name, operand_type_qn, position, arity
        )

    def cpp_operand_class_qn(
        self,
        operand_name: str | None,
        local_var_types: dict[str, str] | None,
        module_qn: str,
    ) -> str | None:
        return self._cpp.cpp_operand_class_qn(operand_name, local_var_types, module_qn)

    def resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        return self._cpp.resolve_cpp_operator_call(call_name, module_qn)

    def _infer_chained_object_type(
        self,
        object_expr: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
    ) -> str | None:
        # Type of a chained receiver expression like `c.Root()` using the shared
        # method_return_types map: the base is a typed local (`c` -> Command), and
        # each `.method()` hop advances the type by that method's return type
        # (Root() -> Command). Language-agnostic; returns the bare type name of the
        # final hop, or None if any hop is untyped/unknown (then the chain stays
        # unresolved). No early-out on an empty method_return_types map: a
        # constructor-temporary base (`Reader<T>(...).m()`) types itself from the
        # class registry alone.
        parts = split_receiver_chain(object_expr)
        if parts is None:
            return None
        base = parts[0]
        if not base:
            return None
        if cs.CHAR_PAREN_OPEN in base:
            # A Rust chain rooted in an associated-function call
            # (`Ping::new(msg).into_frame()`): type the base from the assoc fn's
            # recorded return type. A bare-identifier factory call
            # (`parser(ia, cb).parse()`, C++): type it from the factory's recorded
            # return type. Other paren bases stay unresolved.
            current_type = self._rust.infer_rust_assoc_base_type(
                base, module_qn, call_point, rust_caller(language, caller_qn)
            ) or self._infer_call_base_type(
                base,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
                call_point,
            )
        elif (
            folded := self._fold_import_prefix_hop(
                parts, module_qn, local_var_types, call_point, language
            )
        ) is not None:
            # `p.Box(1).height` under `import '...' as p;`: the base names an
            # IMPORT, not a local, so the local lookup below would end the
            # chain before the construction hop is seen (issue #2033). Fold
            # the prefix into the next hop and type that hop instead.
            current_type, parts = folded
        elif (
            folded := self._js_ts.fold_js_call_hop(
                parts,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language,
                call_point,
            )
        ) is not None:
            current_type, parts = folded
        elif local_var_types:
            current_type = local_var_types.get(base)
        else:
            return None
        for part in parts[1:]:
            if not current_type or cs.CHAR_PAREN_OPEN not in part:
                return None
            method = part.split(cs.CHAR_PAREN_OPEN, 1)[0]
            if language == cs.SupportedLanguage.DART:
                current_type = self._dart_member_return_type(
                    self.resolve_dart_written_type(current_type, module_qn),
                    method,
                    module_qn,
                )
                continue
            current_type = self._member_hop_type(
                current_type, method, module_qn, language, caller_qn
            )
        return current_type

    def _member_hop_type(
        self,
        receiver_type: str,
        method: str,
        module_qn: str,
        language: cs.SupportedLanguage | None,
        caller_qn: str | None,
    ) -> str | None:
        # What `receiver.method()` hands the next hop. A receiver that is a
        # JS/TS class itself (`C.self()` from a static `return this`) reaches
        # only its static members, its ancestors' included.
        if (class_value := class_value_of(receiver_type)) is not None:
            found = self._js_ts.js_static_member(class_value, method)
            if found is None:
                return None
            return self._hop_return_type(found[0], language, True, class_value)
        class_qn = self._chain_class_qn(
            receiver_type, module_qn, rust_caller(language, caller_qn)
        )
        return self._hop_return_type(
            f"{class_qn}{cs.SEPARATOR_DOT}{method}", language, False, class_qn
        )

    def _hop_return_type(
        self,
        callable_qn: str,
        language: cs.SupportedLanguage | None,
        static: bool = False,
        receiver: str | None = None,
    ) -> str | None:
        # The type a call to `callable_qn` hands the next hop. Most languages
        # record it as they ingest; a JS/TS return is read off the callee's
        # declaration on demand (issue #2468), and a `this` return names the
        # class it is called through (`receiver`). A hop's receiver is an
        # instance, so it reaches the instance member unless told otherwise.
        recorded = self.type_inference.facts.method_return_types.get(callable_qn)
        if recorded is None and language in cs.JS_TS_LANGUAGES:
            return bind_receiver(
                self._js_ts.js_return_class(callable_qn, static), receiver
            )
        return recorded

    def _callee_return_type(
        self, callee_qn: str, language: cs.SupportedLanguage | None
    ) -> str | None:
        # Read right after the callee resolved. A JS/TS callee bound only by
        # name is a guess, and typing the rest of the chain from its return
        # would build every later hop on that guess.
        if language in cs.JS_TS_LANGUAGES and self._verdict != cs.EdgeResolution.EXACT:
            return None
        return self._hop_return_type(callee_qn, language)

    def _dart_member_return_type(
        self, class_qn: str, member: str, module_qn: str
    ) -> str | None:
        """The class a Dart hop `.member()` on a `class_qn` receiver returns.

        The member is the class's own, else an inherited one, else an
        extension's (Dart's lookup order). Its return type is recorded under
        the DECLARING qn, so a hop through an inherited or extension member
        ended the chain (`o.move(1).sum()` on a subclass of Point, issue
        #2482). It is also written in the DECLARING library, so it resolves
        there: read in the caller's file instead, a bare `Result` bound to
        whichever `Result` the CALLER sees (PR #2804).
        """
        return_types = self.type_inference.facts.method_return_types
        own_qn = f"{class_qn}{cs.SEPARATOR_DOT}{member}"
        if own_qn in return_types or own_qn in self.function_registry:
            # An own member shadows every inherited and extension one, even
            # when it records no return type.
            member_qn = own_qn
        elif found := self._resolve_inherited_method(
            class_qn, member
        ) or self._dart_extension_member(class_qn, member, module_qn):
            member_qn = found[1]
        else:
            return None
        returned = return_types.get(member_qn)
        if not returned:
            return None
        # Dart has no nested types: a member sits in a top-level class, mixin,
        # enum or extension, so its library is two segments up.
        declaring_module = member_qn.rsplit(cs.SEPARATOR_DOT, 2)[0]
        return self.resolve_dart_written_type(returned, declaring_module)

    def resolve_dart_written_type(self, written: str, module_qn: str) -> str:
        """A Dart type as written in `module_qn` -> its qn, else as written.

        Dart scopes a type name to the library it is written in (Greptile,
        PR #2804): `p.Point` names Point through `module_qn`'s OWN `import
        ... as p` (read as a qualified name it matched nothing), and a bare
        `Result` is the library's own declaration, else the ONE its imports
        expose, before any project-wide guess. A name that is already a
        registered qn (an earlier hop's result) is kept.
        """
        if written in self.function_registry:
            return written
        prefix, dot, name = written.partition(cs.SEPARATOR_DOT)
        if dot:
            hit = self._unique_prefixed_definition(
                self._dart_prefix_targets(prefix, module_qn), name
            )
            if hit is not None:
                return hit[1]
        else:
            own = f"{module_qn}{cs.SEPARATOR_DOT}{written}"
            if own in self.function_registry:
                return own
            imported = [
                qn
                for library in self._dart_visible_libraries(module_qn) - {module_qn}
                if (qn := f"{library}{cs.SEPARATOR_DOT}{written}")
                in self.function_registry
            ]
            if len(imported) == 1:
                return imported[0]
        return self._chain_class_qn(written, module_qn)

    def _dart_extension_member(
        self, class_qn: str, member: str, module_qn: str
    ) -> tuple[str, str] | None:
        """The extension member a Dart receiver of `class_qn` reaches, if any.

        Dart consults extensions only after the receiver's own and inherited
        members miss, applies only those the caller's library can see, and
        among them the one on the most specific type wins: one `on` the
        receiver's class beats one `on` an ancestor. Two at the same level are
        ambiguous (a compile error in Dart) and bind nothing.
        """
        on_types = self.type_inference.facts.dart_extension_on_types
        if not on_types:
            return None
        # The `on` type is resolved from the extension's own library, where it
        # was written (`on p.Point` through ITS `as p` import); an extension
        # is always a top-level declaration.
        candidates = [
            (
                member_qn,
                ext_qn,
                self.resolve_dart_written_type(on_types[ext_qn], ext_module),
                ext_module,
            )
            for member_qn in self.function_registry.find_ending_with(member)
            if (ext_qn := member_qn.rpartition(cs.SEPARATOR_DOT)[0]) in on_types
            and (ext_module := ext_qn.rpartition(cs.SEPARATOR_DOT)[0])
        ]
        if not candidates:
            return None
        levels = self._class_and_ancestor_levels(class_qn)
        receiver_types = set().union(*levels)
        # Applicability, then visibility, then specificity. Only an extension
        # `on` the receiver's class or an ancestor competes at all: filtering
        # by visibility first let a visible extension on an UNRELATED type
        # push out the applicable one (PR #2804).
        applicable = [
            candidate for candidate in candidates if candidate[2] in receiver_types
        ]
        if not applicable:
            return None
        # Among those, an unimported one `on Derived` must not beat an
        # imported one `on Base` (PR #2804). A `package:` import is
        # kept verbatim and proves nothing, so when no applicable candidate is
        # provably visible they all stay in play rather than the call being
        # dropped. A provable one also wins a same-level tie against an
        # unprovable one: were both visible, Dart would reject the call as
        # ambiguous, so in code that compiles the unprovable one is not.
        visible_libraries = self._dart_visible_libraries(module_qn)
        pool = [
            candidate for candidate in applicable if candidate[3] in visible_libraries
        ] or applicable
        for level in levels:
            matches = [
                member_qn for member_qn, _ext, on_qn, _module in pool if on_qn in level
            ]
            if len(matches) == 1:
                return self.function_registry[matches[0]], matches[0]
            if matches:
                return None
        return None

    def _class_and_ancestor_levels(self, class_qn: str) -> list[set[str]]:
        # The class, then each breadth-first generation of its ancestors.
        levels: list[set[str]] = [{class_qn}]
        seen = {class_qn}
        frontier = [class_qn]
        while frontier:
            parents = {
                self._follow_reexports(parent)
                for child in frontier
                for parent in self.facts.class_inheritance.get(child, [])
            } - seen
            if not parents:
                break
            levels.append(parents)
            seen |= parents
            frontier = list(parents)
        return levels

    def _dart_visible_libraries(self, module_qn: str) -> set[str]:
        """The libraries whose top-level names `module_qn` can see: itself,
        every library it imports (prefixed or not), and whatever those hand
        on through `export` / `part`, transitively. A library a dependency
        merely IMPORTS stays hidden (issue #2482)."""
        processor = self.import_processor
        imported = set(processor.import_mapping.get(module_qn, {}).values())
        for libraries in processor.dart.dart_import_aliases.get(module_qn, {}).values():
            imported.update(libraries)
        visible = {module_qn, *imported}
        frontier = list(imported)
        while frontier:
            for exposed in processor.dart.dart_exposed_libraries.get(
                frontier.pop(), ()
            ):
                if exposed not in visible:
                    visible.add(exposed)
                    frontier.append(exposed)
        return visible

    def _drop_named_constructor(self, name: str, target: str) -> str:
        """`Box.named` -> `Box` when the dotted name is not a definition but
        its head is: the tail is a named constructor, not part of the type
        (issue #2033). Any other name is returned unchanged."""
        if cs.SEPARATOR_DOT not in name:
            return name
        head = name.split(cs.SEPARATOR_DOT, 1)[0]
        full_qn = f"{target}{cs.SEPARATOR_DOT}{name}"
        head_qn = f"{target}{cs.SEPARATOR_DOT}{head}"
        if (
            self.function_registry.get(full_qn) is None
            and self.function_registry.get(head_qn) is not None
        ):
            return head
        return name

    def _names_own_constructible(self, name: str, module_qn: str) -> bool:
        """Does `name` resolve, from this module, to something constructible?

        `new X.named(1).m` splits to base `X` + hop `named()`, so the base is
        the CLASS and the hop its named constructor (issue #2033). Asked
        before the import map, since a class in this module is not an import.
        """
        own = self._resolve_class_qn_from_type(
            name,
            self.import_processor.import_mapping.get(module_qn, {}),
            module_qn,
        )
        return bool(own) and self.function_registry.get(own) in (
            _CONSTRUCTIBLE_NODE_TYPES
        )

    def _dart_call_on_shadow(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        call_point: int | None,
    ) -> bool:
        """Is `call_name`'s first segment an import prefix a binding at this
        site shadows, with no type to resolve the call through instead?"""
        if cs.SEPARATOR_DOT not in call_name:
            return False
        prefix = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        typed = local_var_types.get(prefix) if local_var_types else None
        if typed and typed != cs.DART_DYNAMIC_TYPE:
            return False
        return bool(
            self._dart_prefix_targets(prefix, module_qn)
        ) and self._dart_prefix_is_shadowed(prefix, module_qn, call_point)

    def _dart_prefix_is_shadowed(
        self, prefix: str, module_qn: str, call_point: int | None
    ) -> bool:
        """Does a local or parameter named `prefix` bind it at this site?

        The resolver's `local_var_types` only holds names it could infer a
        TYPE for, so `var p = 1` is absent from it and an inferred-type check
        cannot see the shadow. The import processor records the syntactic
        binding spans instead, which are independent of inference.

        A site with no recorded position cannot be placed against a span, so
        it is treated as unshadowed: flow and taint passes resolve without a
        position, and losing the fold there would drop real edges.
        """
        if call_point is None:
            return False
        spans = self.import_processor.dart.dart_prefix_shadows.get(module_qn, {})
        return any(lo <= call_point < hi for lo, hi in spans.get(prefix, ()))

    def _fold_import_prefix_hop(
        self,
        parts: list[str],
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        call_point: int | None = None,
        language: cs.SupportedLanguage | None = None,
    ) -> tuple[str, list[str]] | None:
        # An import prefix followed by a CONSTRUCTION hop (`p`, `Box()`, ...)
        # collapses to that class: the prefix carries no type of its own, and
        # the class is what the rest of the chain hangs off. Returns the
        # constructed type and the remaining chain with the prefix removed, or
        # None when the base is not a prefix or the next hop is not a
        # construction the registry knows.
        if len(parts) < 2:
            return None
        # The bare `p.Box.named(1)` (no `new`) has no construction node, so
        # the class and its named constructor arrive as two hops; rejoined,
        # they take the path `new p.Box.named(1)` takes (issue #2084).
        if (
            language == cs.SupportedLanguage.DART
            and len(parts) >= 3
            and cs.CHAR_PAREN_OPEN not in parts[1]
            and cs.CHAR_PAREN_OPEN in parts[2]
        ):
            parts = [parts[0], f"{parts[1]}{cs.SEPARATOR_DOT}{parts[2]}", *parts[3:]]
        prefix, hop = parts[0], parts[1]
        if cs.CHAR_PAREN_OPEN in prefix or cs.CHAR_PAREN_OPEN not in hop:
            return None
        # A binding of that name at this site shadows the import, whether or
        # not a type could be inferred for it (issue #2033).
        if self._dart_prefix_is_shadowed(prefix, module_qn, call_point):
            return None
        # `new X.named(1).m` splits to base `X` + hop `named()`: the base is
        # the CLASS itself and the hop is its named constructor, so the
        # receiver's type is the base (issue #2033). Checked before the
        # import map, since a class in this module is not an import.
        # Dart only: elsewhere `Foo.create()` is a static FACTORY whose
        # recorded return type the chain must keep, and folding it to `Foo`
        # dropped that hop.
        if (
            language == cs.SupportedLanguage.DART
            and not (local_var_types and prefix in local_var_types)
            and self._names_own_constructible(prefix, module_qn)
        ):
            return prefix, parts[1:]
        # A local or parameter of that name SHADOWS the import prefix, so the
        # base is that variable and the chain is not a prefixed construction
        # at all. Without this a `dynamic p` parameter still resolved
        # `p.Box(1).height` through `import ... as p`, emitting an edge where
        # the resolver had emitted none.
        if local_var_types and prefix in local_var_types:
            return None
        name = hop.split(cs.CHAR_PAREN_OPEN, 1)[0]
        hit = self._unique_prefixed_definition(
            self._dart_prefix_targets(prefix, module_qn), name
        )
        if hit is None:
            return None
        target, qn, kind = hit
        # The hop must be a CONSTRUCTION, so the imported module must define
        # that name as a CLASS. Accepting any registry entry bound a
        # `mod.factory()` whose factory is a FUNCTION to a same-named class in
        # an unrelated module: a confidently wrong edge where the resolver
        # previously emitted none.
        if kind in _CONSTRUCTIBLE_NODE_TYPES:
            # The QUALIFIED name, not the bare one: `qn` is the exact class
            # the prefix names, and returning `name` sent the rest of the
            # chain back through a suffix lookup that another module's
            # same-named class could win (PR #2040).
            return qn, parts[1:]
        # A function hop types the chain by its RECORDED return type instead;
        # with none recorded the chain stays unresolved rather than guessing.
        # The type is a BARE name, qualified in the factory's OWN module: in
        # the caller another import's same-named class could win (Copilot,
        # PR #2040).
        returned = self.type_inference.facts.method_return_types.get(qn)
        return (self._chain_class_qn(returned, target), parts[1:]) if returned else None

    def _dart_prefix_targets(self, prefix: str, module_qn: str) -> list[str]:
        # An explicit `as` alias wins over the file-derived key: with
        # `import 'helper.dart'; import 'other.dart' as helper;` the name
        # `helper` in the source is other.dart, while import_mapping still
        # holds helper.dart under that key so both IMPORTS edges survive
        # (PR #2040).
        aliases = self.import_processor.dart.dart_import_aliases.get(module_qn, {})
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        return aliases.get(prefix) or (
            [import_map[prefix]] if import_map.get(prefix) else []
        )

    def _unique_prefixed_definition(
        self, targets: list[str], name: str
    ) -> tuple[str, str, cs.NodeLabel] | None:
        # Imports sharing a prefix are searched together, and `p.Box` names
        # the ONE library defining `Box`; a name several of them define is
        # an ambiguous import, so neither is guessed (PR #2040).
        if not targets or not name:
            return None
        hits = [
            (target, qn, kind)
            for target in targets
            if (
                kind := self.function_registry.get(
                    qn := self._prefixed_qn(target, name)
                )
            )
            is not None
        ]
        return hits[0] if len(hits) == 1 else None

    def _prefixed_qn(self, target: str, name: str) -> str:
        # `p.Box.named()` reaches the fold as prefix `p` and hop `Box.named`:
        # the parser cannot tell a named CONSTRUCTOR from a class, because
        # both are a trailing identifier (issue #2033). The import map can: if
        # the full dotted name is not a definition but its head is, the tail
        # is a constructor and the receiver's type is the head.
        member = self._drop_named_constructor(name, target)
        return f"{target}{cs.SEPARATOR_DOT}{member}"

    def _chain_class_qn(
        self, type_name: str, module_qn: str, rust_caller: str | None = None
    ) -> str:
        # Resolve a bare type name from a chained-call hop to its class qn, honoring
        # imports (a Rust `use` target is a raw `::`-path, not a registry qn), so a
        # method-return-type lookup keyed by the class qn hits. A Rust caller's own
        # body or inline-mod `use` binds the name ahead of the file's (#2622).
        if rust_caller and (
            scoped := self._rust.rust_scoped_type(type_name, module_qn, rust_caller)
        ):
            return scoped
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
        call_point: int | None = None,
    ) -> str | None:
        # `parser(ia, cb).parse()`: the receiver is a bare-identifier factory call.
        # Resolve the callee to its function/method qn and return its recorded
        # return type. When none resolves, a C++ callee may instead be a
        # CONSTRUCTOR TEMPORARY (`Reader<decltype(ia)>(...)`, nlohmann's
        # from_cbor): the callee names the receiver's class itself. The callee is
        # cut at `<` or `(`, whichever comes first, since template args can carry
        # their own parens. A JS/TS construction (`new Box()`) has no callee to
        # resolve: it is the instance it builds.
        if (
            constructed := self._js_ts.infer_js_construction_base_type(
                base, module_qn, language, call_point
            )
        ) is not None:
            return constructed
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
            callee,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            language,
            call_point,
        )
        if resolved is not None:
            return_type = self._callee_return_type(resolved[1], language)
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
            and self._rust.rust_crate_reaches(module_qn, qn)
        ]
        if not matches and cs.SEPARATOR_DOT in type_path:
            simple = type_path.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            matches = [
                qn
                for qn in self.function_registry.find_ending_with(simple)
                if self.function_registry.get(qn) == cs.NodeLabel.CLASS
                and self._rust.rust_crate_reaches(module_qn, qn)
            ]
        if not matches:
            return None
        matches.sort(key=lambda qn: (len(qn), qn))
        return matches[0]

    def _is_method_chain(
        self, call_name: str, language: cs.SupportedLanguage | None = None
    ) -> bool:
        if cs.CHAR_PAREN_OPEN not in call_name or cs.CHAR_PAREN_CLOSE not in call_name:
            return False
        parts = call_name.split(cs.SEPARATOR_DOT)
        # Rust: split between hops only. The `.` in a float argument cut
        # `Circle::new(2.0).area` into `Circle::new(2` and `0)`, so no hop held
        # both parens and `area` fell to a by-name guess (issue #2698).
        if language == cs.SupportedLanguage.RUST:
            parts = split_receiver_chain(call_name) or parts
        method_calls = sum(
            cs.CHAR_PAREN_OPEN in part and cs.CHAR_PAREN_CLOSE in part for part in parts
        )
        return method_calls >= 1 and len(parts) >= 2

    def _qualify_chained_object_type(
        self, object_type: str, module_qn: str, rust_caller: str | None = None
    ) -> str:
        full_object_type = object_type
        if cs.SEPARATOR_DOT not in object_type:
            # Honor imports (Rust `use` targets are raw `::`-paths) so an
            # imported chained type (`Get::new(k).into_frame()`) resolves.
            if resolved_class := self._chain_class_qn(
                object_type, module_qn, rust_caller
            ):
                full_object_type = resolved_class
        return full_object_type

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
    ) -> tuple[str, str] | None:
        match = _CHAINED_METHOD_PATTERN.search(call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        python = self.type_inference.python_type_inference
        object_type = python.expression_analyzer.infer_expression_return_type(
            object_expr, module_qn, local_var_types
        ) or self._infer_chained_object_type(
            object_expr,
            module_qn,
            local_var_types,
            class_context,
            caller_qn,
            language,
            call_point,
        )
        if object_type and (class_value := class_value_of(object_type)):
            # The class itself, not an instance: only a static member binds.
            return self._js_ts.js_static_member_target(class_value, final_method)
        if object_type:
            full_object_type = self._qualify_chained_object_type(
                object_type, module_qn, rust_caller(language, caller_qn)
            )

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

            if language == cs.SupportedLanguage.DART and (
                extension := self._dart_extension_member(
                    full_object_type, final_method, module_qn
                )
            ):
                return extension

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

        if current_class_qn not in self.facts.class_inheritance:
            logger.debug(ls.CALL_SUPER_NO_INHERITANCE, class_qn=current_class_qn)
            return None

        parent_classes = self.facts.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(ls.CALL_SUPER_NO_PARENTS, class_qn=current_class_qn)
            return None

        if result := self._resolve_inherited_method(
            current_class_qn, method_name, own_members=False
        ):
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

    def _member_alias(self, class_qn: str, member: str) -> tuple[str, str] | None:
        # The alias is no node of its own, so a call through it binds to the
        # first method it may hold; the call pass fans out to the others
        # (member_alias_fanout).
        for target_qn in self.function_registry.member_alias_targets(
            f"{class_qn}{cs.SEPARATOR_DOT}{member}"
        ):
            if (kind := self.function_registry.get(target_qn)) is not None:
                return kind, target_qn
        return None

    def _resolve_self_member_alias(
        self,
        call_name: str,
        class_context: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[bool, tuple[str, str] | None]:
        # The receiver is the enclosing class, so walk its MRO to the first
        # class whose body binds the name. A real def there leaves the call to
        # the usual resolution. A value (`run = None`) found first hides
        # whatever a base binds, so the call reaches nothing of the base's,
        # nor the same-named function a later name-based stage would find.
        receiver, _, member = call_name.partition(cs.SEPARATOR_DOT)
        if (
            receiver not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
            or not member
            or cs.SEPARATOR_DOT in member
            # A method that rebinds the receiver (`self = Other()`) has typed
            # it; the usual resolution follows that type.
            or (local_var_types and receiver in local_var_types)
        ):
            return False, None
        order = self._binding_aware_mro(class_context, member) or [
            self._follow_reexports(class_qn) for class_qn in self._mro(class_context)
        ]
        hidden = False
        for owner in order:
            if alias := self._member_alias(owner, member):
                return True, None if hidden else alias
            if f"{owner}{cs.SEPARATOR_DOT}{member}" in self.function_registry:
                return hidden, None
            if self.function_registry.binds_non_method(owner, member):
                hidden = True
        return hidden, None

    def member_alias_fanout(self, callee_qn: str, member: str) -> list[str]:
        """Every method a call written as `.member` may run, given `callee_qn`.

        A call resolved through a class-body alias lands on one method the
        alias names; a conditional alias (`__call__ = _a if X else _b`)
        names several and picks one at class creation, so each is a
        candidate. A call that bound to the member's own `def` gets the
        alias's methods instead when a later alias replaced that `def`. Any
        other callee is returned alone.
        """
        owner, _, leaf = callee_qn.rpartition(cs.SEPARATOR_DOT)
        if not owner:
            return [callee_qn]
        targets = [
            qn
            for qn in self.function_registry.member_alias_targets(
                f"{owner}{cs.SEPARATOR_DOT}{member}"
            )
            if qn in self.function_registry
        ]
        if callee_qn in targets or (targets and leaf == member):
            return targets
        return [callee_qn]

    def instance_call_targets(
        self,
        callee: str,
        is_call_result: bool,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> set[tuple[str, str]]:
        return self._python.instance_call_targets(
            callee, is_call_result, module_qn, local_var_types
        )

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
            queue.extend(self.facts.class_inheritance.get(current, ()))
        return results

    def _subclass_map(self) -> dict[str, set[str]]:
        if self._subclass_map_cache is None:
            mapping: dict[str, set[str]] = defaultdict(set)
            for subclass_qn, bases in self.facts.class_inheritance.items():
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

    def _binding_aware_mro(self, class_qn: str, member: str) -> list[str] | None:
        """class_qn's C3 MRO when a class body in it binds `member` by assignment.

        A value (`run = None`) or an alias (`run = _impl`) is recorded only
        for a Python class body, and Python looks either up in C3 order:
        `class C(A, B)` with `A(Base)` reaches Base's `run` before B's. Every
        other lookup keeps the breadth-first walk it always had (None).
        """
        registry = self.function_registry
        holders = registry.non_method_holders(member) | registry.alias_holders(member)
        if not holders:
            return None
        order = self._c3_mro(class_qn)
        return None if holders.isdisjoint(order) else order

    def _c3_mro(self, class_qn: str) -> list[str]:
        # Python's linearisation (C3). A hierarchy it rejects (a cycle, or
        # bases in an order no class could have) cannot have been defined,
        # so the breadth-first order stands in for it.
        memo: dict[str, list[str] | None] = {}

        def linearise(cls: str, active: frozenset[str]) -> list[str] | None:
            if cls in memo:
                return memo[cls]
            if cls in active:
                return None
            bases = [
                self._follow_reexports(base)
                for base in self.facts.class_inheritance.get(cls, [])
            ]
            sequences: list[list[str]] = []
            for base in bases:
                if (base_order := linearise(base, active | {cls})) is None:
                    return None
                sequences.append(list(base_order))
            sequences.append(bases)
            if (order := self._c3_merge(cls, sequences)) is not None:
                memo[cls] = order
            return order

        return linearise(class_qn, frozenset()) or [
            self._follow_reexports(qn) for qn in self._mro(class_qn)
        ]

    @staticmethod
    def _c3_merge(class_qn: str, sequences: list[list[str]]) -> list[str] | None:
        order = [class_qn]
        while sequences := [seq for seq in sequences if seq]:
            head = next(
                (
                    seq[0]
                    for seq in sequences
                    if not any(seq[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if head is None:
                return None
            order.append(head)
            sequences = [seq[1:] if seq[0] == head else seq for seq in sequences]
        return order

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str, own_members: bool = True
    ) -> tuple[str, str] | None:
        # Every caller has already looked for a def of the name on class_qn
        # itself. A class-body alias there (`run = _plain`, issue #2620) is
        # still the class's own member, and a value there (`run = None`) hides
        # every base's `run`, the way attribute lookup stops at the first
        # class that binds the name. `super()` starts above the class, so it
        # skips both.
        if own_members:
            if alias := self._member_alias(class_qn, method_name):
                return alias
            if self.function_registry.binds_non_method(class_qn, method_name):
                return None
        if found := self._go.go_variant_method(class_qn, method_name):
            return found
        if class_qn not in self.facts.class_inheritance:
            return None
        # Which class binds the name first decides whether a value hides
        # it, and that order is the C3 MRO, not this walk's breadth-first
        # order: `class C(A, B)` reaches a `run` that A inherits before B's
        # `run = None` or `run = _impl`.
        if (order := self._binding_aware_mro(class_qn, method_name)) is not None:
            return self._resolve_inherited_along_mro(order, method_name)
        return self._resolve_inherited_breadth_first(class_qn, method_name)

    def _resolve_inherited_along_mro(
        self, order: list[str], method_name: str
    ) -> tuple[str, str] | None:
        for owner in order[1:]:
            if alias := self._member_alias(owner, method_name):
                return alias
            owner_method_qn = f"{owner}{cs.SEPARATOR_DOT}{method_name}"
            if owner_method_qn in self.function_registry:
                return self.function_registry[owner_method_qn], owner_method_qn
            if self.function_registry.binds_non_method(owner, method_name):
                return None
        return None

    def _resolve_inherited_breadth_first(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        bfs_queue = deque(
            self._go.with_go_variants(self.facts.class_inheritance.get(class_qn, []))
        )
        visited = set(bfs_queue)

        while bfs_queue:
            # Base classes are recorded by the name the subclass imported, which
            # may be a package re-export (class_ingest.ClassIngest) rather than
            # the real definition (class_ingest.ingest.ClassIngest); follow the
            # re-export so the inherited method qn matches the registry.
            parent_class_qn = self._follow_reexports(bfs_queue.popleft())
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if alias := self._member_alias(parent_class_qn, method_name):
                return alias
            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            if parent_class_qn in self.facts.class_inheritance:
                for grandparent_qn in self._go.with_go_variants(
                    self.facts.class_inheritance[parent_class_qn]
                ):
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
        candidate_parts = _qn_parts(candidate_qn)
        candidate_len = len(candidate_parts)
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
        while type_name in self.facts.type_aliases and type_name not in seen:
            seen.add(type_name)
            type_name = self.facts.type_aliases[type_name]
        return type_name

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        # A Rust name the file imports from an external crate is that
        # crate's type. The lookup below skips an unregistered import target
        # and searches first-party types by simple name, which bound
        # `use regex_syntax::Parser;` to whichever `Parser` the repo holds
        # (issue #2622).
        if self._rust.rust_file_use_is_external(class_name, module_qn):
            return None
        # Call resolution runs in Pass 3, after every definition pass, so a
        # class qn missing from the registry can never be a real node;
        # require registration so an import-map module entry (a C++ header
        # stem shadowing its class name) cannot mask the real class.
        if (
            package_type := self._go.go_package_type(class_name, module_qn)
        ) is not None:
            return package_type
        resolved = resolve_class_name(
            self._dealias_type(class_name),
            module_qn,
            self.import_processor,
            self.function_registry,
            require_registered=True,
        )
        if resolved is None or self._rust.rust_crate_reaches(module_qn, resolved):
            return resolved
        # Only the last step, the search by simple name, can land in a crate
        # the caller cannot depend on; search again among those it can.
        return self._rust.rust_reachable_type(class_name, module_qn)

    def resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        caller_qn: str | None = None,
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = self._python.redirect_protocol_method(
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

    def resolve_go_call_site(
        self,
        call_node: Node,
        module_qn: str,
    ) -> tuple[str, str] | None:
        return self.type_inference.go_type_inference.resolve_go_call_site(
            call_node, module_qn
        )

    def resolve_java_call_site(
        self,
        call_node: Node,
        module_qn: str,
    ) -> tuple[str, str] | None:
        return self.type_inference.java_type_inference.resolve_java_call_site(
            call_node, module_qn
        )

    def resolve_python_call_site(
        self, call_node: Node, module_qn: str
    ) -> tuple[str, str] | None:
        return self._python.resolve_python_call_site(call_node, module_qn)
