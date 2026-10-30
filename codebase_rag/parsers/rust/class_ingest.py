"""Rust's share of the class pass: the methods an `impl` block gives its
target type, the trait it implements and whether that trait is another
crate's, a re-exported parent's declaring module, a struct's field types,
and the inline `mod` items and bodyless `mod` declarations a file holds.

`ClassIngest` owns what every language shares (the scan of a file's class
captures, a class's node, members and relationships, and the deferred
INHERITS/IMPLEMENTS resolution) and hands Rust's part to the
`RustClassIngest` it builds."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text
from ...types_defs import (
    DeferredInherit,
    DeferredParentLink,
    FunctionLocation,
    PropertyDict,
    RustTraitImpl,
)
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..class_ingest import base_targets as bt
from ..class_ingest import identity as id_
from ..class_ingest.utils import body_method_nodes, skip_method
from ..language_definitions import LanguageClassIngest
from ..method_ingest import MethodIngestHost, ingest_method
from ..utils import function_span_key
from . import utils as rs_utils
from .type_inference import RustTypeInferenceEngine

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import LanguageQueries
    from ..definition_context import DefinitionContext


class RustClassHost(Protocol):
    """What Rust class ingestion reads and calls on the class pass that
    builds it: the INHERITS/IMPLEMENTS edges held until every class is
    registered, which an `impl Trait for Type` block adds to, and how a
    written name and a held-back parent resolve."""

    _deferred_inherits: list[DeferredInherit]

    def _resolve_to_qn(
        self,
        name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str: ...

    def _resolve_deferred_parent_qn(
        self, entry: DeferredInherit
    ) -> tuple[str, bool] | None: ...


class RustImplScope(NamedTuple):
    class_qn: str
    module_qn: str
    owner_module_qn: str
    impl_target: str


def _enclosing_mod(module_node: Node) -> Node | None:
    enclosing_module_node = module_node.parent
    while (
        enclosing_module_node is not None
        and enclosing_module_node.type != cs.TS_RS_MOD_ITEM
    ):
        enclosing_module_node = enclosing_module_node.parent
    return enclosing_module_node


class RustClassIngest(LanguageClassIngest):
    """Ingests Rust impl blocks and inline modules, and decides which trait
    impls only another crate's dispatch can call.

    Built and owned by `ClassIngest`. The registries, the module paths, the
    definition facts and the impl maps are the pass's own, held by reference:
    the override walk reads the maps this fills."""

    __slots__ = (
        "_host",
        "_method_host",
        "ingestor",
        "repo_path",
        "project_name",
        "function_registry",
        "module_qn_to_file_path",
        "flow_capture_enabled",
        "import_processor",
        "facts",
        "impl_owner_module",
        "impl_method_traits",
        "inherent_impl_methods",
        "_deferred_parent_links",
        "_trait_impls",
    )

    # First claim wins: a fn in a mod inside a trait const default is already
    # claimed by the function pass under its own qn, and overwriting would
    # re-attribute its calls to this pass's twin.
    keeps_claimed_method_spans = True
    # An impl's OVERRIDES follow the impl block (impl_method_traits,
    # inherent_impl_methods), which only parsing records, and walking the
    # trait instead would link an inherent method sharing the trait method's
    # name. An unchanged impl keeps the edges its own parse wrote.
    joins_read_back_implementers = False

    def __init__(
        self,
        host: RustClassHost,
        context: DefinitionContext,
        *,
        method_host: MethodIngestHost,
        flow_capture_enabled: bool,
        impl_owner_module: dict[str, str],
        impl_method_traits: dict[str, str],
        inherent_impl_methods: set[str],
        deferred_parent_links: list[DeferredParentLink],
    ) -> None:
        self._host = host
        # What an impl block's methods are written through.
        self._method_host = method_host
        self.ingestor = context.ingestor
        self.repo_path = context.repo_path
        self.project_name = context.project_name
        self.function_registry = context.function_registry
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.flow_capture_enabled = flow_capture_enabled
        self.import_processor = context.import_processor
        self.facts = context.facts
        self.impl_owner_module = impl_owner_module
        self.impl_method_traits = impl_method_traits
        self.inherent_impl_methods = inherent_impl_methods
        # Shared with the function pass, which emits them once every parent
        # is registered.
        self._deferred_parent_links = deferred_parent_links
        # Rust `impl Trait for Type` blocks paired with the method qns they
        # ingested. Whether the trait is external is only knowable once every
        # trait is registered, so resolve_deferred_inherits judges it (through
        # flag_external_trait_overrides) and flags the methods of the external
        # ones (issue #1048).
        self._trait_impls: list[RustTraitImpl] = []

    def skips_class(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        lang_config: LanguageSpec,
        file_path: Path | None,
        sorted_func_nodes: list[Node] | None,
        func_node_starts: list[int] | None,
        allow_defer: bool,
    ) -> bool:
        # An impl block is no type of its own: its methods join the type it
        # targets.
        if class_node.type != cs.TS_IMPL_ITEM:
            return False
        self.ingest_impl_methods(
            class_node,
            module_qn,
            language,
            lang_queries,
            sorted_func_nodes=sorted_func_nodes,
            func_node_starts=func_node_starts,
        )
        return True

    def ingest_impl_methods(
        self,
        class_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        if not (impl_target := rs_utils.extract_impl_target(class_node)):
            return

        # An impl block inside `mod inner` targets a type whose node lives
        # under the module path (proj...inner.Widget). Resolve the impl target
        # against its enclosing module so the method binds to the real type
        # node instead of a phantom under the file module.
        mod_parts = rs_utils.build_module_path(class_node)
        owner_module_qn = (
            f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(mod_parts)}"
            if mod_parts
            else module_qn
        )
        class_qn = f"{owner_module_qn}.{impl_target}"
        written_path = rs_utils.extract_impl_target_path(class_node)
        if written_path:
            self.import_processor.rust.record_rust_impl_self_path(
                module_qn, class_qn, written_path
            )

        # `impl Trait for Type` means Type IMPLEMENTS Trait. The target type's
        # node label may be Class/Enum/Type, so match the relationship source
        # to its registered label (else the IMPLEMENTS edge never resolves).
        # Collected either way: an inherent block's methods implement nothing,
        # which the override pass has to be TOLD, or it reads their absence
        # from the trait map as no information and guesses (issue #1078).
        impl_method_qns: list[str] = []
        trait_unrepresentable = False
        if trait_name := rs_utils.extract_impl_trait(class_node):
            # The file, not `owner_module_qn`: the prune matches the modules a
            # removed file recorded, and an inline `mod` is not one of them.
            self.impl_owner_module[class_qn] = module_qn
            trait_unrepresentable = not self._defer_impl_trait(
                class_node, trait_name, class_qn, owner_module_qn, impl_method_qns
            )

        body_node = class_node.child_by_field_name("body")

        if not body_node:
            return

        file_path = self.module_qn_to_file_path.get(module_qn)
        lang_config: LanguageSpec = lang_queries[cs.QUERY_CONFIG]

        method_nodes = body_method_nodes(
            body_node, lang_queries, sorted_func_nodes, func_node_starts
        )
        if method_nodes is None:
            return

        for method_node in method_nodes:
            if skip_method(method_node, class_node, body_node, lang_config):
                continue
            ingested_qn = ingest_method(
                self._method_host,
                method_node,
                class_qn,
                cs.NodeLabel.CLASS,
                language,
                file_path=file_path,
                # The impl target may be a primitive (`impl From<Foo> for u8`)
                # or a type registered later in the pass; defer the containment
                # edge so it verifies against the registry (module fallback for
                # primitives) instead of dangling on a phantom Class qn.
                defer_containment=self._deferred_parent_links,
                module_qn=owner_module_qn,
            )
            self._record_impl_method(
                method_node,
                ingested_qn,
                RustImplScope(
                    class_qn=class_qn,
                    module_qn=module_qn,
                    owner_module_qn=owner_module_qn,
                    impl_target=impl_target,
                ),
                impl_method_qns,
            )

        # Two blocks of one module can share `class_qn` while naming
        # different types (the crate's `String` and `std::string::String`),
        # so each method keeps its own block's self type (#2595).
        if written_path:
            for method_qn in impl_method_qns:
                self.import_processor.rust.record_rust_impl_self_path(
                    module_qn, method_qn, written_path
                )

        # The PATH decides, not the name: `extract_impl_trait` reads no name
        # off `impl std::ops::Add<u32> for S`, and calling that inherent would
        # bar a real trait impl's methods from ever overriding.
        if (
            rs_utils.extract_impl_trait_path(class_node) is None
            or trait_unrepresentable
        ):
            self.inherent_impl_methods.update(impl_method_qns)

    def _defer_impl_trait(
        self,
        class_node: Node,
        trait_name: str,
        class_qn: str,
        owner_module_qn: str,
        impl_method_qns: list[str],
    ) -> bool:
        # `impl Trait for Type`: defer the IMPLEMENTS edge and register the
        # trait impl. False when the trait path is unrepresentable.
        trait_qn, alt_trait_qn = self._trait_qn(
            rs_utils.extract_impl_trait_path(class_node) or trait_name,
            trait_name,
            owner_module_qn,
        )
        if trait_qn != cs.RUST_UNRESOLVABLE_QN:
            # The trait (or the impl target) may live in a file not yet
            # parsed; hold the IMPLEMENTS edge back for
            # resolve_deferred_inherits so an unresolvable trait
            # (std::fmt::Display) emits no phantom edge. A trait path
            # through an unrepresentable #[path] module has no referent at
            # all, so it is skipped entirely rather than bound to a
            # name-derived shadow trait (issue #1082).
            trait_entry = DeferredInherit(
                rel_type=cs.RelationshipType.IMPLEMENTS,
                child_qn=class_qn,
                parent_qn=trait_qn,
                module_qn=owner_module_qn,
                base_index=0,
                language=cs.SupportedLanguage.RUST,
                alt_parent_qn=alt_trait_qn,
            )
            self._host._deferred_inherits.append(trait_entry)
            # Collect this block's methods against the trait AS WRITTEN: if
            # the trait belongs to another crate, its dispatch is the only
            # caller they can ever have (issue #1048). The decision waits
            # for resolve_deferred_inherits, when every first-party trait
            # is registered.
            self._trait_impls.append(
                RustTraitImpl(
                    entry=trait_entry,
                    spelling=(
                        rs_utils.extract_impl_trait_path(class_node) or trait_name
                    ),
                    method_qns=impl_method_qns,
                )
            )
            # Record the implementer so a Rust trait call to the sole
            # concrete impl redirects, matching the class-declaration
            # IMPLEMENTS path.
            self.facts.interface_implementers.setdefault(trait_qn, set()).add(class_qn)
            return True
        # The trait path has no referent, so no OVERRIDES target is
        # knowable: the caller classifies these methods as inherent so the
        # generic override pass never matches them by name against a
        # DIFFERENT same-named trait the type also implements (#1082).
        return False

    def _record_impl_method(
        self,
        method_node: Node,
        ingested_qn: str | None,
        scope: RustImplScope,
        impl_method_qns: list[str],
    ) -> None:
        class_qn = scope.class_qn
        module_qn = scope.module_qn
        owner_module_qn = scope.owner_module_qn
        impl_target = scope.impl_target
        # Record where this method landed, same as the generic method
        # path: the registered qn (a collision deduplicates it to
        # `natural@<line>`) is recoverable afterwards only by span.
        # First claim wins: a fn nested in a mod inside a const
        # initializer is already claimed by the function pass under
        # its own qn, and overwriting the record would re-attribute
        # its calls to this pass's twin.
        if ingested_qn is not None:
            impl_method_qns.append(ingested_qn)
            # An impl block is no module, so the method's `super::` counts
            # from the impl's own enclosing module -- the file module unless
            # the block sits in an inline `mod` (issue #1086).
            self.facts.rust_function_modules[ingested_qn] = owner_module_qn
            span = function_span_key(module_qn, method_node)
            if span not in self.facts.function_locations:
                self.facts.function_locations[span] = FunctionLocation(
                    label=cs.NodeLabel.METHOD.value,
                    qualified_name=ingested_qn,
                    container_qn=class_qn,
                )
        # Record the method's return type (Self -> impl target) so a chained
        # call (`Ping::new(msg).into_frame()`) and a call-bound local
        # (`let cmd = Command::from_frame(f)`) can resolve the next hop.
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        method_name = safe_decode_text(name_node) if name_node else None
        if method_name and (
            return_type := rs_utils.extract_return_type_name(method_node, impl_target)
        ):
            self.facts.method_return_types[
                f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
            ] = return_type

    def _trait_qn(self, path: str, name: str, module_qn: str) -> tuple[str, str | None]:
        """Qn for the trait an `impl ... for Type` block names, and a fallback.

        Resolving the SIMPLE name alone ends in a project-wide sweep that any
        same-named trait can answer, so `impl crate::z::Base for S` recorded
        `a.Base` (issue #1080). The written path outranks the sweep wherever
        it says where to look: a crate-relative head names a first-party
        module outright, and a head the toolchain or the manifest speaks for
        names another crate's trait, which no first-party sweep may answer
        for however the spelling collides.

        The crate-relative answer carries the name-anchored one as a
        fallback, since `crate::Base` for a re-exported trait names the entry
        module while the trait registers where it is declared.
        """
        head, _, tail = path.partition(cs.SEPARATOR_DOUBLE_COLON)
        anchored = self._host._resolve_to_qn(name, module_qn, cs.SupportedLanguage.RUST)
        if not tail:
            return anchored, None
        if head in (cs.RUST_CRATE_KEYWORD, cs.KEYWORD_SELF, cs.KEYWORD_SUPER):
            rewritten = self.import_processor.rust.rewrite_rust_local_use_path(
                path, module_qn
            )
            if rewritten == cs.RUST_UNRESOLVABLE_QN:
                # The trait path traverses an unrepresentable #[path] module:
                # it has no referent. Surface the sentinel so the caller drops
                # the relationship entirely, rather than the name-anchored qn,
                # which would rebind to a same-named trait shadow (issue #1082).
                return cs.RUST_UNRESOLVABLE_QN, None
            return (rewritten, anchored) if rewritten != path else (anchored, None)
        # The head is itself a name a `use` may have bound (`use std::io;`
        # then `impl io::Read`), and only the expanded crate says who speaks
        # for the trait.
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        expanded = f"{import_map.get(head, head)}{cs.SEPARATOR_DOUBLE_COLON}{tail}"
        if expanded.startswith(f"{self.project_name}{cs.SEPARATOR_DOT}"):
            # A `use` of a first-party module, already rewritten to its qn:
            # the binding says which module the head means, so the path is
            # every bit as exact as the crate-relative spelling above.
            return (
                expanded.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT),
                anchored,
            )
        crate = expanded.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        if crate in cs.RS_STDLIB_CRATES or (
            self.import_processor.rust.rust_head_is_external_dep(crate, module_qn)
        ):
            return expanded, None
        return anchored, None

    def flag_external_trait_overrides(self) -> None:
        # A method in an `impl <ExternalTrait> for Type` block is only ever
        # invoked through that trait's dispatch, by code the graph does not
        # hold: the stdlib calls io::Read::read, serde calls its Visitor
        # methods, the log facade calls Log::log. No first-party edge can
        # prove it live, so it roots the dead-code walk (issue #1048).
        impls = self._trait_impls
        self._trait_impls = []
        for impl in impls:
            if not self._trait_is_external(impl):
                # First-party instead: bind its methods to the trait THIS block
                # names, so the override pass stops deriving the parent from the
                # method's qualified name (issue #1076).
                self._record_impl_method_traits(impl)
                continue
            for method_qn in impl.method_qns:
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.METHOD,
                    {
                        cs.KEY_QUALIFIED_NAME: method_qn,
                        cs.KEY_OVERRIDES_EXTERNAL: True,
                    },
                )

    def _record_impl_method_traits(self, impl: RustTraitImpl) -> None:
        # Only a REGISTERED trait: the override edge needs a real endpoint, and
        # a spelling that resolves nowhere leaves the generic ancestry walk as
        # the better guess.
        resolved = self._host._resolve_deferred_parent_qn(impl.entry)
        if resolved is None or resolved[1]:
            return
        trait_qn = resolved[0]
        if self.function_registry.get(trait_qn) != cs.NodeLabel.INTERFACE:
            return
        for method_qn in impl.method_qns:
            self.impl_method_traits[method_qn] = trait_qn

    def _trait_is_external(self, impl: RustTraitImpl) -> bool:
        # Only POSITIVE evidence roots: the crate the trait is written under
        # must be the toolchain or a manifest-proven external dependency. An
        # unresolved head alone means nothing (a workspace sibling reached by
        # its crate name, `use grep_searcher::Sink`, resolves through a layout
        # the import half may not cover), and rooting on it would hide a
        # genuinely dead first-party trait impl for good.
        module_qn = impl.entry.module_qn
        resolved = self._host._resolve_deferred_parent_qn(impl.entry)
        if (
            resolved is not None
            and not resolved[1]
            and self.function_registry.get(resolved[0]) == cs.NodeLabel.INTERFACE
        ):
            # A registered first-party TRAIT dispatches through OVERRIDES
            # liveness, which already covers its impls. The node kind has to
            # be checked: parent resolution ends in a project-wide simple-name
            # sweep, so a struct the project happens to call `Default` answers
            # for the prelude trait and would silence every `impl Default`.
            return False
        head, scoped = self._trait_head(impl.spelling, module_qn)
        if self.import_processor.rust.rust_head_is_repo_crate(head):
            # The crate is indexed here whatever the manifest says about
            # fetching it, so its traits are first-party.
            return False
        if (
            head in cs.RS_STDLIB_CRATES
            or self.import_processor.rust.rust_head_is_external_dep(head, module_qn)
        ):
            return True
        # A bare name with no import and no first-party declaration is in
        # scope only because the prelude put it there.
        return not scoped and head in cs.RS_PRELUDE_TRAITS

    def _trait_head(self, spelling: str, module_qn: str) -> tuple[str, bool]:
        """The crate the written trait path speaks for, and whether it is scoped.

        The written head is itself an imported name whenever the module said
        so: `use std::io;` then `impl io::Read` (Rust's usual spelling for a
        stdlib trait) writes the head `io`, which speaks for `std`. Only a
        head no `use` mentions stands for itself, and a bare one of those is
        unscoped: nothing but the prelude can have put it in scope.
        """
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        head = spelling.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0]
        if (target := import_map.get(head)) is not None:
            return target.split(cs.SEPARATOR_DOUBLE_COLON, 1)[0], True
        return head, cs.SEPARATOR_DOUBLE_COLON in spelling

    def reexport_target(self, entry: DeferredInherit) -> str | None:
        """Where a `pub use` in the named module declares the parent.

        `crate::Base` is a legal path for a trait the crate root re-exports,
        and it is exact, but the trait registers where it is DECLARED. The
        binding says where that is, so the written path still decides and no
        project-wide sweep of the simple name has to (issue #1080). Only a
        registered target answers: a re-export OUT of the project resolves
        nowhere first-party, and guessing there would bind a decoy. A facade
        module that re-exports what it was itself given is the usual reason
        the crate root can name the trait at all, so the chain is followed to
        its end rather than one hop.
        """
        qn = entry.parent_qn
        seen = {qn}
        while True:
            owner, _, item = qn.rpartition(cs.SEPARATOR_DOT)
            bound = self.import_processor.import_mapping.get(owner, {}).get(item)
            if not bound:
                return None
            qn = (
                bound
                if bound.startswith(f"{self.project_name}{cs.SEPARATOR_DOT}")
                else self.import_processor.rust.rust_resolve_relative(
                    owner, bound.split(cs.SEPARATOR_DOUBLE_COLON), owner
                )
            )
            if qn in seen:
                # Two modules re-exporting each other's name never declare it.
                return None
            seen.add(qn)
            if self.function_registry.get(qn) in bt.base_target_kinds(entry.language):
                return qn

    def record_members(
        self,
        class_node: Node,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        label: cs.NodeLabel,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        # Record Rust struct field types so a field-hop receiver
        # (`self.shutdown.is_shutdown()`) resolves through the field's type,
        # plus guard-container inner types (`state: Mutex<State>` -> State),
        # applied only at a lock/read/borrow hop.
        rust_engine = RustTypeInferenceEngine()
        if field_types := rust_engine.build_field_type_map(class_node):
            self.facts.class_field_types[class_qn] = field_types
        if guard_inner := rust_engine.build_field_guard_inner_map(class_node):
            self.facts.class_field_guard_inner[class_qn] = guard_inner
        if elements := rust_engine.build_field_element_map(class_node):
            self.facts.class_field_element_types[class_qn] = elements

    def ingest_inline_modules(
        self,
        module_nodes: list[Node],
        module_qn: str,
        lang_config: LanguageSpec,
    ) -> None:
        gated_targets: set[str] = set()
        ungated_targets: set[str] = set()
        for module_node in module_nodes:
            if not isinstance(module_node, Node):
                continue
            if not (module_name_node := module_node.child_by_field_name("name")):
                continue
            if not module_name_node.text:
                continue

            decorators = rs_utils.attribute_texts(module_node)
            gated = any(
                "".join(str(decorator).split()) == cs.RS_CFG_TEST_ATTRIBUTE
                for decorator in decorators
            )
            # A bodyless `mod foo;` only declares that the file module foo.rs
            # belongs here; foo.rs already yields its own real-path Module node
            # with the same qn. Emitting a second synthetic-path node collides
            # on that qn and clobbers the file's real path, so skip it -- but
            # a `#[cfg(test)]` gate on the declaration is the ONLY record that
            # the target compiles for tests alone. The gate is recorded on
            # THIS file's own Module node (as target-qn candidates), never on
            # the target's: an incremental re-parse of the target deletes and
            # re-mints its node, which would silently erase a merged property,
            # while the declaring node lives exactly as long as the
            # declaration does (issue #1010).
            if module_node.child_by_field_name(cs.FIELD_BODY) is None:
                # Ungated declarations record too: another Cargo target
                # (src/main.rs) declaring the SAME file module without the
                # gate compiles it as production code, and that must win
                # over a gated sibling declaration.
                candidates = self._cfg_test_candidates(
                    module_node,
                    module_qn,
                    safe_decode_text(module_name_node) or "",
                    decorators,
                )
                (gated_targets if gated else ungated_targets).update(candidates)
                continue

            self._ingest_bodied_inline_module(
                module_node, module_name_node, module_qn, lang_config, decorators
            )
        if gated_targets or ungated_targets:
            self._record_mod_declarations(module_qn, gated_targets, ungated_targets)

    def _ingest_bodied_inline_module(
        self,
        module_node: Node,
        module_name_node: Node,
        module_qn: str,
        lang_config: LanguageSpec,
        decorators: list[str],
    ) -> None:
        module_name = safe_decode_text(module_name_node)
        nested_qn = id_.build_nested_qualified_name_for_class(
            module_node,
            module_qn,
            module_name or "",
            lang_config,
            include_impl_targets=True,
        )
        inline_module_qn = nested_qn or f"{module_qn}.{module_name}"

        module_props: PropertyDict = {
            cs.KEY_QUALIFIED_NAME: inline_module_qn,
            cs.KEY_NAME: module_name,
            cs.KEY_PATH: f"{cs.INLINE_MODULE_PATH_PREFIX}{module_name}",
            cs.KEY_START_LINE: module_node.start_point[0] + 1,
            cs.KEY_END_LINE: module_node.end_point[0] + 1,
            # An inline mod's coverage is its FILE's: this producer is
            # the Rust bodied-mod path and Rust sits in the source/sink
            # registry, so only the capture toggle decides — a missing
            # property would read as a spurious coverage gap under the
            # file's real path (issue #1050).
            cs.KEY_FLOW_COVERED: self.flow_capture_enabled,
        }
        if decorators:
            module_props[cs.KEY_DECORATORS] = decorators
        # A bodied inline module is physically located in this file; give
        # it the real path so it joins containment on (file, line).
        file_path = self.module_qn_to_file_path.get(module_qn)
        owner = self.module_qn_to_file_path.get(inline_module_qn)
        if owner is not None and owner != file_path:
            # A cfg twin of another file's module (`mod b { .. }` here,
            # `mod b;` and b.rs under the other cfg) shares that file's qn,
            # and the node is the file's, as for a bodyless declaration.
            # Given this file's path, b.rs's own re-parse delete missed it
            # and the graph seed handed its qn to this file, filing a
            # re-parsed b.rs under `<qn>.rs`. Only the identity is merged.
            module_props = {cs.KEY_QUALIFIED_NAME: inline_module_qn}
        elif file_path is not None:
            module_props[cs.KEY_PATH] = cached_relative_path(
                file_path, self.repo_path
            ).as_posix()
            module_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)
        logger.debug(
            ls.CLASS_FOUND_INLINE_MODULE.format(name=module_name, qn=inline_module_qn)
        )
        # Same-qn bodied twins (mutually-exclusive cfg mods, or two impls
        # each declaring a method-local mod) upsert this ONE node: Module
        # identity is the global qualified_name, so the last twin's
        # properties win. Accepted representational merge (#1164); call
        # resolution stays per-twin through each mod body's own uses
        # (#1163), so only the node's location properties are lossy.
        self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, module_props)
        # Record the inline module qn so deferred import verification
        # counts it as a real internal target.
        self.facts.declared_module_qns.add(inline_module_qn)

        # Link the inline module into the containment tree: its enclosing
        # module (file module, or an outer mod) DEFINES it. Without this the
        # inline Module node is an orphan defining nothing. The parent is
        # the nearest enclosing MODULE node, whose qn is rebuilt with the
        # same scoped construction used to register it. Neither the qn's
        # rsplit prefix nor a mods-only re-walk works: under a trait/impl
        # body a mod keeps the class scope in its qn (foo.T.outer), so the
        # prefix foo.T is the TRAIT node (not a module), and a mods-only
        # walk drops that scope to foo.outer (which does not exist) — either
        # way the Module->Module DEFINES dangles (issues #1018, nested #1166).
        parent_module_qn = self._inline_module_parent_qn(
            module_node, module_qn, lang_config
        )
        if parent_module_qn and parent_module_qn != inline_module_qn:
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, parent_module_qn),
                cs.RelationshipType.DEFINES,
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, inline_module_qn),
            )

    def _inline_module_parent_qn(
        self, module_node: Node, module_qn: str, lang_config: LanguageSpec
    ) -> str:
        enclosing_module_node = _enclosing_mod(module_node)
        if enclosing_module_node is None:
            return module_qn
        enclosing_name = safe_decode_text(
            enclosing_module_node.child_by_field_name(cs.FIELD_NAME)
        )
        return (
            id_.build_nested_qualified_name_for_class(
                enclosing_module_node,
                module_qn,
                enclosing_name or "",
                lang_config,
                include_impl_targets=True,
            )
            or f"{module_qn}{cs.SEPARATOR_DOT}{enclosing_name}"
        )

    def _record_mod_declarations(
        self, module_qn: str, gated_targets: set[str], ungated_targets: set[str]
    ) -> None:
        decl_props: PropertyDict = {cs.KEY_QUALIFIED_NAME: module_qn}
        if gated_targets:
            decl_props[cs.KEY_RUST_CFG_TEST_MODS] = sorted(gated_targets)
        if ungated_targets:
            decl_props[cs.KEY_RUST_UNGATED_MODS] = sorted(ungated_targets)
        self.ingestor.ensure_node_batch(cs.NodeLabel.MODULE, decl_props)

    def _cfg_test_candidates(
        self,
        module_node: Node,
        module_qn: str,
        module_name: str,
        decorators: Sequence[object] = (),
    ) -> set[str]:
        """Qualified-name candidates for a bodyless declaration's target.

        Resolved through the same relative-path machinery `self::` use
        paths take (entry files and explicit manifest targets attach
        declared submodules beside themselves via the declaring scan;
        mod.rs directories and plain files nest them). A declaration
        inside an inline mod adds the inline-nested spelling too, since
        the two cannot coexist (E0428). Dead-code keeps only candidates
        that name a REAL module node, so a spelling the qn scheme does
        not produce (a `#[path]` override, a tests/ crate sibling) is
        inert rather than a mismark.
        """
        if not module_name:
            return set()
        chain = rs_utils.build_module_path(module_node)
        if redirect := self._path_attribute_qn(module_qn, chain, decorators):
            # `#[path]` names the backing file outright, and the file is
            # where the qn scheme keys the module, so the name-derived
            # spellings below back nothing at all here (issue #1035).
            return {redirect}
        resolved_candidate = self.import_processor.rust.rust_resolve_relative(
            module_qn, [*chain, module_name], module_qn
        )
        # An unrepresentable #[path] target yields no candidate qn (issue
        # #1082); the file-derived spelling below still applies for a chain.
        candidates = (
            set()
            if resolved_candidate == cs.RUST_UNRESOLVABLE_QN
            else {resolved_candidate}
        )
        if chain:
            # A chain declaration's target FILE lives under the declaring
            # file's directory tree regardless of the inline qn nesting
            # (`mod outer { mod helpers; }` in src/lib.rs backs
            # src/outer/helpers.rs, qn src.outer.helpers), so the
            # path-derived spelling joins the candidates alongside the
            # inline-nested one; the relative machinery resolves crate
            # PATHS (which nest inline), not file placement.
            candidates.add(cs.SEPARATOR_DOT.join([module_qn, *chain, module_name]))
            file_path = self.module_qn_to_file_path.get(module_qn)
            if file_path is not None:
                dir_parts = cached_relative_path(file_path, self.repo_path).parts[:-1]
                candidates.add(
                    cs.SEPARATOR_DOT.join(
                        [self.project_name, *dir_parts, *chain, module_name]
                    )
                )
        return candidates

    def _path_attribute_qn(
        self, module_qn: str, chain: list[str], decorators: Sequence[object]
    ) -> str | None:
        """The qn of the file a `#[path]` declaration redirects to.

        The path counts from the directory holding the declaring file, and
        a `mod.rs` target names the directory itself, which is the qn the
        indexer gives that directory's module. Inside inline `mod` blocks
        the chain joins that directory, and a file that is not itself a
        module root (anything but lib.rs, main.rs or mod.rs) contributes
        its own stem as a directory first, exactly as rustc counts it.
        """
        redirect = rs_utils.path_attribute_target(decorators)
        file_path = self.module_qn_to_file_path.get(module_qn)
        if redirect is None or file_path is None:
            return None
        declaring = cached_relative_path(file_path, self.repo_path)
        dir_parts = list(declaring.parts[:-1])
        if chain:
            if declaring.stem not in cs.RS_ENTRY_STEMS:
                dir_parts.append(declaring.stem)
            dir_parts.extend(chain)
        parts = rs_utils.path_attribute_qn_parts(dir_parts, redirect)
        return (
            None
            if parts is None
            else cs.SEPARATOR_DOT.join([self.project_name, *parts])
        )
