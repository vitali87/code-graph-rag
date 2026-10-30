"""Rust's share of the name-based callee resolution: what a path, a `use` in
an inner scope, an initializer block's items and an inline `mod` bind a Rust
call to, which impl block owns a method, and which crates a call can reach.

`CallResolver` owns what every language shares (the order the stages run in,
the resolution cache and the verdict each answer carries, the simple-name
fallback) and hands Rust's part to the `RustResolver` it builds."""

from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionRegistryTrieProtocol
from ..definition_facts import DefinitionFacts
from . import utils as rs_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver
    from ..call_site import CallSite
    from ..import_processor import ImportProcessor
    from ..type_inference import TypeInferenceEngine


# One initializer block's scope record, as held per module by
# `RustImporter.rust_block_scope_imports`: (start, end, imports,
# mod_holes, fn_holes, item_scopes).
_RustBlockScope = tuple[
    int,
    int,
    dict[str, str],
    list[tuple[int, int]],
    list[tuple[int, int]],
    list[tuple[int, int, dict[str, tuple[int, int]]]],
]

# Node labels a Rust receiver type name may resolve to: a struct (Class), an
# enum, a type alias, or a trait (Interface, when the receiver is typed to a
# `dyn`/`impl` trait or a trait-returning factory); all can carry methods.
_RS_TYPE_NODE_TYPES = frozenset(
    {cs.NodeLabel.CLASS, cs.NodeLabel.ENUM, cs.NodeLabel.TYPE, cs.NodeLabel.INTERFACE}
)

# Registry kinds a Rust `Type::f()` qualifier may name and read methods off:
# the type alias of the receiver set is missing because its target, which
# owns the methods, is not recorded.
_RS_OWNER_NODE_TYPES = frozenset(
    {cs.NodeLabel.CLASS, cs.NodeLabel.ENUM, cs.NodeLabel.UNION, cs.NodeLabel.INTERFACE}
)


def _rust_assoc_path(call: CallSite) -> tuple[list[str], str] | None:
    """A Rust `Type::f` call's object path and item, or None for any other shape.

    Turbofish arguments (`Vec::<u8>::new`) drop out of the path. A dotted
    receiver is a method call, and a `<T as Trait>::f` qualified path does
    not name its owner in its first segment, so neither qualifies.
    """
    name = call.call_name
    if (
        call.language != cs.SupportedLanguage.RUST
        or cs.SEPARATOR_DOUBLE_COLON not in name
        or cs.SEPARATOR_DOT in name
        or name.startswith(cs.CHAR_ANGLE_OPEN)
    ):
        return None
    segments = [
        segment
        for segment in _drop_angle_groups(name).split(cs.SEPARATOR_DOUBLE_COLON)
        if segment
    ]
    if len(segments) < 2:
        return None
    return segments[:-1], segments[-1]


def _drop_angle_groups(text: str) -> str:
    if cs.CHAR_ANGLE_OPEN not in text:
        return text
    kept: list[str] = []
    depth = 0
    for char in text:
        if char == cs.CHAR_ANGLE_OPEN:
            depth += 1
        elif char == cs.CHAR_ANGLE_CLOSE:
            depth = max(0, depth - 1)
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def rust_caller(
    language: cs.SupportedLanguage | None, caller_qn: str | None
) -> str | None:
    # The caller whose inner-scope `use`s bind a Rust type name, or None for
    # any other language, whose scopes those maps never describe.
    return caller_qn if language == cs.SupportedLanguage.RUST else None


def _rust_enclosing_scopes(module_qn: str, caller_qn: str | None) -> list[str]:
    # The caller's qn scopes strictly between it and the file module,
    # innermost first (inline mod chains; impl targets appear too,
    # harmlessly, since mod-level use storage keys mirror them).
    if not caller_qn or not caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}"):
        return []
    scopes = []
    scope = caller_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
    while len(scope) > len(module_qn):
        scopes.append(scope)
        scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
    return scopes


def _rust_module_sees(module_qn: str, root: str) -> bool:
    # An item visible within `root` is visible to it and every module
    # below it. A crate entry file (lib.rs, main.rs) is the parent of
    # the modules beside it, which its own qn does not prefix, so its
    # directory stands in for it there; but another crate's entry beside
    # it (main.rs beside lib.rs) and a binary under src/bin/ are crates
    # of their own, which see none of it (#2599).
    if module_qn == root or module_qn.startswith(f"{root}{cs.SEPARATOR_DOT}"):
        return True
    parent, _, stem = root.rpartition(cs.SEPARATOR_DOT)
    if stem not in cs.RS_ENTRY_STEMS or not module_qn.startswith(
        f"{parent}{cs.SEPARATOR_DOT}"
    ):
        return False
    head = module_qn[len(parent) + 1 :].split(cs.SEPARATOR_DOT, 1)[0]
    return head not in cs.RS_ENTRY_STEMS and head != cs.RS_BIN_DIR


def _rust_block_serves_site(block: _RustBlockScope, name: str, call_point: int) -> bool:
    start, end, imports, mod_holes, _fn_holes, item_scopes = block
    if not (start <= call_point < end) or name not in imports:
        return False
    if any(s <= call_point < e for s, e in mod_holes):
        return False
    return not any(s <= call_point < e and name in items for s, e, items in item_scopes)


