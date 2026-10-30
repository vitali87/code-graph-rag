"""JavaScript's and TypeScript's share of the name-based callee resolution:
prototype siblings and member twins, the one visible class an untyped
receiver's member can be, re-export chains, builtins, and the static members,
`this` returns and constructions a call chain is typed through.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the hop-by-hop
chain typing) and hands JS/TS's part to the `JsTsResolver` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ... import constants as cs
from ...node_text import safe_decode_text
from ...types_defs import FunctionRegistryTrieProtocol
from ...utils import qn_markers
from ..call_site import CallSite
from ..resolver_host import ResolverHost
from . import utils as js_ts_utils

if TYPE_CHECKING:
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


# Chain bases whose value depends on the member they are written in.
_JS_THIS_RECEIVERS = frozenset({cs.TS_THIS, cs.KEYWORD_SUPER})
_JS_RECEIVER_MARKERS = frozenset({cs.JS_RECEIVER_INSTANCE, cs.JS_RECEIVER_CLASS})


def class_value_of(type_name: str) -> str | None:
    # The class qn a hop's value is the class itself of (`typeof C`), else
    # None for an instance type.
    if type_name.startswith(cs.JS_CLASS_VALUE_PREFIX):
        return type_name[len(cs.JS_CLASS_VALUE_PREFIX) :]
    return None


def bind_receiver(returned: str | None, receiver: str | None) -> str | None:
    # A `this` return is the class the call went through: an instance of
    # it, or the class itself. With no such class there is nothing to name.
    if returned == cs.JS_RECEIVER_INSTANCE:
        return receiver
    if returned == cs.JS_RECEIVER_CLASS:
        return f"{cs.JS_CLASS_VALUE_PREFIX}{receiver}" if receiver else None
    return returned


def function_prototype_builtin(method: str) -> tuple[str, str]:
    """The builtin `Function.prototype.<method>`: `bind`, `call` or `apply`."""
    return (
        cs.NodeLabel.FUNCTION,
        f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}Function{cs.SEPARATOR_PROTOTYPE}{method}",
    )


def resolve_builtin_call(call_name: str) -> tuple[str, str] | None:
    if call_name in cs.JS_BUILTIN_PATTERNS:
        return (cs.NodeLabel.FUNCTION, f"{cs.BUILTIN_PREFIX}.{call_name}")

    # `X.prototype.m.call(...)` runs `X.prototype.m`, so it is named before
    # the `.call` it also ends in: the platform's own method when X is a
    # platform type, else the name as written, which the call pass binds.
    if cs.SEPARATOR_PROTOTYPE in call_name and call_name.endswith(
        (cs.JS_SUFFIX_CALL, cs.JS_SUFFIX_APPLY)
    ):
        method = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
        if method.split(cs.SEPARATOR_DOT, 1)[0] in cs.JS_TS_GLOBAL_RECEIVER_TYPES:
            method = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}{method}"
        return (cs.NodeLabel.FUNCTION, method)

    for suffix, method in cs.JS_FUNCTION_PROTOTYPE_SUFFIXES.items():
        if call_name.endswith(suffix):
            return function_prototype_builtin(method)

    return None


class JsTsResolverHost(ResolverHost, Protocol):
    """What JS/TS resolution reads and calls on the `CallResolver` that builds
    it.

    A chain's first hop is resolved as that call written alone would be
    (`resolve_call`), and `verdict` says how surely it bound: a guessed
    callee types nothing after it. The rest is resolution every language
    shares, and the return type a hop hands the next one."""

    def resolve_call(self, call: CallSite) -> tuple[str, str] | None: ...

    def hop_return_type(
        self,
        callable_qn: str,
        language: cs.SupportedLanguage | None,
        static: bool = False,
        receiver: str | None = None,
    ) -> str | None: ...


class JsTsResolver:
    """Binds JS/TS calls by the language's prototype, module and class rules.

    Built and owned by `CallResolver`. The registry, the import processor
    (whose `JsTsImporter` holds the re-exports, default exports and package
    imports read here) and the type inference engine are the resolver's
    own, held by reference and read as they stand when a call resolves. The
    return-class memo is this resolver's: the watch pass clears it with the
    resolver's other caches."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "_js_return_class_cache",
    )

    def __init__(
        self,
        host: JsTsResolverHost,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
    ) -> None:
        self._host = host
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        # {JS/TS callable qn: the class a call to it returns}, read off its
        # declaration on first use (issue #2468).
        self._js_return_class_cache: dict[tuple[str, bool], str | None] = {}

    def reset_resolution_memos(self) -> None:
        # A re-parsed TS file may declare a different return.
        self._js_return_class_cache.clear()

    def resolve_js_prototype_sibling(
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
                twins.add((str(label), qn))
        return twins

    def resolve_js_member_call_unique(
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
        # A default import names `<module>.default`; the class the module
        # default-exports is what it makes visible (issue #3179).
        imported |= {
            self.import_processor.js_ts.follow_js_default_import(
                value,
                self.type_inference.module_qn_to_file_path,
                self.function_registry,
            )
            for value in imported
        }
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
        # A declaration that never runs (an interface or abstract member, issue
        # #2524) must not make its sole implementation ambiguous: the two share
        # the name by design. It is chosen only when nothing implements it.
        visible = [
            qn for qn in visible if not self._is_bodiless_declaration(qn)
        ] or visible
        if len(visible) == 1:
            return self.function_registry[visible[0]], visible[0]
        return None

    def _is_bodiless_declaration(self, qn: str) -> bool:
        # Interface ownership is read from the owner's label, which an
        # incremental run rehydrates for unchanged files; the abstract mark
        # exists only for members this run parsed.
        if self.function_registry.is_abstract(qn):
            return True
        owner_qn = qn.rpartition(cs.SEPARATOR_DOT)[0]
        return self.function_registry.get(owner_qn) == cs.NodeLabel.INTERFACE

    def js_member_head_is_external(self, member_name: str, module_qn: str) -> bool:
        """Whether a dotted JS/TS name's head is imported from a package.

        `<Prim.Item>` under `import * as Prim from "@radix-ui/react-accordion"`
        (or a default, named or `require` binding of a package) names a member
        of that package, which the project never indexes, so its last segment
        is no licence to bind a same-named first-party symbol (issue #2535).
        Only the import processor can tell a package from project code: a
        bare `react` resolves to the qn `react`, which a project named
        `react` or a repo-root `react.tsx` also spells, so the head's target
        qn is never compared with project paths. Relative, tsconfig-alias,
        workspace and aliased-scheme (deno `ext:`) imports are first-party
        and keep today's resolution.
        """
        head, sep, _member = member_name.partition(cs.SEPARATOR_DOT)
        if not sep:
            return False
        package_imports = self.import_processor.js_ts.js_ts_package_imports.get(
            module_qn
        )
        return bool(package_imports) and head in package_imports

    def try_resolve_js_reexport(
        self,
        call_name: str,
        import_map: dict[str, str],
        language: cs.SupportedLanguage | None,
        module_qn: str,
    ) -> tuple[str, str] | None:
        # `import { plus } from "./lib"` maps `plus` to `lib.plus`, where a
        # barrel registers nothing: its `export { add as plus } from` names
        # the definition, possibly through further barrels. Without this the
        # call fell to the name-only fallback, which found no `plus` at all,
        # or only guessed the right `add` (issue #2464).
        if language not in cs.JS_TS_LANGUAGES or call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        followed = self.import_processor.js_ts.follow_js_reexports(
            imported_qn,
            self.type_inference.module_qn_to_file_path,
            self.function_registry,
        )
        if followed == imported_qn:
            return None
        # The chain's end resolves as a direct import of it would, so a
        # CommonJS whole-module export there is still reached.
        return self._host.try_resolve_direct_import(
            call_name, {call_name: followed}, language, module_qn
        )

    def js_static_member(self, class_qn: str, method: str) -> tuple[str, int] | None:
        """`(member qn, line)` of the static `method` the class reaches: its
        own, else the nearest ancestor's. The walk stops at the first class
        declaring a static of that name, field or accessor included, which
        hides every parent's; one with no method body answers nothing.
        Instance members are a different namespace and never answer."""
        for owner in self._host.mro(class_qn):
            declared, line = self.type_inference.js_static_member(owner, method)
            if declared:
                return (
                    (f"{owner}{cs.SEPARATOR_DOT}{method}", line)
                    if line is not None
                    else None
                )
        return None

    def js_static_member_target(
        self, class_qn: str, method: str
    ) -> tuple[str, str] | None:
        # A static and an instance member of one name register as `m` and
        # `m@<line>`, the first in the source taking the plain name; the
        # static one's line says which it is.
        found = self.js_static_member(class_qn, method)
        if found is None:
            return None
        member_qn, line = found
        marked = {
            marker: variant
            for variant in self.function_registry.variants(member_qn)
            if (marker := qn_markers.marker_line(variant)) is not None
        }
        if line in marked:
            variant = marked[line]
        elif member_qn in self.function_registry and all(
            marker > line for marker in marked
        ):
            variant = member_qn
        else:
            return None
        return self.function_registry[variant], variant

    def js_return_class(self, callable_qn: str, static: bool) -> str | None:
        # Only a class registered under the qn the declaring module resolved
        # counts: a name that module could not place is not looked up again
        # from the caller, where a same-named class may answer.
        key = (callable_qn, static)
        if key in self._js_return_class_cache:
            return self._js_return_class_cache[key]
        returned = self.type_inference.js_callable_return_type(callable_qn, static)
        if returned in _JS_RECEIVER_MARKERS:
            self._js_return_class_cache[key] = returned
            return returned
        class_qn = (
            self._host.follow_reexports(returned)
            if returned and cs.SEPARATOR_DOT in returned
            else None
        )
        if class_qn is not None and self.function_registry.get(class_qn) != (
            cs.NodeLabel.CLASS
        ):
            class_qn = None
        self._js_return_class_cache[key] = class_qn
        return class_qn

    def fold_js_call_hop(
        self, parts: list[str], chain: CallSite
    ) -> tuple[str, list[str]] | None:
        # `TBox.create().with(1)`, `this.with(1).bump()`: the base is no typed
        # local, but its first hop binds the way that call written alone
        # does, in the caller `chain` is written in. Its return types the
        # rest of the chain (issue #2468).
        language = chain.language
        if language not in cs.JS_TS_LANGUAGES or len(parts) < 2:
            return None
        module_qn, local_var_types = chain.module_qn, chain.local_var_types
        call_point = chain.call_point
        base, hop = parts[0], parts[1]
        if (
            cs.CHAR_PAREN_OPEN in base
            or cs.CHAR_PAREN_OPEN not in hop
            or (local_var_types and base in local_var_types)
        ):
            return None
        # What `this` / `super` name is settled before anything resolves: an
        # object literal's or a plain function's own `this` is no class, so
        # neither the resolver nor the class-anchored fallback below may
        # answer for it with the class around it.
        is_this = base in _JS_THIS_RECEIVERS
        this_static = (
            self._js_this_names_class(module_qn, call_point) if is_this else False
        )
        if this_static is None:
            return None
        method = hop.split(cs.CHAR_PAREN_OPEN, 1)[0]
        # `D.make()` names a class, so it reaches the static `make` D has or
        # inherits along its `extends` chain, and a `this` that `make`
        # returns is D. `this` / `super` stand for the enclosing class; any
        # other base is a value, reaching an instance member.
        named = (
            None
            if is_this
            else self._named_class(base, module_qn, language, call_point)
        )
        callee_qn = self._first_hop_callee(
            base,
            method,
            named,
            chain._replace(
                call_name=f"{base}{cs.SEPARATOR_DOT}{method}", constructing=False
            ),
        )
        if callee_qn is None:
            return None
        receiver = chain.class_context if is_this else named
        static = this_static or named is not None
        returned = self._host.hop_return_type(callee_qn, language, static, receiver)
        return (returned, parts[1:]) if returned else None

    def _first_hop_callee(
        self, base: str, method: str, named: str | None, call: CallSite
    ) -> str | None:
        if named is not None:
            found = self.js_static_member(named, method)
            return found[0] if found else None
        return self._exact_first_hop(base, method, call)

    def _named_class(
        self,
        name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        call_point: int | None,
    ) -> str | None:
        # The class a base name is at the call: declared in this module or
        # imported, and not rebound by a parameter or local around the call.
        # A same-named class elsewhere in the project is never it.
        root = self._host.cached_module_root(module_qn)
        site = (
            root.named_descendant_for_byte_range(call_point, call_point)
            if root is not None and call_point is not None
            else None
        )
        if site is None or safe_decode_text(site) != name:
            return None
        class_qn = self.type_inference.js_type_inference.visible_class(
            site, name, module_qn, language
        )
        if class_qn is None:
            return None
        class_qn = self._host.follow_reexports(class_qn)
        return (
            class_qn
            if self.function_registry.get(class_qn) == cs.NodeLabel.CLASS
            else None
        )

    def _exact_first_hop(self, base: str, method: str, call: CallSite) -> str | None:
        # The callee of the chain's first hop, bound exactly or not at all: a
        # callee bound only by name is a guess, and typing the rest of the
        # chain from its return would build every later hop on that guess.
        resolved = self._host.resolve_call(call)
        if resolved is not None:
            return (
                resolved[1] if self._host.verdict == cs.EdgeResolution.EXACT else None
            )
        # `this.m()` in an arrow: name resolution climbs from the arrow to its
        # method, not the class, so the call processor anchors such a call on
        # the enclosing class instead (#971). The hop is typed the same way.
        if base != cs.TS_THIS or not call.class_context:
            return None
        own_qn = f"{call.class_context}{cs.SEPARATOR_DOT}{method}"
        if own_qn in self.function_registry:
            return own_qn
        inherited = self._host.resolve_inherited_method(call.class_context, method)
        return inherited[1] if inherited else None

    def _js_this_names_class(
        self, module_qn: str, call_point: int | None
    ) -> bool | None:
        # `this` / `super` at the call: the class inside a static member, an
        # instance inside an instance member, and neither (None) inside a
        # plain function or an object literal's method.
        root = self._host.cached_module_root(module_qn)
        if root is None or call_point is None:
            return None
        return js_ts_utils.this_names_class_at(root, call_point)

    def infer_js_construction_base_type(
        self,
        base: str,
        module_qn: str,
        language: cs.SupportedLanguage | None,
        call_point: int | None,
    ) -> str | None:
        # `new Box().bump()`, `(new Box()).bump()`: the base is neither a
        # local, an import nor a factory with a recorded return, so the chain
        # stayed untyped and the call bound nothing beside INSTANTIATES Box
        # (issue #2465). It types the way `const b = new Box()` types `b`, so
        # both spellings of one construction reach the same class. The
        # receiver starts where the call does; without that site in the
        # file's tree, a `Box` the caller binds itself cannot be ruled out.
        if language not in cs.JS_TS_LANGUAGES or call_point is None:
            return None
        root = self._host.cached_module_root(module_qn)
        if root is None:
            return None
        construction = js_ts_utils.construction_at(root, call_point, base)
        if construction is None:
            return None
        var_type = (
            self.type_inference.js_type_inference._infer_js_variable_type_from_value(
                construction, module_qn, language
            )
        )
        if not var_type:
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        return (
            self._host.resolve_class_qn_from_type(var_type, import_map, module_qn)
            or None
        )