class RustResolver:
    """Binds Rust calls by Rust's own scoping and path rules.

    Built and owned by `CallResolver`. The registry, the import processor
    (whose `RustImporter` holds every `use`, block item and Cargo fact read
    here), the type inference engine and the definition facts are the
    resolver's own, held by reference and read as they stand when a call
    resolves: the earlier passes fill them before the call pass runs. A Rust
    stage hands a narrowed question to, or falls back on, the resolution
    every language shares on that resolver, and sets its `verdict` with the
    method an owner lookup picks."""

    __slots__ = (
        "_host",
        "function_registry",
        "import_processor",
        "type_inference",
        "facts",
    )

    def __init__(
        self,
        host: CallResolver,
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

    def rust_glob_imported_type(self, name: str, module_qn: str) -> str | None:
        # A Rust type the module brings in by a glob (`use crate::cmd::*;`)
        # has no import-map key of its own. Look it up in the module's
        # namespace the way rustc does, globs and their re-exports included,
        # before the bare-name search can pick a same-named type from
        # anywhere (issue #2542).
        if self._host.module_language(module_qn) != cs.SupportedLanguage.RUST:
            return None
        return self._rust_scope_type(f"{module_qn}{cs.SEPARATOR_DOT}{name}")

    def rust_glob_reexported_type(self, qn: str) -> str | None:
        # A Rust glob re-export (`pub use inner::*;`) binds no name the
        # re-export walk can follow, so a type exported through one stayed a
        # dead qn and every method called on it lost its edge (issue #2542).
        if self._host.module_language(qn) != cs.SupportedLanguage.RUST:
            return None
        return self._rust_scope_type(qn)

    def _rust_scope_type(self, qn: str) -> str | None:
        hit = self._follow_rust_scope_target(qn)
        return hit[1] if hit is not None and hit[0] in _RS_TYPE_NODE_TYPES else None

    def rust_scoped_local_types(
        self,
        local_var_types: dict[str, str] | None,
        module_qn: str,
        caller_qn: str | None,
        language: cs.SupportedLanguage | None,
    ) -> dict[str, str] | None:
        # A receiver's recorded type is the bare name its binding spells
        # (`b: Builder`, or the `Builder` that `Builder::new()` returns), and
        # every probe resolves a bare type name through the FILE's bindings.
        # A `use` in the caller's own body or enclosing inline mod rebinds
        # that name for this caller alone, so `b.build()` in
        # `mod tests { use my_beta::Builder; }` reached the crate's own
        # `Builder` (issue #2622). A declared module path (`x: self::Builder`)
        # is read through the file module too, which an inline mod's
        # `self::`, `super::` and bare paths do not count from. Rebinding the
        # map once covers them all.
        if (
            language != cs.SupportedLanguage.RUST
            or not local_var_types
            or not caller_qn
        ):
            return local_var_types
        scoped: dict[str, str] | None = None
        for name, type_name in local_var_types.items():
            if cs.SEPARATOR_DOT in type_name:
                continue
            if cs.SEPARATOR_DOUBLE_COLON in type_name:
                bound = self._rust_inline_path_type(type_name, module_qn, caller_qn)
            else:
                bound = self.rust_scoped_type(type_name, module_qn, caller_qn)
            if bound is None:
                continue
            if scoped is None:
                scoped = dict(local_var_types)
            scoped[name] = bound
        return local_var_types if scoped is None else scoped

    def _rust_inline_path_type(
        self, path: str, module_qn: str, caller_qn: str
    ) -> str | None:
        """The type a module path declared in an inline `mod` names there.

        `self::` and `super::` count from the innermost inline mod around the
        caller, and a bare head names that mod's own child module or `use`
        first. Read through the file module instead, `x: self::Builder` in
        `mod inner` bound the file's `Builder` and `super::Builder` in
        `mod inner { mod deeper {..} }` climbed past `inner` (PR #2791
        review). A `self::`/`super::` path naming no type there names none
        of the file's either, so it binds the unresolvable sentinel. None
        for a caller at file level, a `crate::` path, and a bare path the mod
        does not resolve: the file's reading then decides, as before.
        """
        effective = self.facts.rust_function_modules.get(caller_qn)
        if effective is None:
            # No ingest record (a rehydrated definition): the innermost
            # declared mod around the caller stands in for it.
            mods = self._rust_enclosing_mod_scopes(module_qn, caller_qn)
            effective = mods[0] if mods else module_qn
        head = path.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        if effective == module_qn or head == cs.RUST_CRATE_KEYWORD:
            return None
        relative = head in (cs.KEYWORD_SELF, cs.KEYWORD_SUPER)
        hit = self._rust_module_path_type(path, effective)
        if hit is None and relative:
            hit = self._rust_relative_use_path_type(path, effective)
        if hit is None and not relative:
            # A child mod written inline has no file and no import-map key
            # of its own, so only its qn under the mod finds it.
            hit = self._rust_module_path_type(
                f"{cs.KEYWORD_SELF}{cs.SEPARATOR_DOUBLE_COLON}{path}", effective
            )
        if hit is None and relative:
            return cs.RUST_UNRESOLVABLE_QN
        return hit

    def _rust_relative_use_path_type(self, path: str, effective: str) -> str | None:
        """The type a `self::`/`super::` path names through `use` bindings.

        The relative rewrite attaches every segment as a child module, so a
        segment the named mod binds by `use` led nowhere: `self::alias::X`
        after `use crate::a as alias;` lost its edge (PR #2791). Each
        segment is read as the current mod's child module first, then as its
        `use` binding; a binding that leaves the repo names no first-party
        type, so the walk stops with None.
        """
        parts = path.split(cs.SEPARATOR_DOUBLE_COLON)
        depth = 0
        while depth < len(parts) and parts[depth] == cs.KEYWORD_SUPER:
            depth += 1
        if depth:
            current: str | None = self.import_processor.rust.rust_super_base(
                effective, depth
            )
            rest = parts[depth:]
        else:
            current, rest = effective, parts[1:]
        for segment in rest[:-1]:
            if current is None:
                return None
            current = self._rust_scope_module(current, segment)
        if current is None or not rest:
            return None
        hit = self._follow_rust_scope_target(f"{current}{cs.SEPARATOR_DOT}{rest[-1]}")
        return hit[1] if hit is not None and hit[0] in _RS_TYPE_NODE_TYPES else None

    def _rust_scope_module(self, scope: str, segment: str) -> str | None:
        # The module `segment` names inside `scope`: a child module, or the
        # module a `use` there binds it to (`{self}` imports first, as they
        # name a module even where a value took the shared slot, #1054).
        child = f"{scope}{cs.SEPARATOR_DOT}{segment}"
        if (
            child in self.facts.declared_module_qns
            or child in self.type_inference.module_qn_to_file_path
            or child in self.import_processor.import_mapping
        ):
            return child
        imports = self.import_processor
        mapped = imports.rust.rust_self_module_imports.get(scope, {}).get(
            segment
        ) or imports.import_mapping.get(scope, {}).get(segment)
        if mapped is None or mapped == cs.RUST_UNRESOLVABLE_QN:
            return None
        return self._rust_local_qn(mapped, scope)

    def resolve_rust_block_scope(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn, caller_qn = call.call_name, call.module_qn, call.caller_qn
        # A Rust call sited inside a const/static initializer block binds
        # the block's own use before ANY other probe, including the
        # enclosing-scope and same-module ones that run after it: a use
        # shadows outer items for the remainder of its block (rustc-verified), so even
        # the file's own same-named item loses to it. A nested fn's own
        # body use still outranks the block. Ahead of all of it stands an
        # item the call's own block declares, which every use loses to;
        # that probe belongs HERE rather than with the scope walk, since
        # a module-level initializer's caller qn is the module itself and
        # never reaches the walk (issues #1026 and #1061). A `::`-qualified
        # first segment
        # bound by the block (`T::assoc()` under `use crate::beta::T`)
        # resolves through a map holding just that binding.
        if not (
            call.language == cs.SupportedLanguage.RUST
            and caller_qn
            and cs.SEPARATOR_DOT not in call_name
        ):
            return False, None
        if cs.SEPARATOR_DOUBLE_COLON not in call_name and (
            item_qn := self._rust_block_item_at(module_qn, call_name, call.call_point)
        ):
            return True, (self.function_registry[item_qn], item_qn)
        first = call_name.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        block_hit = self._rust_block_import_at(module_qn, first, call.call_point)
        if not block_hit:
            return False, None
        block_target, defers = block_hit
        # A nested fn's own body use outranks the block for BOTH
        # shapes: the bare call and the `First::rest` path whose
        # first segment it binds (`use crate::delta::T; T::assoc()`).
        if defers and (
            strong := self.import_processor.rust.rust_fn_scope_imports.get(
                caller_qn, {}
            ).get(first)
        ):
            block_target = strong
        if first != call_name:
            return True, self._resolve_rust_block_path(call, first, block_target)
        return True, self._follow_rust_scope_target(block_target)

    def _resolve_rust_block_path(
        self, call: CallSite, first: str, block_target: str
    ) -> tuple[str, str] | None:
        # `T::assoc()` under the block's `use crate::beta::T`. The qualified
        # probe reads `T`'s own members alone, so a block written away from
        # `T`'s definition, or a trait default `T` inherits, is found the
        # way a scoped `use` finds it (PR #2791).
        result = self._host.try_resolve_qualified_call(
            call.call_name,
            {first: block_target},
            call.module_qn,
            call.local_var_types,
            call.language,
        )
        item = call.call_name.partition(cs.SEPARATOR_DOUBLE_COLON)[2]
        if result is not None or cs.SEPARATOR_DOUBLE_COLON in item:
            return result
        bound = self._rust_scoped_use_type(block_target, call.module_qn, call.module_qn)
        if bound is None:
            return None
        return self._rust_bound_type_method(bound, item, first, call)

    def rust_caller_dependent(self, call: CallSite) -> bool:
        # A Rust caller nested below the file module (an inline `mod` block)
        # or holding function-body `use` declarations of its own resolves
        # caller-dependently: its scope's imports shadow file items, so the
        # file-keyed cache would serve it another caller's answer. A file
        # holding initializer-block uses resolves SITE-dependently (the
        # block's use answers only calls inside its span), so no caller in
        # it may share cached answers either.
        module_qn, caller_qn = call.module_qn, call.caller_qn
        if call.language != cs.SupportedLanguage.RUST or not caller_qn:
            return False
        rust = self.import_processor.rust
        return (
            module_qn in rust.rust_block_scope_imports
            or module_qn in rust.rust_block_items
            or (
                caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
                and (
                    cs.SEPARATOR_DOT in caller_qn[len(module_qn) + 1 :]
                    or caller_qn in rust.rust_fn_scope_imports
                )
            )
        )

    def resolve_rust_inline_scope(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # A Rust caller INSIDE an inline `mod` block sees that module's own
        # items and imports first (its `use` shadows the enclosing file's
        # items for code within the block). Caller-dependent, so never
        # cached under the file-scoped key.
        if call.language != cs.SupportedLanguage.RUST or not call.caller_qn:
            return False, None
        scoped = self._try_resolve_rust_inline_scope(
            call.call_name, call.module_qn, call.caller_qn, call.call_point
        )
        if scoped is not None:
            return True, scoped[0]
        return self._resolve_rust_scoped_type_path(call)

    def _resolve_rust_scoped_type_path(
        self, call: CallSite
    ) -> tuple[bool, tuple[str, str] | None]:
        # `Type::f()` where a `use` in the caller's own body or enclosing
        # inline mod names `Type`. Every later probe reads the FILE's
        # bindings and then searches types by simple name, so the crate's
        # own `Builder` answered for `mod tests { use my_beta::Builder; }`
        # and any first-party `Parser` for a function-scoped
        # `use regex_syntax::Parser;`, both as exact edges (issue #2622).
        # Caller-dependent, and the cache key already excludes such callers.
        type_name, sep, item = call.call_name.partition(cs.SEPARATOR_DOUBLE_COLON)
        if (
            not sep
            or not type_name
            or cs.SEPARATOR_DOUBLE_COLON in item
            or cs.SEPARATOR_DOT in call.call_name
        ):
            return False, None
        bound = self.rust_scoped_type(type_name, call.module_qn, call.caller_qn)
        if bound is None:
            return False, None
        return True, self._rust_bound_type_method(bound, item, type_name, call)

    def _rust_bound_type_method(
        self, bound: str, item: str, type_name: str, call: CallSite
    ) -> tuple[str, str] | None:
        """The method `item` of the one type a caller's scoped `use` binds.

        The type's own or inherited method first. Failing that, what the
        named-owner and external-path stages find for a type the file
        binds: a block written away from the type's definition, registered
        under `<its module>.<Type>`, or a trait default the type does not
        override; for an external `bound`, which holds no methods in the
        graph, only the crate's own impl on it can. A block counts only when
        its own written self path names `bound`: the path names this type,
        so no other type's same-named item is the target, the shadowed one
        the bare-name guess would bind included (PR #2791). None
        when the graph holds no such method (a derive or a macro made it).
        """
        if bound == cs.RUST_UNRESOLVABLE_QN:
            return None
        if bound in self.function_registry:
            if own := self._host.try_method_on_class(
                bound, item, cs.SEPARATOR_DOT, call.call_name, type_name, type_name
            ):
                return own
            owners, name = frozenset({bound}), bound.rpartition(cs.SEPARATOR_DOT)[2]
        else:
            owners = frozenset()
            name = bound.rpartition(cs.SEPARATOR_DOUBLE_COLON)[2]
        return self._rust_owned_method(
            item, call, owners, frozenset({name}), self_type=bound
        )

    def resolve_rust_prefixed_or_local(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        call_name, module_qn = call.call_name, call.module_qn
        # A `crate::`/`self::`/`super::` path says outright which module holds
        # the item, so it must not reach the probes that answer by NAME. Both
        # the same-module and the import probe ignore the prefix and hand back
        # the caller's OWN item of that name, which for `super::` is one module
        # too low (issue #1093). Undecided here means the path names nothing
        # first-party, so the ordinary fallbacks still run.
        if (
            call.language == cs.SupportedLanguage.RUST
            and cs.SEPARATOR_DOUBLE_COLON in call_name
            and call_name.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
            in (cs.RUST_CRATE_KEYWORD, cs.KEYWORD_SELF, cs.KEYWORD_SUPER)
        ):
            prefixed, decided = self._try_resolve_rust_module_qualified(
                call_name, module_qn, call.caller_qn
            )
            if decided:
                self._host.remember_cacheable(cache_key, prefixed)
                return True, prefixed

        # Rust name resolution prefers items defined in the module itself: a
        # glob import NEVER shadows a local item, and a MODULE-scoped named
        # use colliding with one is a compile error (E0255). A use inside a
        # function body DOES shadow module items, but it is stored under the
        # function's qn and already answered by the earlier scope walk, so at
        # this point same-module wins. Elsewhere imports shadow module-level
        # definitions.
        if call.language == cs.SupportedLanguage.RUST and (
            result := self._host.try_resolve_same_module(
                call_name, module_qn, call.call_point
            )
        ):
            self._host.remember_cacheable(cache_key, result)
            return True, result
        return False, None

    def resolve_rust_unresolvable_import(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        # A bare name imported from an unrepresentable #[path] module is
        # spoken for by that (unresolvable) import: the earlier precise probes
        # already declined, so drop it rather than let the trie rebind it to
        # a name-derived shadow file that merely shares the name (issue #1082).
        if (
            call.language == cs.SupportedLanguage.RUST
            and cs.SEPARATOR_DOT not in call.call_name
            and self._rust_name_maps_unresolvable(
                call.call_name, call.module_qn, call.caller_qn
            )
        ):
            self._host.remember_cacheable(cache_key, None)
            return True, None
        return False, None

    def _rust_name_maps_unresolvable(
        self, call_name: str, module_qn: str, caller_qn: str | None
    ) -> bool:
        import_mapping = self.import_processor.import_mapping
        for scope in (*_rust_enclosing_scopes(module_qn, caller_qn), module_qn):
            if import_mapping.get(scope, {}).get(call_name) == cs.RUST_UNRESOLVABLE_QN:
                return True
        return False

    def resolve_rust_external_path(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        # A Rust path whose head binds an item outside the project names
        # that crate's associated function: `String::new()`,
        # `Default::default()`, `std::mem::take(..)`, `fmt::Debug::fmt(..)`
        # under `use std::fmt`, `tokio::sync::Mutex::new(..)` with tokio in
        # the manifest. The graph holds none of them, and every later probe
        # answers by name alone: the type lookup with an out-of-scope
        # first-party `String`, the same-module guess with the file's own
        # `fn new`, the trie with any type's `new` (issue #2543). Only an
        # impl block ON the external type (`impl From<Foo> for u8`) can
        # hold a first-party target, so the path decides here.
        path = _rust_assoc_path(call)
        if path is None:
            return False, None
        object_path, item = path
        if not self._rust_head_is_external(
            object_path[0], call.module_qn, call.caller_qn
        ):
            return False, None
        result = self._rust_owned_method(
            item, call, frozenset(), frozenset({object_path[-1]})
        )
        self._host.remember_cacheable(cache_key, result)
        return True, result

    def resolve_rust_named_owner(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        # The last stop before the bare-name trie for a Rust call that names
        # the type owning its target: `Type::f()` or `Self::f()` on a
        # first-party type, or a method on a literal, whose type is a
        # primitive. Every precise probe has missed, so the target is a
        # method the graph cannot see (a derive's `Config::default()`,
        # clap's `Cli::parse()`) unless an impl block elsewhere or a trait
        # default supplies it; the trie would bind whatever type's
        # same-named method sat closest (issue #2543). A type alias, a
        # generic parameter or an external type the earlier probes did not
        # place is unknown rather than known, and keeps the trie.
        if path := _rust_assoc_path(call):
            object_path, item = path
            owners = self._rust_qualifier_types(
                object_path, call.module_qn, call.class_context
            )
            if owners is None:
                return False, None
            names = frozenset(qn.rpartition(cs.SEPARATOR_DOT)[2] for qn in owners)
            result = self._rust_owned_method(item, call, owners, names)
            # `Self` means the caller's impl target, so the answer is the
            # caller's, never the module's.
            if object_path != [cs.RS_SELF_TYPE]:
                self._host.remember_cacheable(cache_key, result)
            return True, result
        if call.language != cs.SupportedLanguage.RUST:
            return False, None
        receiver, _sep, item = call.call_name.rpartition(cs.SEPARATOR_DOT)
        if not receiver or (types := rs_utils.literal_receiver_types(receiver)) is None:
            return False, None
        result = self._rust_owned_method(item, call, frozenset(), types)
        self._host.remember_cacheable(cache_key, result)
        return True, result

    def _rust_qualifier_types(
        self, object_path: list[str], module_qn: str, class_context: str | None
    ) -> frozenset[str] | None:
        """The first-party type qns a Rust `Type::` qualifier names, or None.

        `Self` is the caller's impl target; when that impl block sits away
        from its type's definition its qn is unregistered, so the type it
        names is added. Any other qualifier counts only when it resolves to
        a registered struct, enum, union or trait: a type alias's target is
        not recorded, so its methods are not knowable from its qn.
        """
        qualifier = object_path[-1]
        owners: set[str] = set()
        if object_path == [cs.RS_SELF_TYPE]:
            if not class_context:
                return None
            owners.add(class_context)
            if class_context in self.function_registry:
                return frozenset(owners)
            qualifier = class_context.rpartition(cs.SEPARATOR_DOT)[2]
        type_qn = self._host.resolve_class_name(qualifier, module_qn)
        if type_qn is not None and self.function_registry.get(type_qn) in (
            _RS_OWNER_NODE_TYPES
        ):
            owners.add(type_qn)
        return frozenset(owners) or None

    def _rust_owned_method(
        self,
        item: str,
        call: CallSite,
        owners: frozenset[str],
        names: frozenset[str],
        self_type: str | None = None,
    ) -> tuple[str, str] | None:
        """The method `item` of the Rust type a call names, or None.

        A candidate counts when its owner is one of `owners`, an impl block
        on a type called one of `names` written in another module (which
        registers under an unregistered owner qn of that name), or a trait
        either of those implements, for its default method. Free functions
        never count: a path through a type or a method call names neither.
        With `self_type`, such a block counts only when its written self
        path names that type, which makes its method that type's own.
        """
        candidates = [
            qn
            for qn in self._host.nameable_candidates(
                self.function_registry.find_ending_with(item),
                call.module_qn,
                call.call_point,
            )
            if self.function_registry[qn] == cs.NodeLabel.METHOD.value
            and self._rust_method_owner_matches(qn, owners, names, self_type)
        ]
        if not candidates:
            logger.debug(ls.CALL_RUST_OWNER_UNRESOLVED, call_name=call.call_name)
            return None
        best = self._host.best_trie_candidate(candidates, call.module_qn)
        owner = best.rpartition(cs.SEPARATOR_DOT)[0]
        self._host.verdict = (
            cs.EdgeResolution.EXACT
            if owner in owners
            or (self_type is not None and owner not in self.function_registry)
            else cs.EdgeResolution.HEURISTIC
        )
        return self.function_registry[best], best

    def _rust_method_owner_matches(
        self,
        method_qn: str,
        owners: frozenset[str],
        names: frozenset[str],
        self_type: str | None = None,
    ) -> bool:
        owner = method_qn.rpartition(cs.SEPARATOR_DOT)[0]
        if self._rust_owner_is(owner, owners, names, method_qn, self_type):
            return True
        if self.function_registry.get(owner) != cs.NodeLabel.INTERFACE:
            return False
        # The read-back pairs too: a run that re-parsed only the caller has
        # `impl FloatErrors for u64` in them alone, and `u64::error_halfscale()`
        # lost the trait method a clean index binds (issue #2403).
        implementers = self.facts.interface_implementers.get(
            owner, set()
        ) | self.facts.rehydrated_interface_implementers.get(owner, set())
        return any(
            self._rust_owner_is(implementer, owners, names, self_type=self_type)
            for implementer in implementers
        )

    def _rust_owner_is(
        self,
        owner: str,
        owners: frozenset[str],
        names: frozenset[str],
        method_qn: str | None = None,
        self_type: str | None = None,
    ) -> bool:
        if owner in owners:
            return True
        if (
            owner in self.function_registry
            or owner.rpartition(cs.SEPARATOR_DOT)[2] not in names
        ):
            return False
        if self_type is not None:
            return self._rust_impl_names_type(owner, method_qn, self_type)
        # With no first-party owner the names are external or primitive
        # types, and the impl block must be on that type, not on the
        # crate's own type of the same name.
        return bool(owners) or self._rust_impl_on_external_type(owner, method_qn)

    def _rust_impl_names_type(
        self, owner: str, method_qn: str | None, type_qn: str
    ) -> bool:
        """Whether the impl block registered under `owner` is on `type_qn`.

        Its self type as written, read in the block's own module the way
        rustc reads it: `impl Builder` under `use crate::model::Builder;`
        and `impl crate::model::Builder` both name the model's type, while
        the same name bound to another type there names that one. A block
        not parsed this run (an unchanged file on an incremental run) has
        no recorded path, and its bare type name stands in for one.
        """
        impl_module, _sep, name = owner.rpartition(cs.SEPARATOR_DOT)
        paths = self.import_processor.rust.rust_impl_self_paths
        written = (
            (paths.get(method_qn) if method_qn else None) or paths.get(owner) or {name}
        )
        return any(
            self._rust_written_type(path, impl_module) == type_qn for path in written
        )

    def _rust_written_type(self, path: str, module_qn: str) -> str | None:
        # The type a path written in `module_qn` names: a first-party type's
        # qn, or the raw path of an external one. A bare name binds the
        # module's own type or its `use` or glob import, never a type found
        # by searching for the name.
        if cs.SEPARATOR_DOUBLE_COLON in path:
            if self.rust_path_is_external(path, module_qn):
                return path
            return self._rust_module_path_type(path, module_qn)
        target = self.import_processor.import_mapping.get(module_qn, {}).get(path)
        if target is not None:
            return self._rust_scoped_use_type(target, module_qn, module_qn)
        return self._rust_scope_type(f"{module_qn}{cs.SEPARATOR_DOT}{path}")

    def _rust_impl_on_external_type(
        self, owner: str, method_qn: str | None = None
    ) -> bool:
        """Whether an impl block registered under `owner` is on a type the
        crate does not define.

        An impl block registers under `<its module>.<Type>`, a qn nothing
        else holds, both for a type outside the crate (`impl From<Foo> for
        u8`, `impl Describe for std::string::String`) and for the crate's own
        type written away from its definition (`impl String { fn new() }`
        beside a `use crate::shadow::String`). The self type as written
        decides, read the way rustc reads it in the impl's own module: its
        first segment's binding there, the prelude for a bare `String`
        nothing rebinds, `crate::`/`self::`/`super::` for the crate's own
        (#2595). A crate type that merely shares the name decides
        nothing.
        """
        impl_module = owner.rpartition(cs.SEPARATOR_DOT)[0]
        paths = self.import_processor.rust.rust_impl_self_paths
        # The method's own block when known: two blocks of one module share
        # `owner` while naming different types.
        written = (paths.get(method_qn) if method_qn else None) or paths.get(owner)
        if not written:
            # Not parsed this run (an unchanged file on an incremental run):
            # no path to read, so a crate type of the name is taken to mean
            # its own, as rustc allows a foreign type only a trait impl.
            return self._rust_unwritten_impl_is_external(owner)
        return any(
            self._rust_head_is_external(
                path.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0], impl_module, None
            )
            for path in written
        )

    def _rust_unwritten_impl_is_external(self, owner: str) -> bool:
        impl_module, _sep, name = owner.rpartition(cs.SEPARATOR_DOT)
        if self._rust_head_binding(name, impl_module, None) is not None:
            return self._rust_head_is_external(name, impl_module, None)
        return not any(
            qn.rpartition(cs.SEPARATOR_DOT)[2] == name
            and self.function_registry.get(qn) in cs.RS_TYPE_SCOPE_LABELS
            and self._host.module_language(qn) == cs.SupportedLanguage.RUST
            for qn in self.function_registry.find_ending_with(name)
        )

    def _rust_head_is_external(
        self, head: str, module_qn: str, caller_qn: str | None
    ) -> bool:
        """Whether a Rust path's first segment binds an item outside the project.

        A binding in scope decides by where it points: a first-party item or
        a `use` of a local module says no, a `use` from a std crate or a
        manifest-proven external dependency says yes. Unbound, a prelude or
        primitive name is the language's own, and any other head names a
        crate, external on the same evidence.
        """
        binding = self._rust_head_binding(head, module_qn, caller_qn)
        path = head if binding is None else binding
        if self._rust_local_qn(path, module_qn) is not None:
            return False
        if binding is None and head in cs.RS_PRELUDE_NAMES:
            return True
        crate = path.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        # A pathless entry renamed onto a workspace member (`base-alias =
        # { version = "1", package = "my-base" }`) fetches the crate the repo
        # holds, so its head is first-party though no lib bears the name.
        return crate in cs.RS_STDLIB_CRATES or (
            not self.import_processor.rust.rust_head_is_repo_crate(crate)
            and not self.import_processor.rust.rust_head_is_member_dep(crate, module_qn)
            and self.import_processor.rust.rust_head_is_external_dep(crate, module_qn)
        )

    def _rust_head_binding(
        self, head: str, module_qn: str, caller_qn: str | None
    ) -> str | None:
        """What binds a Rust path head at a call site, innermost scope first.

        A `use` target (raw `::` path when external), or the qn of the
        first-party type a scope defines or glob-imports; None when nothing
        in scope binds the name. A path head is a type, trait or module,
        never a function, so a same-named function or method (`fmt` inside
        `impl Debug`) is no binding; a module holds no registry entry and
        is left to the caller's module check.
        """
        if self_module := self._rust_self_module_head(head, module_qn, caller_qn):
            return self_module[0]
        if caller_qn and (
            target := self.import_processor.rust.rust_fn_scope_imports.get(
                caller_qn, {}
            ).get(head)
        ):
            return target
        import_mapping = self.import_processor.import_mapping
        for scope in (*_rust_enclosing_scopes(module_qn, caller_qn), module_qn):
            local = f"{scope}{cs.SEPARATOR_DOT}{head}"
            if self.function_registry.get(local) in cs.RS_TYPE_SCOPE_LABELS:
                return local
            scope_map = import_mapping.get(scope, {})
            if (target := scope_map.get(head)) is not None:
                return target
            if glob_hit := self._rust_glob_type(scope_map, scope, head):
                return glob_hit
        return None

    def _rust_glob_type(
        self, scope_map: dict[str, str], scope: str, name: str
    ) -> str | None:
        for key, value in scope_map.items():
            if not key.startswith(cs.RS_WILDCARD_PREFIX):
                continue
            base = self._rust_local_qn(value, scope)
            if base is None:
                continue
            hit = self._follow_rust_scope_target(f"{base}{cs.SEPARATOR_DOT}{name}")
            if hit is not None and hit[0] in cs.RS_TYPE_SCOPE_LABELS:
                return hit[1]
        return None

    def _try_resolve_rust_inline_scope(
        self, call_name: str, module_qn: str, caller_qn: str, call_point: int | None
    ) -> tuple[tuple[str, str] | None] | None:
        """Resolve a bare call through the caller's enclosing Rust scopes.

        Probes the caller's OWN body uses first (kept in a separate map:
        `mod run` and `fn run` occupy different Rust namespaces but share
        one qn string, so the module-keyed map must never answer for the
        function itself), then walks the enclosing inline modules down to
        the file module, checking each scope's own items, the caller's
        weak entries (its enclosing mod's use fanned out per function, in
        case the mod's shared key was arbitrated to a twin), and the
        scope's import map. An item declared by a block the call sits
        inside outranks the body use and every one of those scopes, and
        binds by SPAN: the block item and a same-named item at module
        level both register flat in one module, so a name lookup would
        answer with whichever of them took the natural qn (issue #1026).
        Initializer-block uses are served earlier, by the site-gated
        `resolve_rust_block_scope`. Returns a 1-tuple
        so a deliberate drop (the scope imports the name from OUTSIDE the
        indexed first-party graph) is distinguishable from "no scope
        claims it".
        """
        if (
            cs.SEPARATOR_DOT in call_name
            or cs.SEPARATOR_DOUBLE_COLON in call_name
            or not caller_qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")
        ):
            return None
        target = self.import_processor.rust.rust_fn_scope_imports.get(
            caller_qn, {}
        ).get(call_name)
        if target is None:
            local, target = self._rust_walk_enclosing_scopes(
                call_name, module_qn, caller_qn, call_point
            )
            if local is not None:
                return (local,)
        if target is None:
            return None
        return (self._follow_rust_scope_target(target),)

    def _rust_walk_enclosing_scopes(
        self, call_name: str, module_qn: str, caller_qn: str, call_point: int | None
    ) -> tuple[tuple[str, str] | None, str | None]:
        """Walk the caller's inline mods outwards for an item or an import.

        Returns (resolved item, import target): the first scope owning the
        name as its own item answers outright, otherwise the innermost
        binding found on the way out is handed back for following.
        """
        import_mapping = self.import_processor.import_mapping
        weak = self.import_processor.rust.rust_fn_scope_mod_imports.get(
            caller_qn, {}
        ).get(call_name)
        scope = caller_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        while len(scope) > len(module_qn):
            # The scope segment may be an impl target whose method a
            # bare path can never name (issue #1011). A block-local item
            # that took the scope's flat qn answers to that name too, and
            # is out of scope wherever its block is not (issue #1061).
            if (
                local := self._host.scope_candidate(
                    scope, call_name, cs.SupportedLanguage.RUST
                )
            ) and not self.rust_block_item_hidden(local[1], module_qn, call_point):
                return local, None
            raw = import_mapping.get(scope, {}).get(call_name)
            target = weak if weak is not None else raw
            if target == cs.RUST_UNRESOLVABLE_QN:
                # An inner unrepresentable #[path] import still OWNS the
                # imported name and shadows any outer same-named item, so it
                # is a deliberate drop: stop the walk and hand the sentinel
                # back rather than letting an outer binding answer (#1082).
                return None, target
            if target is not None:
                return None, target
            scope = scope.rsplit(cs.SEPARATOR_DOT, 1)[0]
        return None, None

    def rust_block_item_hidden(
        self, qn: str, module_qn: str, call_point: int | None
    ) -> bool:
        """Is `qn` a Rust block-local item this call site sits outside of?

        A site with no recorded position cannot be placed against the
        block's span. Flow and taint passes resolve that way, so an
        unplaceable site keeps the item reachable rather than losing the
        edge outright.
        """
        if (
            call_point is None
            or qn not in self.import_processor.rust.rust_block_item_qns
        ):
            return False
        return not any(
            start <= call_point < end and qn in items.values()
            for start, end, items in self.import_processor.rust.rust_block_items.get(
                module_qn, ()
            )
        )

    def _rust_block_item_at(
        self, module_qn: str, name: str, call_point: int | None
    ) -> str | None:
        """The item `name` binds to in the innermost block holding this site.

        Innermost wins: nested blocks may each declare the name, and only
        the tightest one is in scope where the call is written. Keyed by
        FILE, not by caller: a fn declared inside the block is inside the
        block, so its own body binds the block's items too.
        """
        return rs_utils.block_item_at(
            self.import_processor.rust.rust_block_items.get(module_qn, ()),
            self.function_registry,
            name,
            call_point,
        )

    def resolve_rust_module_qualified(
        self, call: CallSite, cache_key: tuple[str, str, bool] | None
    ) -> tuple[bool, tuple[str, str] | None]:
        # A Rust `module::item` call names its function through the module
        # path, so it binds inside that module: a direct item, or the
        # module's own `use` re-export (ripgrep's flags::parse() through
        # flags/mod.rs). Runs after the import probes, so an aliased or
        # imported first segment keeps its precise resolution, and DECIDES
        # whenever the path names a real first-party module: an item the
        # module does not hold is a drop, never a bare-name trie guess at
        # an unrelated same-named function (issue #1009).
        if (
            call.language == cs.SupportedLanguage.RUST
            and cs.SEPARATOR_DOUBLE_COLON in call.call_name
        ):
            result, decided = self._try_resolve_rust_module_qualified(
                call.call_name, call.module_qn, call.caller_qn
            )
            if decided:
                self._host.remember_cacheable(cache_key, result)
                return True, result
        return False, None

    def _try_resolve_rust_module_qualified(
        self, call_name: str, module_qn: str, caller_qn: str | None
    ) -> tuple[tuple[str, str] | None, bool]:
        """Bind `module::item` inside the named module, or drop.

        Returns (result, decided). Undecided (False) means the path names
        no first-party module here and the ordinary fallbacks apply: a
        registered type's associated call, or an external path such as
        std::mem::replace. Decided with None means the module is real but
        holds no such item, so the edge is dropped rather than rebound by
        bare name.

        The first segment binds where Rust binds it: the caller's inner
        scopes first (an inline mod's own child module or use shadows the
        file's), then the file module's use (at most one binding per
        scope is legal, E0255/E0428), then the file's own submodule tree
        via the same relative-path machinery its use declarations resolve
        through (entry files attach submodules beside themselves via the
        declaring scan; other files nest them). A use binding the segment
        to a path outside the indexed project decides a drop when the
        path's head is a standard-library crate (external by
        construction) and stops the probe undecided otherwise: the name
        is spoken for either way, so the module tree must not claim it.
        """
        parts = call_name.split(cs.SEPARATOR_DOUBLE_COLON)
        object_path, item = parts[:-1], parts[-1]
        if not item or not all(object_path):
            return None, False
        head = object_path[0]
        if head in (cs.RUST_CRATE_KEYWORD, cs.KEYWORD_SELF, cs.KEYWORD_SUPER):
            return self._resolve_rust_prefixed_path(
                object_path, item, module_qn, caller_qn
            )
        # A module the caller bound with `use path::{self}` is a module, full
        # stop: decide through it before the type probe below, whose
        # name-based resolution would otherwise answer with a same-named
        # value and abandon the path (issue #1054).
        if binding := self._rust_self_module_head(head, module_qn, caller_qn):
            mapped, scope = binding
            return self._decide_rust_module_item(mapped, object_path[1:], item, scope)
        # A registered type as the first segment is an associated-function
        # call, owned by the class-resolution paths.
        if self._host.resolve_class_name(head, module_qn):
            return None, False
        import_mapping = self.import_processor.import_mapping
        for scope in _rust_enclosing_scopes(module_qn, caller_qn):
            base = cs.SEPARATOR_DOT.join([scope, *object_path])
            target = f"{base}{cs.SEPARATOR_DOT}{item}"
            if (hit := self.function_registry.get(target)) is not None:
                return (hit, target), True
            if base in import_mapping:
                # The scope's own child module holds the item only via
                # re-exports; it still shadows every outer same-named
                # module, so the decision is made HERE.
                return self._decide_rust_base(base, item)
            if mapped := import_mapping.get(scope, {}).get(head):
                return self._decide_rust_module_item(
                    mapped, object_path[1:], item, scope
                )
        if mapped := import_mapping.get(module_qn, {}).get(head):
            return self._decide_rust_module_item(
                mapped, object_path[1:], item, module_qn
            )
        base = self.import_processor.rust.rust_resolve_relative(
            module_qn, list(object_path), module_qn
        )
        return self._decide_rust_base(base, item)

    def _rust_self_module_head(
        self, head: str, module_qn: str, caller_qn: str | None
    ) -> tuple[str, str] | None:
        # A `use path::{self}` binding names a MODULE outright, so it answers
        # a qualified `head::item` even when the shared import map's one slot
        # went to a value of the same name, which Rust's separate namespaces
        # allow (issue #1054). Innermost scope first, like every other Rust
        # name lookup here.
        self_modules = self.import_processor.rust.rust_self_module_imports
        for scope in (*_rust_enclosing_scopes(module_qn, caller_qn), module_qn):
            if mapped := self_modules.get(scope, {}).get(head):
                return mapped, scope
        return None

    def _resolve_rust_prefixed_path(
        self,
        object_path: list[str],
        item: str,
        module_qn: str,
        caller_qn: str | None,
    ) -> tuple[tuple[str, str] | None, bool]:
        # crate:: is chain-independent; self::/super:: count from the caller's
        # enclosing MOD chain, which qn space cannot reconstruct -- an inline
        # mod and an impl block are both a bare segment. The ingest pass walked
        # the AST and recorded the answer, so read it (issue #1086). Without a
        # record (a rehydrated definition, a body-local fn), an impl block
        # still resolves from the file module exactly like a free function
        # (issue #1093); only a genuine declared mod chain declines, letting
        # the ordinary fallbacks keep their weaker but safe answer.
        effective_module = module_qn
        if object_path[0] != cs.RUST_CRATE_KEYWORD:
            recorded = (
                self.facts.rust_function_modules.get(caller_qn) if caller_qn else None
            )
            if recorded is not None:
                effective_module = recorded
            elif self._rust_enclosing_mod_scopes(module_qn, caller_qn):
                return None, False
        base = self.import_processor.rust.rewrite_rust_local_use_path(
            cs.SEPARATOR_DOUBLE_COLON.join(object_path), effective_module
        )
        if base == cs.RUST_UNRESOLVABLE_QN:
            # The path names a module backed by a file the qn scheme cannot
            # key (an unrepresentable #[path]): it has no referent, so drop it
            # rather than let a weaker fallback bind a name-derived shadow
            # file that merely sits where the name points (issue #1082).
            return None, True
        if cs.SEPARATOR_DOUBLE_COLON in base:
            return None, False
        return self._decide_rust_base(base, item)

    def _rust_enclosing_mod_scopes(
        self, module_qn: str, caller_qn: str | None
    ) -> list[str]:
        """Enclosing scopes that are inline `mod` blocks, not impl targets.

        `_rust_enclosing_scopes` deliberately keeps impl targets, whose use
        storage keys mirror mod scopes. A `super::` path cares which is which:
        an impl block adds no module level, so a method's `super::` counts
        from the file module's parent, while an inline mod's does not.

        Membership of the ingested inline-mod set is what decides it. Asking
        the type registry instead only proves a scope is NOT a registered
        type, which an impl target on a primitive or an unindexed foreign type
        also is not (`impl Trait for u8`), so that test silently read those
        impl blocks as modules and left `super::` one level short.
        """
        return [
            scope
            for scope in _rust_enclosing_scopes(module_qn, caller_qn)
            if scope in self.facts.declared_module_qns
        ]

    def rust_scoped_type(
        self, name: str, module_qn: str, caller_qn: str | None
    ) -> str | None:
        """What a bare Rust type name binds to in the caller's inner scopes.

        Innermost first, in the order `_rust_walk_enclosing_scopes` reads a
        bare call: the caller's own body `use`, then each scope between it
        and the file module, where a type the scope defines outranks the
        scope's `use`. A registered qn is the first-party type; any other
        string is a provably external path (or an unrepresentable module)
        that speaks for the name, so no first-party type may answer. None
        when no inner scope binds the name to a type: the file's own
        bindings then decide, as they always did.
        """
        if not caller_qn:
            return None
        effective = self.facts.rust_function_modules.get(caller_qn, module_qn)
        fn_scope = self.import_processor.rust.rust_fn_scope_imports.get(caller_qn, {})
        target = fn_scope.get(name)
        if target is not None:
            return self._rust_scoped_use_type(target, effective, module_qn)
        return self._rust_enclosing_scope_type(name, module_qn, caller_qn, effective)

    def _rust_enclosing_scope_type(
        self, name: str, module_qn: str, caller_qn: str, effective: str
    ) -> str | None:
        """Bind `name` in the scopes between the caller and its file module.

        Innermost first, a type the scope defines outranks the scope's
        `use`. The weak binding (an impure inline mod's own `use` fanned out
        to the functions its block declares) has mod-level precedence: it
        answers once the innermost scope defines no such type, read from
        the caller's effective module rather than that scope.
        """
        imports = self.import_processor
        weak = imports.rust.rust_fn_scope_mod_imports.get(caller_qn, {}).get(name)
        for scope in _rust_enclosing_scopes(module_qn, caller_qn):
            local = f"{scope}{cs.SEPARATOR_DOT}{name}"
            if self.function_registry.get(local) in _RS_TYPE_NODE_TYPES:
                return local
            if weak is not None:
                return self._rust_scoped_use_type(weak, effective, module_qn)
            raw = imports.import_mapping.get(scope, {}).get(name)
            if raw is not None:
                return self._rust_scoped_use_type(raw, scope, module_qn)
        return None

    def _rust_scoped_use_type(
        self, target: str, owner: str, module_qn: str
    ) -> str | None:
        """The type an inner scope's `use` target names, read from `owner`.

        An unrepresentable module and a provably external path speak for
        the name as they are; a first-party path counts only when it lands
        on a type.
        """
        if target == cs.RUST_UNRESOLVABLE_QN:
            return target
        resolved = self._rust_local_qn(target, owner)
        if resolved is None:
            return target if self.rust_path_is_external(target, module_qn) else None
        hit = self._follow_rust_scope_target(resolved)
        return hit[1] if hit is not None and hit[0] in _RS_TYPE_NODE_TYPES else None

    def rust_path_is_external(self, path: str, module_qn: str) -> bool:
        """Whether a raw Rust `use` path provably names an item outside the repo.

        Its head must be a toolchain crate, or a dependency the importing
        package's manifest fetches from outside the repo that is no crate
        the repo itself holds, directly or through the file's import of the
        head (`use std::io;` then `io::Result`). Any other raw head may
        still be first-party in a layout the import half does not rewrite,
        so it proves nothing.
        """
        if (
            cs.SEPARATOR_DOT in path
            or self._host.module_language(module_qn) != cs.SupportedLanguage.RUST
        ):
            return False
        head = path.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        if self._rust_crate_is_external(head, module_qn):
            return True
        mapped = self.import_processor.import_mapping.get(module_qn, {}).get(head)
        return (
            mapped is not None
            and cs.SEPARATOR_DOT not in mapped
            and self._rust_crate_is_external(
                mapped.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0], module_qn
            )
        )

    def _rust_crate_is_external(self, crate: str, module_qn: str) -> bool:
        rust = self.import_processor.rust
        return crate in cs.RS_STDLIB_CRATES or (
            not rust.rust_head_is_repo_crate(crate)
            and not rust.rust_head_is_member_dep(crate, module_qn)
            and rust.rust_head_is_external_dep(crate, module_qn)
        )

    def rust_file_use_is_external(self, name: str, module_qn: str) -> bool:
        target = self.import_processor.import_mapping.get(module_qn, {}).get(name)
        return target is not None and self.rust_path_is_external(target, module_qn)

    def _decide_rust_module_item(
        self, mapped: str, rest: list[str], item: str, owner: str
    ) -> tuple[tuple[str, str] | None, bool]:
        if mapped == cs.RUST_UNRESOLVABLE_QN:
            # A name imported from an unrepresentable #[path] module has no
            # referent: decided drop so no fallback binds a name-derived
            # shadow file (issue #1082).
            return None, True
        resolved = self._rust_local_qn(mapped, owner)
        if resolved is None:
            head = mapped.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
            if head in cs.RS_STDLIB_CRATES or (
                self.import_processor.rust.rust_head_is_external_dep(head, owner)
            ):
                # A standard-library head, or one the importer's own
                # manifest binds outside the repo (a registry version),
                # is external by construction: the name is spoken for,
                # so neither the module tree nor the trie may claim it
                # for a same-named first-party sibling (issue #1033).
                return None, True
            # Any other unresolved head may still be first-party in a
            # layout the import half does not cover (a nested workspace
            # root, a path dependency without a workspace, a #[path]
            # module): stay undecided so the ordinary fallbacks keep
            # their edges.
            return None, False
        return self._decide_rust_base(cs.SEPARATOR_DOT.join([resolved, *rest]), item)

    def _decide_rust_base(
        self, base: str, item: str
    ) -> tuple[tuple[str, str] | None, bool]:
        target = f"{base}{cs.SEPARATOR_DOT}{item}"
        result, unknown = self._follow_rust_scope_item(target, {target})
        if result is not None:
            return result, True
        if unknown:
            # A glob re-export whose base we cannot index may supply the
            # item; never a decided drop.
            return None, False
        if (
            base in self.type_inference.module_qn_to_file_path
            or base in self.import_processor.import_mapping
        ):
            return None, True
        return None, False

    def _rust_local_qn(self, value: str, owner: str) -> str | None:
        """A use-mapping value as a project qn, or None when external.

        crate::/super::/self:: values were rewritten to dotted qns at
        parse time, so a value still holding :: (or a single bare name)
        is a Rust 2018 uniform path: it binds locally when its head
        names a module in the owning scope (`use pool::connect;` beside
        `mod pool;`), else it names an external crate.
        """
        if cs.SEPARATOR_DOUBLE_COLON not in value and cs.SEPARATOR_DOT in value:
            return value
        segments = value.split(cs.SEPARATOR_DOUBLE_COLON)
        head_qn = self.import_processor.rust.rust_resolve_relative(
            owner, segments[:1], owner
        )
        if (
            head_qn in self.type_inference.module_qn_to_file_path
            or head_qn in self.import_processor.import_mapping
        ):
            return self.import_processor.rust.rust_resolve_relative(
                owner, segments, owner
            )
        return None

    def _follow_rust_scope_target(self, target: str) -> tuple[str, str] | None:
        return self._follow_rust_scope_item(target, {target})[0]

    def _follow_rust_scope_item(
        self, target: str, seen: set[str], via_glob_from: str | None = None
    ) -> tuple[tuple[str, str] | None, bool]:
        """Chase a candidate qn through re-export hops to a registered fn.

        A module's `pub use` maps the name to the re-exporting module's
        qn, not the defining one; hop through that module's own import
        map before concluding the target is outside the graph. Named
        hops win; failing those, each glob re-export (`pub use m::*;`)
        is expanded when its base is indexable. The second element is
        True when an unindexable glob base was met: the item may live
        behind it, so the caller must not turn the miss into a decided
        drop.

        `via_glob_from` is the module whose glob reached this candidate. A
        private `use` in the glob's base binds its name for the base and
        the modules below it alone, and shadows the base's own globs for
        that name, so a glob written anywhere else gets nothing under it.
        """
        while True:
            if (target_type := self.function_registry.get(target)) is not None:
                return (target_type, target), False
            owner, _, item = target.rpartition(cs.SEPARATOR_DOT)
            owner_map = self.import_processor.import_mapping.get(owner)
            if not owner_map:
                return None, False
            hidden = self._rust_names_hidden_from_glob(owner, via_glob_from)
            if (hop := owner_map.get(item)) is not None:
                if item in hidden:
                    return None, False
                resolved = self._rust_local_qn(hop, owner)
                if resolved is None or resolved in seen:
                    return None, False
                seen.add(resolved)
                target = resolved
                # A named `use` is a path the source spells out, which rustc
                # already checked is visible where it is written.
                via_glob_from = None
                continue
            return self._expand_rust_glob_hops(owner_map, owner, item, seen, hidden)

    def _rust_names_hidden_from_glob(
        self, owner: str, via_glob_from: str | None
    ) -> Collection[str]:
        # The names `owner` binds by a `use` whose visibility does not reach
        # the module whose glob reached `owner`.
        if via_glob_from is None:
            return frozenset()
        restricted = self.import_processor.rust.rust_restricted_use_names.get(owner)
        if not restricted:
            return frozenset()
        return frozenset(
            name
            for name, root in restricted.items()
            if not _rust_module_sees(via_glob_from, root)
        )

    def _expand_rust_glob_hops(
        self,
        owner_map: dict[str, str],
        owner: str,
        item: str,
        seen: set[str],
        hidden: Collection[str] = frozenset(),
    ) -> tuple[tuple[str, str] | None, bool]:
        unknown = False
        for key, value in owner_map.items():
            if not key.startswith(cs.RS_WILDCARD_PREFIX) or key in hidden:
                continue
            resolved = self._rust_local_qn(value, owner)
            if resolved is None:
                unknown = True
                continue
            candidate = f"{resolved}{cs.SEPARATOR_DOT}{item}"
            if candidate in seen:
                continue
            seen.add(candidate)
            result, sub_unknown = self._follow_rust_scope_item(
                candidate, seen, via_glob_from=owner
            )
            if result is not None:
                return result, False
            unknown = unknown or sub_unknown
        return None, unknown

    def _rust_block_import_at(
        self, module_qn: str, name: str, call_point: int | None
    ) -> tuple[str, bool] | None:
        """The innermost initializer-block use serving `name` at this site.

        Returns (resolved target, defers_to_fn), or None when no block
        serves it: the site lies outside every block span, a nested mod
        boundary intervenes (its members never see the block's use), or
        a containing nested block declares the name as its own direct
        item (the local item wins; the ordinary scope probes find it
        unless the enclosing fn's own body use shadows it there, issue
        #1026).
        defers_to_fn marks a site inside a nested fn, whose own body
        uses outrank the block's while a use-less fn still inherits it
        (rustc-verified).
        """
        if call_point is None:
            return None
        blocks = self.import_processor.rust.rust_block_scope_imports.get(module_qn)
        if not blocks:
            return None
        best_size: int | None = None
        result: tuple[str, bool] | None = None
        for block in blocks:
            if not _rust_block_serves_site(block, name, call_point):
                continue
            start, end, imports, _mod_holes, fn_holes, _item_scopes = block
            size = end - start
            if best_size is None or size < best_size:
                best_size = size
                result = (
                    imports[name],
                    any(s <= call_point < e for s, e in fn_holes),
                )
        return result

    def resolve_rust_class_qn(self, class_qn: str, module_qn: str | None = None) -> str:
        # A path into an external crate names that crate's type, which the
        # graph does not hold; a same-named first-party type is not it
        # (issue #2622). Kept raw, it resolves to no class, so the receiver
        # reads as external instead of binding the lookalike's methods.
        if module_qn is not None:
            if self.rust_path_is_external(class_qn, module_qn):
                return class_qn
            if (
                spelled := self._rust_module_path_type(class_qn, module_qn)
            ) is not None:
                return spelled
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
                and (module_qn is None or self.rust_crate_reaches(module_qn, qn))
            ),
            class_qn,
        )

    def _rust_module_path_type(self, path: str, module_qn: str) -> str | None:
        """The registered type a module path written in `module_qn` names.

        `crate::`, `self::`, `super::` and a workspace crate head rewrite the
        way a `use` does, a sibling module head resolves beside the module,
        and a head the file imports (`use crate::a;` then `a::Builder`)
        expands through its import. Matching the leaf by name instead let a
        receiver declared as `crate::a::Builder` bind whichever `Builder`
        came first (PR #2791). None when the path leads to no type.
        """
        imports = self.import_processor
        target: str | None = imports.rust.rewrite_rust_local_use_path(path, module_qn)
        if target is not None and cs.SEPARATOR_DOUBLE_COLON in target:
            target = self._rust_local_qn(path, module_qn)
        if target is None:
            head, _, rest = path.partition(cs.SEPARATOR_DOUBLE_COLON)
            mapped = imports.import_mapping.get(module_qn, {}).get(head)
            base = self._rust_local_qn(mapped, module_qn) if mapped else None
            if base is None:
                return None
            target = cs.SEPARATOR_DOT.join(
                [base, *rest.split(cs.SEPARATOR_DOUBLE_COLON)]
            )
        if target == cs.RUST_UNRESOLVABLE_QN:
            return None
        hit = self._follow_rust_scope_target(target)
        return hit[1] if hit is not None and hit[0] in _RS_TYPE_NODE_TYPES else None

    def infer_rust_assoc_base_type(
        self,
        base: str,
        module_qn: str,
        call_point: int | None = None,
        rust_caller: str | None = None,
    ) -> str | None:
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
        # A bare type name at a site inside an initializer block resolves
        # through the block's own use, not the file's (the receiver must
        # bind through the same scope its callee did).
        if cs.SEPARATOR_DOUBLE_COLON not in type_name and (
            block_hit := self._rust_block_import_at(module_qn, type_name, call_point)
        ):
            class_qn = (
                self._host.resolve_class_qn_from_type(
                    type_name, {type_name: block_hit[0]}, module_qn
                )
                or type_name
            )
        else:
            class_qn = self._host.chain_class_qn(type_name, module_qn, rust_caller)
        ret = self.type_inference.facts.method_return_types.get(
            f"{class_qn}{cs.SEPARATOR_DOT}{method}"
        )
        if (
            ret is not None
            and ret != type_name
            and ret == class_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        ):
            # `Fetch::new()` for `pub use inner::Get as Fetch;` returns `Get`,
            # the type the caller spelled `Fetch`. `Get` itself need not be in
            # scope here, and a bare-name search for it lands on whichever
            # same-named type comes first (issue #2542).
            return class_qn
        # The recorded return type is the bare source name; downstream
        # hops remap it through the FILE's imports, so at a block-gated
        # site qualify it through the block's binding here instead.
        if (
            ret is not None
            and cs.SEPARATOR_DOT not in ret
            and cs.SEPARATOR_DOUBLE_COLON not in ret
            and (ret_hit := self._rust_block_import_at(module_qn, ret, call_point))
        ):
            return (
                self._host.resolve_class_qn_from_type(ret, {ret: ret_hit[0]}, module_qn)
                or ret
            )
        return ret

    def rust_reachable_type(self, name: str, module_qn: str) -> str | None:
        matches = [
            qn
            for qn in self.function_registry.find_ending_with(name)
            if self.function_registry.get(qn) in _RS_TYPE_NODE_TYPES
            and self.rust_crate_reaches(module_qn, qn)
        ]
        own = [qn for qn in matches if qn.startswith(f"{module_qn}{cs.SEPARATOR_DOT}")]
        if own:
            return min(own, key=len)
        return matches[0] if matches else None

    def rust_lands_outside_crate(
        self,
        call_name: str,
        module_qn: str,
        qn: str,
        language: cs.SupportedLanguage | None,
    ) -> bool:
        # The probes filter their name-based candidates by crate already;
        # this catches what reached a crate by another name-based route (a
        # receiver typed by a recorded name, a match arm typed by its
        # variant's), since no Rust call can land outside the caller's
        # dependency closure, precise or not (issue #2622).
        if language != cs.SupportedLanguage.RUST or self.rust_crate_reaches(
            module_qn, qn
        ):
            return False
        logger.debug(ls.CALL_RUST_OUTSIDE_CRATE, call_name=call_name, qn=qn)
        return True

    def rust_crate_reaches(self, module_qn: str, target_qn: str) -> bool:
        # Rust to Rust only: every other language's candidates, and a module
        # of unknown language, keep their behaviour.
        if (
            self._host.module_language(module_qn) != cs.SupportedLanguage.RUST
            or self._host.module_language(target_qn) != cs.SupportedLanguage.RUST
        ):
            return True
        return self.import_processor.rust.rust_crate_reaches(module_qn, target_qn)
