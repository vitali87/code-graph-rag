"""Rust's import pass: `use` declarations into the shared import map, and the
Cargo crate, workspace and target layout that rewrites `crate::`, `super::`,
`self::` and workspace-crate paths into project qns.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Rust
file's imports to the `RustImporter` it builds."""

import os
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...types_defs import FunctionLocation, FunctionSpanKey, PropertyDict
from ...utils.path_utils import should_keep_dir, should_skip_rel_file
from ..import_host import ImporterContext, ImportQueueHost, WalkPolicyHost
from ..utils import import_site_props, is_project_qn
from . import cargo, cargo_semver
from . import source_scan as rs_scan
from . import utils as rs_utils


class RustImportHost(ImportQueueHost, WalkPolicyHost, Protocol):
    """What the Rust importer reads and calls on the import processor that
    builds it: the import-site record and deferred-edge queue, and the
    indexer's walk policy, read live so the sweep never reads declarations
    in files the indexer skips (issue #1100)."""


def _kept_mod_scope_writers(
    writers: list[tuple[str, bool, dict[str, str]]],
    owner_path: str | None,
    known_module_paths: Mapping[str, str],
) -> list[tuple[str, bool, dict[str, str]]]:
    # An owned key keeps only writers from its owning file. An unowned one
    # lets pure module chains oust fn-/block-local forgeries, and drops the
    # key entirely when writers from several files survive (cfg/macro twins).
    if owner_path:
        return [w for w in writers if known_module_paths.get(w[0]) == owner_path]
    kept = writers
    if any(w[1] for w in writers) and not all(w[1] for w in writers):
        kept = [w for w in writers if w[1]]
    return [] if len({w[0] for w in kept}) > 1 else kept


class RustImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "_import_sites",
        "_rust_dir_listing",
        "_rust_entry_mod_decls",
        "_rust_module_mod_decls",
        "_rust_redirect_parents",
        "_rust_explicit_targets",
        "_rust_auto_build_flags",
        "_rust_auto_discovery_flags",
        "_rust_workspace_crates",
        "_rust_pkg_deps",
        "_rust_dep_closures",
        "_rust_qn_packages",
        "_rust_dep_members",
        "_rust_member_packages",
        "_rust_inline_scope_keys",
        "rust_impl_self_paths",
        "_rust_impl_self_path_keys",
        "_rust_pending_fn_scope_uses",
        "rust_fn_scope_imports",
        "rust_fn_scope_mod_imports",
        "rust_block_items",
        "rust_block_item_qns",
        "rust_block_scope_imports",
        "rust_self_module_imports",
        "rust_restricted_use_names",
        "_rust_restricted_use_keys",
        "_rust_fn_scope_keys",
        "_rust_pending_mod_scope_uses",
        "_rust_mod_scope_registry",
        "_rust_mod_scope_shadows",
    )

    def __init__(
        self,
        host: RustImportHost,
        context: ImporterContext,
        import_sites: dict[str, dict[str, PropertyDict]],
    ) -> None:
        self._host = host
        self.repo_path = context.repo_path
        self.project_name = context.project_name
        self.import_mapping = context.import_mapping
        # The processor's own site record, shared rather than copied: a
        # binding's site rides on the IMPORTS edge the processor's flush
        # emits.
        self._import_sites = import_sites
        self._init_rust_path_caches()
        self._init_rust_scope_maps()

    def _init_rust_path_caches(self) -> None:
        """What the filesystem and the Cargo manifests say, read lazily;
        `reset_rust_path_caches` clears each when either may have changed."""
        # Exact-case directory listings and entry-file `mod` declarations for
        # Rust path rewriting (issue #1007); cleared per run by
        # reset_rust_path_caches so watch re-runs re-observe the filesystem.
        self._rust_dir_listing: dict[str, frozenset[str]] = {}
        self._rust_entry_mod_decls: dict[
            tuple[str, ...], dict[str, rs_scan.RustEntryDecls]
        ] = {}
        # Same, for an ORDINARY module file: its own declarations, plus the
        # directory its `#[path]` targets count from. Kept apart from the
        # entry map, whose every stem is a crate root candidate (issue #1065).
        self._rust_module_mod_decls: dict[
            tuple[tuple[str, ...], bool, bool],
            tuple[rs_scan.RustEntryDecls, list[str]] | None,
        ] = {}
        # `#[path]` target qn -> the module that DECLARES it, for the `super::`
        # climb inside such a file (issue #1083). Lazy: a file cannot say who
        # declares it, so filling this at all means sweeping the repository.
        self._rust_redirect_parents: dict[str, str] | None = None
        # Explicit Cargo target entry paths per package dir ([[bin]]/[lib]/
        # [[example]]/[[test]]/[[bench]] `path` overrides): such entries
        # root their own crates wherever they sit, unlike auto-targets
        # found by location alone.
        self._rust_explicit_targets: dict[tuple[str, ...], frozenset[str]] = {}
        # Whether each package auto-detects build.rs: cargo compiles it
        # only when [package] build is UNSET (a string names the script
        # explicitly, false disables it entirely). Filled alongside the
        # explicit-target parse, cleared with it.
        self._rust_auto_build_flags: dict[tuple[str, ...], bool] = {}
        self._rust_auto_discovery_flags: dict[tuple[str, ...], dict[str, bool]] = {}
        # Workspace crate names (underscore-spelled) -> (package dir, lib
        # root dir, entry stem), so `use grep_searcher::sinks;` rewrites
        # to a project qn at parse time like crate:: paths do (issue
        # #1033). Built lazily from the root manifest's members plus the
        # root package; None until first use, re-None'd on manifest edits.
        self._rust_workspace_crates: (
            dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] | None
        ) = None
        # Per-package dependency targets: dep name (underscore-spelled) ->
        # repo-relative dir of the path/workspace dependency it resolves
        # to, or None for registry and renamed entries. Gates the member
        # rewrite to dependencies cargo actually binds to the member.
        self._rust_pkg_deps: dict[
            tuple[str, ...], dict[str, tuple[str, ...] | None]
        ] = {}
        # Per-package dependency closure (the package itself, its
        # dependencies of every kind and theirs), or None for a manifest
        # without a [package] table; and the package each probed qn sits in.
        # Both derive from the manifests, so they reset with the above.
        self._rust_dep_closures: dict[
            tuple[str, ...], frozenset[tuple[str, ...]] | None
        ] = {}
        self._rust_qn_packages: dict[str, tuple[str, ...] | None] = {}
        # Per-package dependency name (as the code spells it) -> the
        # workspace member a pathless entry fetches by package name, and the
        # members' package names -> package dir. Manifest-derived as well.
        self._rust_dep_members: dict[tuple[str, ...], dict[str, tuple[str, ...]]] = {}
        self._rust_member_packages: dict[str, tuple[str, ...]] | None = None

    def _init_rust_scope_maps(self) -> None:
        """The bindings a `use`, an inline mod or a block gives the names of
        one scope, filled as each file is parsed and read where a call is
        resolved."""
        # Inline-mod import scopes minted per file (file qn -> effective qns),
        # so a watch-mode re-parse of the file drops its stale sub-scopes.
        self._rust_inline_scope_keys: dict[str, set[str]] = {}
        # An impl block's registration qn (`<module>.<Type>`) -> the self
        # types written for it (`std::string::String`, `crate::shadow::String`,
        # `String`). The qn alone cannot tell an impl on a foreign type from
        # one on the crate's own same-named type written in another module
        # (#2595); the path, read in the impl's module, can. Keys are
        # tracked per declaring file for the re-parse drop.
        self.rust_impl_self_paths: dict[str, set[str]] = {}
        self._rust_impl_self_path_keys: dict[str, set[str]] = {}
        # Function-body uses parsed BEFORE the file's functions register:
        # the enclosing function's REGISTERED qn (colliding naturals are
        # deduplicated to `natural@<start_line>`) is only knowable after
        # ingestion, so entries queue by the function node's span until
        # finalise_rust_function_scope_uses resolves them.
        # The bool marks WEAK entries: an impure inline mod's use fanned
        # out to the functions declared in its block.
        self._rust_pending_fn_scope_uses: dict[
            str, list[tuple[int, int, dict[str, str], bool]]
        ] = {}
        # Function-body uses, keyed by the enclosing function's registered
        # qn. Kept OUT of import_mapping: Rust puts `mod run` and `fn run`
        # in different namespaces, so a function qn may equal a module qn
        # and the module-keyed map must never answer for the function.
        self.rust_fn_scope_imports: dict[str, dict[str, str]] = {}
        # Weak counterpart: an impure mod's own use fanned out to the
        # functions declared in its block. Mod-level precedence, so the
        # resolver consults it only after local items, unlike the body-use
        # map above which shadows everything.
        self.rust_fn_scope_mod_imports: dict[str, dict[str, str]] = {}
        # Every plain block in a file that declares function items directly,
        # by span, each name mapped to the REGISTERED qn of the item it
        # declares. Blocks nest inside functions and never overlap, so the
        # innermost block holding a call site is the scope that binds these
        # names, whatever else in the module answers to them (issue #1026),
        # and the companion set of those qns is what keeps a name lookup
        # from reaching them from outside their block (issue #1061).
        self.rust_block_items: dict[str, list[tuple[int, int, dict[str, str]]]] = {}
        self.rust_block_item_qns: set[str] = set()
        # Uses inside const/static initializer blocks, keyed by file
        # module qn: (block start byte, block end byte, imports, nested
        # mod spans, nested fn spans, nested item scopes with their
        # direct-child item names). No qn scope corresponds to such a
        # block and the use is E0425 outside it, so the resolver serves
        # these span-gated, only to calls whose site falls inside the
        # block; a call inside a nested mod span never binds through the
        # block (hard boundary), one inside a nested fn span binds
        # through it only after the fn's own body uses miss, and one
        # inside any nested block that declares the name as a direct
        # function item binds that local item instead.
        self.rust_block_scope_imports: dict[
            str,
            list[
                tuple[
                    int,
                    int,
                    dict[str, str],
                    list[tuple[int, int]],
                    list[tuple[int, int]],
                    list[tuple[int, int, dict[str, tuple[int, int]]]],
                ]
            ],
        ] = {}
        self._rust_fn_scope_keys: dict[str, set[str]] = {}
        # Modules bound by a `use path::{self}` brace item, per scope qn.
        # A module name lives in Rust's TYPE namespace while import_mapping
        # holds one slot for both namespaces, so a qualified `name::item`
        # reads here and a bare call reads there (issue #1054).
        self.rust_self_module_imports: dict[str, dict[str, str]] = {}
        # Names a module-level `use` binds with less than `pub` visibility,
        # per scope qn, each with the module whose subtree alone sees it: the
        # scope itself for a private (or `pub(self)`) one, the module a
        # `pub(crate)`/`pub(super)`/`pub(in path)` names. A glob re-export
        # (`pub use inner::*;`) written outside that subtree does not carry
        # them (issue #2542, #2599). Keys are tracked per declaring
        # file so a re-parse drops exactly what it wrote.
        self.rust_restricted_use_names: dict[str, dict[str, str]] = {}
        self._rust_restricted_use_keys: dict[str, set[str]] = {}
        # Sub-scope (inline mod) maps held back until every file is
        # parsed: whether the key collides with an indexer-registered
        # module is only knowable then (finalise_rust_mod_scope_uses).
        self._rust_pending_mod_scope_uses: dict[
            str, list[tuple[str, bool, dict[str, str]]]
        ] = {}
        # Every file's live mod-scope uses, surviving finalisation: the
        # watch path re-parses one file at a time, and arbitration must
        # weigh ALL writers of a key, not just the touched file's.
        self._rust_mod_scope_registry: dict[
            str, list[tuple[str, bool, dict[str, str]]]
        ] = {}
        # Sub-scope entries also commit into import_mapping for the
        # duration of their OWN file's parse (its impl ingestion resolves
        # traits through them), recording each overwritten value here so
        # retract_rust_mod_scope_uses can restore the pre-commit state
        # exactly (None means the name was absent).
        self._rust_mod_scope_shadows: dict[str, list[tuple[str, str, str | None]]] = {}

    def reset_rust_path_caches(self) -> None:
        # The filesystem may have gained or lost files since the caches
        # were filled; called per run by GraphUpdater._process_files.
        self._rust_dir_listing.clear()
        self._rust_entry_mod_decls.clear()
        self._rust_module_mod_decls.clear()
        self._rust_redirect_parents = None
        self._rust_explicit_targets.clear()
        self._rust_auto_build_flags.clear()
        self._rust_auto_discovery_flags.clear()
        self._rust_workspace_crates = None
        self._rust_pkg_deps.clear()
        self._rust_dep_closures.clear()
        self._rust_qn_packages.clear()
        self._rust_dep_members.clear()
        self._rust_member_packages = None

    def refresh_rust_path_caches_for(self, file_path: Path, created: bool) -> None:
        # The realtime watcher re-parses through process_file without
        # entering _process_files, so each event re-observes exactly what
        # it can have changed: only a CREATE changes the file set (the
        # directory listing), a MODIFY changes at most the touched file's
        # own contents (entry declarations when it is an entry file, the
        # package's explicit targets when it is the manifest). A DELETE
        # deliberately refreshes nothing: an editor's atomic save or a
        # checkout storm deletes and recreates entry files within one
        # debounce window, and a sibling re-parsed mid-storm must not
        # bake the transient absence into its import map (the stale view
        # converges on the file's return or the next full run, exactly
        # as before the watcher refreshed at all).
        directory = file_path.parent
        self._note_rust_listing_delta(file_path, created)
        try:
            dir_parts = directory.relative_to(self.repo_path).parts
        except ValueError:
            return
        if file_path.suffix == cs.EXT_RS:
            self._evict_rust_module_decls(file_path, dir_parts)
        if self._is_rust_entry_file(file_path, dir_parts):
            self._refresh_rust_entry_decls(file_path, dir_parts)
        if file_path.name == cs.PKG_CARGO_TOML:
            self._rebuild_rust_manifest_caches()

    def _note_rust_listing_delta(self, file_path: Path, created: bool) -> None:
        directory = file_path.parent
        if (cached := self._rust_dir_listing.get(str(directory))) is not None and (
            created or (file_path.name not in cached and file_path.is_file())
        ):
            # Apply the event's own delta rather than re-observing: during
            # a storm the filesystem may transiently lack files this event
            # did not touch, and a re-listing would bake that absence. A
            # MODIFY whose file the cached listing lacks is a CREATE the
            # debounce layer coalesced away (dispatch keeps only the
            # latest pending event per path), so it applies the same delta,
            # event-locally like the entry-declaration refresh below: only
            # while the file is observably present, so a MODIFY processed
            # after the file's deletion never bakes in a dead name.
            self._rust_dir_listing[str(directory)] = cached | {file_path.name}

    def _evict_rust_module_decls(
        self, file_path: Path, dir_parts: tuple[str, ...]
    ) -> None:
        # Any .rs file may back a module and declare a redirect, so its
        # own entry is evicted rather than replaced: an unreadable file
        # then re-reads on the next access instead of caching a hole.
        module = (
            dir_parts
            if file_path.stem == cs.INDEX_MOD
            else (*dir_parts, file_path.stem)
        )
        for dir_backed in (False, True):
            for want_mods in (False, True):
                self._rust_module_mod_decls.pop((module, dir_backed, want_mods), None)
        # Whole-map, because the edit may have added or removed a
        # redirect naming any file at all, and a stale declarer keeps
        # `super::` in a file it no longer moves pointing at it.
        self._rust_redirect_parents = None

    def _is_rust_entry_file(self, file_path: Path, dir_parts: tuple[str, ...]) -> bool:
        directory = file_path.parent
        return (
            file_path.name in (cs.LIB_RS, cs.MAIN_RS)
            or file_path.name in self._rust_explicit_entry_files(tuple(dir_parts))
            or (
                file_path.name == f"{cs.RS_BUILD_STEM}{cs.EXT_RS}"
                and cs.PKG_CARGO_TOML in self._rust_dir_entries(directory)
                and self._rust_has_auto_build(tuple(dir_parts))
            )
        )

    def _refresh_rust_entry_decls(
        self, file_path: Path, dir_parts: tuple[str, ...]
    ) -> None:
        # File-scoped like the event itself, and REPLACED only on a
        # successful read: a storm can modify the entry and delete it
        # before a sibling re-parses, and an absent stem would send
        # _rust_entry_stem to its non-definitive fallback, letting the
        # item tie-break flip a definitive crate attribution.
        stems = self._rust_entry_mod_decls.get(tuple(dir_parts))
        # The watcher's own relevance filter only knows the built-in
        # ignores, so an `--exclude`d entry file still reaches here. Its
        # declarations must not enter the cache: `_rust_entry_decls`
        # gates its OWN reads, but returns whatever this path cached
        # before that gate ever runs (issue #1100).
        if stems is not None and self._rust_file_is_indexed(
            [*dir_parts, file_path.name]
        ):
            try:
                source = file_path.read_text(
                    encoding=cs.RS_ENCODING_UTF8, errors="ignore"
                )
            except OSError:
                # A failed read keeps the stem's cached declarations, for
                # the storm reason above.
                pass
            else:
                top_level = rs_scan.top_level_only(
                    rs_scan.strip_comments_and_strings(source)
                )
                stems[file_path.stem] = rs_scan.entry_decls_of(top_level)

    def _rebuild_rust_manifest_caches(self) -> None:
        # A manifest edit can add, remove, or repoint explicit targets
        # anywhere in its package, and a CREATED manifest moves the
        # package boundary for every file below it, so the whole
        # target cache rebuilds. The entry-declaration map is DERIVED
        # from the target set: evict exactly the stems the fresh
        # manifests no longer back. lib/main keep their declarations
        # (and their storm protection) only while their kind's
        # discovery opt-out permits them — an edit flipping autolib or
        # autobins to false must leave the map exactly as a clean
        # index would (issue #1030).
        self._rust_explicit_targets.clear()
        self._rust_auto_build_flags.clear()
        self._rust_auto_discovery_flags.clear()
        self._rust_workspace_crates = None
        self._rust_pkg_deps.clear()
        self._rust_dep_closures.clear()
        self._rust_qn_packages.clear()
        self._rust_dep_members.clear()
        self._rust_member_packages = None
        for key, stems in self._rust_entry_mod_decls.items():
            allowed = {
                name[: -len(cs.EXT_RS)] for name in self._rust_explicit_entry_files(key)
            }
            auto_lib, auto_bins = self._rust_src_auto_entry_flags(list(key))
            if auto_lib:
                allowed.add(cs.LIB_RS[: -len(cs.EXT_RS)])
            if auto_bins:
                allowed.add(cs.MAIN_RS[: -len(cs.EXT_RS)])
            for stem in list(stems):
                if stem not in allowed:
                    stems.pop(stem, None)

    def drop_rust_module_import_state(self, module_qn: str) -> None:
        # Everything a file's parse contributed to the Rust import maps,
        # dropped so a re-parse rebuilds it or a deletion leaves nothing:
        # shadows (linger only when a parse aborted before its retraction
        # ran), pendings, the file's mod-scope registry entries, and the
        # committed fn-scope and inline-scope keys.
        self.retract_rust_mod_scope_uses(module_qn)
        self._rust_pending_fn_scope_uses.pop(module_qn, None)
        self._rust_pending_mod_scope_uses.pop(module_qn, None)
        self._rust_mod_scope_registry.pop(module_qn, None)
        self.rust_block_scope_imports.pop(module_qn, None)
        for _start, _end, items in self.rust_block_items.pop(module_qn, ()):
            self.rust_block_item_qns.difference_update(items.values())
        self.rust_self_module_imports.pop(module_qn, None)
        for key in self._rust_restricted_use_keys.pop(module_qn, ()):
            self.rust_restricted_use_names.pop(key, None)
        for key in self._rust_fn_scope_keys.pop(module_qn, ()):
            self.rust_fn_scope_imports.pop(key, None)
            self.rust_fn_scope_mod_imports.pop(key, None)
        for key in self._rust_inline_scope_keys.pop(module_qn, ()):
            self.import_mapping.pop(key, None)
        for key in self._rust_impl_self_path_keys.pop(module_qn, ()):
            self.rust_impl_self_paths.pop(key, None)

    def record_rust_impl_self_path(
        self, module_qn: str, impl_qn: str, written_path: str
    ) -> None:
        self.rust_impl_self_paths.setdefault(impl_qn, set()).add(written_path)
        self._rust_impl_self_path_keys.setdefault(module_qn, set()).add(impl_qn)

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        self.drop_rust_module_import_state(module_qn)
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_USE_DECLARATION:
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        imports = rs_utils.extract_use_imports(use_node)

        # super::/self:: are relative to the DECLARING module: a use inside
        # an inline `mod tests` block resolves against the inline-mod chain.
        # Its entries also STORE under the inline module's key: at file scope
        # they would shadow the file's own same-named items and rebind every
        # bare call in the file. A use inside a FUNCTION body shadows module
        # items only within that function, so it stores in the fn-scope map
        # under the function's REGISTERED qn (the caller's scope walk reads
        # it), while crate::/super::/self:: still resolve against the module
        # chain alone.
        # Resolution and storage follow different chains: crate::/super::/
        # self:: resolve against the MODULE chain alone, while the storage
        # key mirrors the registered-qn path (which keeps impl/trait/class
        # segments and skips functions).
        mod_parts = rs_utils.build_module_path(use_node)
        resolve_qn = (
            cs.SEPARATOR_DOT.join([module_qn, *mod_parts]) if mod_parts else module_qn
        )
        scope_node, scope_parts, pure_chain = rs_utils.rust_use_scope(use_node)
        effective_qn = (
            cs.SEPARATOR_DOT.join([module_qn, *scope_parts])
            if scope_parts
            else module_qn
        )
        sub_scope = scope_node is not None or scope_parts != []
        local_mods = rs_utils.enclosing_mod_names(use_node)
        resolved_imports: dict[str, str] = {}
        for imported_name, full_path in imports.items():
            resolved = self.rewrite_rust_local_use_path(
                full_path, resolve_qn, local_mods
            )
            if imported_name.startswith(cs.RS_SELF_MODULE_PREFIX):
                imported_name = imported_name[len(cs.RS_SELF_MODULE_PREFIX) :]
                # A `{self}` item binds a MODULE, which lives in Rust's type
                # namespace, so it is recorded where qualified `name::item`
                # resolution can find it whatever else claims the name.
                self.rust_self_module_imports.setdefault(effective_qn, {})[
                    imported_name
                ] = resolved
                if imported_name in self.import_mapping.get(effective_qn, {}):
                    # The one slot the shared map has is already taken by a
                    # binding from the other namespace, and evicting it would
                    # send that name's bare calls to the trie (issue #1054).
                    continue
            source_name = full_path.split(cs.SEPARATOR_DOUBLE_COLON)[-1]
            if not sub_scope and not self._rust_use_takes_file_slot(
                module_qn, imported_name, resolved, use_node, source_name
            ):
                continue
            resolved_imports[imported_name] = resolved
            site = self._host.record_import_site(
                effective_qn,
                imported_name,
                use_node,
                source_name,
                owner_qn=module_qn,
            )
            if sub_scope:
                # The generic deferral loop only reads the file-level map;
                # sub-scope imports still owe the file its IMPORTS edge.
                self._host.defer_import_edge(
                    module_qn, resolved, cs.SupportedLanguage.RUST, site=site
                )
            logger.debug(ls.IMP_RUST, name=imported_name, path=resolved)
        self._store_rust_use_imports(
            use_node,
            module_qn,
            effective_qn,
            scope_node,
            scope_parts,
            pure_chain,
            resolved_imports,
        )

    def _rust_use_takes_file_slot(
        self,
        module_qn: str,
        name: str,
        resolved: str,
        use_node: Node,
        source_name: str,
    ) -> bool:
        """Settle a second file-level `use` of a bound name (issue #2462).

        Rust rejects two same-namespace imports of one name in one scope
        (E0252), so a second `use` brings in another namespace (thiserror's
        derive macro beside the crate's own `Error` type) or a cfg-exclusive
        twin; both are real imports. The map keeps one binding per name, and
        the module's IMPORTS edges are read from it alone, so the binding
        left out queues its own edge here instead of vanishing.
        """
        held = self.import_mapping.get(module_qn, {}).get(name)
        if held is None or held == resolved:
            return True
        if self._rust_held_use_keeps_slot(module_qn, name, held, resolved):
            self._host.defer_import_edge(
                module_qn,
                resolved,
                cs.SupportedLanguage.RUST,
                site=import_site_props(use_node, name, source_name),
            )
            return False
        self._host.defer_import_edge(
            module_qn,
            held,
            cs.SupportedLanguage.RUST,
            site=self._import_sites.get(module_qn, {}).get(name),
        )
        return True

    def _rust_held_use_keeps_slot(
        self, module_qn: str, name: str, held: str, incoming: str
    ) -> bool:
        if self.rust_self_module_imports.get(module_qn, {}).get(name) == held:
            # A `{self}` module stays reachable through its own map, so it
            # yields the slot as before (issue #1054).
            return False
        # Name resolution only ever binds project items, which an external
        # crate's item (a derive macro) can never be: letting it take the
        # slot would just send the project binding's uses to a name-wide
        # fallback. Two bindings on the same side keep the later one.
        return is_project_qn(held, self.project_name) and not is_project_qn(
            incoming, self.project_name
        )

    def _store_rust_use_imports(
        self,
        use_node: Node,
        module_qn: str,
        effective_qn: str,
        scope_node: Node | None,
        scope_parts: list[str] | None,
        pure_chain: bool,
        resolved_imports: dict[str, str],
    ) -> None:
        if scope_node is not None:
            self._record_rust_body_scope_use(scope_node, module_qn, resolved_imports)
            return
        if scope_parts is None:
            # No enclosing block could be attributed (defensive): no qn
            # scope corresponds to the use, and any key would serve a
            # real scope's readers. Keep only the IMPORTS edges.
            return
        self._record_rust_use_visibility(
            use_node, module_qn, effective_qn, resolved_imports
        )
        if not scope_parts:
            self.import_mapping.setdefault(effective_qn, {}).update(resolved_imports)
            return
        self._commit_rust_mod_scope_use(
            use_node, module_qn, effective_qn, pure_chain, resolved_imports
        )

    def _record_rust_use_visibility(
        self,
        use_node: Node,
        module_qn: str,
        effective_qn: str,
        resolved_imports: dict[str, str],
    ) -> None:
        # The map has one slot per name and the last `use` writing it wins,
        # so the slot's visibility follows the same writer.
        root = self._rust_use_visibility_root(use_node, effective_qn)
        if root is None:
            if names := self.rust_restricted_use_names.get(effective_qn):
                for name in resolved_imports:
                    names.pop(name, None)
            return
        self.rust_restricted_use_names.setdefault(effective_qn, {}).update(
            dict.fromkeys(resolved_imports, root)
        )
        self._rust_restricted_use_keys.setdefault(module_qn, set()).add(effective_qn)

    def _rust_use_visibility_root(
        self, use_node: Node, effective_qn: str
    ) -> str | None:
        # The module whose subtree alone sees what a `use` binds; None for a
        # plain `pub`, and for a restriction path that leaves the project,
        # which no glob of it can be outside of.
        path = rs_utils.use_visibility_path(use_node)
        if path is None:
            return None
        if path == cs.KEYWORD_SELF:
            return effective_qn
        root = self.rewrite_rust_local_use_path(path, effective_qn)
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if root == self.project_name or root.startswith(project_prefix):
            return root
        return None

    def _record_rust_body_scope_use(
        self, scope_node: Node, module_qn: str, resolved_imports: dict[str, str]
    ) -> None:
        if scope_node.type == cs.TS_RS_FUNCTION_ITEM:
            self._rust_pending_fn_scope_uses.setdefault(module_qn, []).append(
                (
                    scope_node.start_point[0] + 1,
                    scope_node.start_point[1],
                    resolved_imports,
                    False,
                )
            )
            return
        # A const/static initializer block: no qn scope, so the entry is
        # span-gated and answers only calls written inside the block, with
        # nested mod/fn/item-scope spans recorded so inner scopes' own
        # bindings keep precedence.
        mod_holes, fn_holes, item_scopes = rs_utils.rust_block_scope_holes(scope_node)
        self.rust_block_scope_imports.setdefault(module_qn, []).append(
            (
                scope_node.start_byte,
                scope_node.end_byte,
                resolved_imports,
                mod_holes,
                fn_holes,
                item_scopes,
            )
        )

    def _commit_rust_mod_scope_use(
        self,
        use_node: Node,
        module_qn: str,
        effective_qn: str,
        pure_chain: bool,
        resolved_imports: dict[str, str],
    ) -> None:
        # A sub-scope key may collide with a module the INDEXER registered
        # (a fn-local or cfg-twin Rust module file, or a same-named module
        # of another language, since the qn scheme is language-agnostic):
        # ownership is only knowable once every file is parsed, so the
        # permanent commit defers to finalise_rust_mod_scope_uses. The
        # entries still commit for THIS file's parse window (its own impl
        # ingestion resolves traits through them), shadow-recorded so
        # retract_rust_mod_scope_uses restores the pre-commit state.
        shadows = self._rust_mod_scope_shadows.setdefault(module_qn, [])
        target = self.import_mapping.setdefault(effective_qn, {})
        for name, resolved in resolved_imports.items():
            shadows.append((effective_qn, name, target.get(name)))
            target[name] = resolved
        self._rust_pending_mod_scope_uses.setdefault(module_qn, []).append(
            (effective_qn, pure_chain, resolved_imports)
        )
        # Fan every inline-mod use out to its own functions' spans as weak
        # fn-scope entries, so a function binds through its enclosing mod
        # body even when the shared qn key is corrupted (issue #1017: one
        # qn, two modules). A fn-local mod can lose the shared-key
        # arbitration to a pure twin, and two pure cfg/macro twins merge
        # onto one key and overwrite each other; either way the span-gated
        # fn-scope map answers for each function instead of whatever the key
        # ends up holding. enclosing_mod_fn_spans is empty unless the use
        # sits directly in an inline `mod {}` body, so file-level uses are
        # untouched.
        for line, col in rs_utils.enclosing_mod_fn_spans(use_node):
            self._rust_pending_fn_scope_uses.setdefault(module_qn, []).append(
                (line, col, resolved_imports, True)
            )

    def rust_mod_scope_targets(self, writer_qn: str) -> list[str]:
        """What the inline `mod` uses of the file `writer_qn` name.

        Read before `finalise_rust_mod_scope_uses` decides which of them
        commit: until then a parsed or restored file's inline-scope maps are
        held back, and a walk out from the file has to see them (#2805
        review). This run's entries if it parsed the file, else the last
        finalised ones.
        """
        entries = self._rust_pending_mod_scope_uses.get(writer_qn)
        if entries is None:
            entries = self._rust_mod_scope_registry.get(writer_qn, [])
        return [
            target for _key, _pure, imports in entries for target in imports.values()
        ]

    def retract_rust_mod_scope_uses(self, module_qn: str) -> None:
        # Reverse replay so a key shadowing another file's entries (the
        # collision finalise arbitrates later) gets them back exactly.
        for key, name, old in reversed(self._rust_mod_scope_shadows.pop(module_qn, [])):
            target = self.import_mapping.get(key)
            if target is None:
                continue
            if old is None:
                target.pop(name, None)
                if not target:
                    del self.import_mapping[key]
            else:
                target[name] = old

    def _retract_rust_inline_scope_keys(
        self, known_module_paths: Mapping[str, str]
    ) -> None:
        for keys in self._rust_inline_scope_keys.values():
            for key in keys:
                if known_module_paths.get(key):
                    # The key has since become an indexed file's own module
                    # qn (a watch CREATE of the cfg twin's file form): the
                    # map is that file's parse output now, not this
                    # arbitration's to retract. The owner branch keeps no
                    # inline writers for it, so the claim lapses.
                    continue
                self.import_mapping.pop(key, None)
        self._rust_inline_scope_keys = {}

    def finalise_rust_mod_scope_uses(
        self, known_module_paths: Mapping[str, str]
    ) -> None:
        # Arbitrates every deferred sub-scope map with the indexer's own
        # answer (a file on disk that was ignored, excluded, or written in
        # an unindexed language owns nothing). A key whose module qn is a
        # DIFFERENT file's keeps no writers from this side. On an unowned
        # key, PURE module chains oust fn-local or block-local forgeries
        # whatever file either lives in (issue #1017: no key can serve
        # both), and same-purity entries surviving from multiple files are
        # ambiguous cfg or macro twins, dropped rather than guessed.
        # Arbitration runs over the persistent registry of EVERY file's
        # mod-scope uses, not just the pending ones: the watch path parses
        # one file at a time, and a contest decided from the touched
        # file's entries alone would be won by whoever was touched last.
        # Previous commits retract first so a changed outcome cannot leave
        # a stale key standing.
        pending = self._rust_pending_mod_scope_uses
        self._rust_pending_mod_scope_uses = {}
        for writer_qn, entries in pending.items():
            self._rust_mod_scope_registry[writer_qn] = entries
        self._retract_rust_inline_scope_keys(known_module_paths)
        by_key: dict[str, list[tuple[str, bool, dict[str, str]]]] = {}
        for writer_qn, entries in self._rust_mod_scope_registry.items():
            for key, pure, imports in entries:
                by_key.setdefault(key, []).append((writer_qn, pure, imports))
        for key, writers in by_key.items():
            kept = _kept_mod_scope_writers(
                writers, known_module_paths.get(key), known_module_paths
            )
            for writer_qn, _pure, imports in kept:
                self.import_mapping.setdefault(key, {}).update(imports)
                self._rust_inline_scope_keys.setdefault(writer_qn, set()).add(key)

    def finalise_rust_function_scope_uses(
        self,
        module_qn: str,
        function_locations: Mapping[FunctionSpanKey, FunctionLocation],
    ) -> None:
        # Runs after the file's functions and methods register: the span
        # lookup hands back the exact qn the registry assigned (natural or
        # `natural@<start_line>`), which no source-name derivation can
        # reproduce on collisions. A span with no record means the function
        # was never registered (e.g. an impl whose target has no extractable
        # name): no caller exists to read the key, and a name-derived
        # fallback could collide with a real function's qn, so drop it.
        for (
            start_line,
            start_col,
            imports,
            weak,
        ) in self._rust_pending_fn_scope_uses.pop(module_qn, []):
            location = function_locations.get((module_qn, start_line, start_col))
            if location is None:
                continue
            key = location.qualified_name
            if weak:
                # Fanned out from an enclosing impure mod's use: mod-level
                # precedence, so it lives in the weak map the resolver
                # consults only after the function's own body uses and the
                # scope's local items.
                self.rust_fn_scope_mod_imports.setdefault(key, {}).update(imports)
            else:
                self.rust_fn_scope_imports.setdefault(key, {}).update(imports)
            self._rust_fn_scope_keys.setdefault(module_qn, set()).add(key)

    def record_rust_block_items(
        self,
        module_qn: str,
        root_node: Node,
        function_locations: Mapping[FunctionSpanKey, FunctionLocation],
    ) -> None:
        """Record this file's block-local function items by span and by qn.

        Runs beside finalise_rust_function_scope_uses, once the registry
        has handed out the qns (natural or `natural@<start_line>`) these
        spans have to be spoken of by. An item span with no record was
        never registered, so no call can bind it and the name stays with
        whatever else answers to it.
        """
        scopes: list[tuple[int, int, dict[str, str]]] = []
        for start, end, items in rs_utils.rust_block_item_scopes(root_node):
            resolved = {
                name: location.qualified_name
                for name, span in items.items()
                if (location := function_locations.get((module_qn, *span)))
            }
            if not resolved:
                continue
            scopes.append((start, end, resolved))
            self.rust_block_item_qns.update(resolved.values())
        if scopes:
            self.rust_block_items[module_qn] = scopes

    def _rust_dir_entries(self, directory: Path) -> frozenset[str]:
        # Exact-case listing: is_file() answers case-insensitively on
        # macOS/Windows, so probing (dir / "Err.rs") would match err.rs and
        # misclassify a root ITEM as a submodule.
        key = str(directory)
        cached = self._rust_dir_listing.get(key)
        if cached is None:
            try:
                cached = frozenset(entry.name for entry in directory.iterdir())
            except OSError:
                cached = frozenset()
            self._rust_dir_listing[key] = cached
        return cached

    def _rust_file_is_indexed(self, rel_parts: Sequence[str]) -> bool:
        """Whether the graph holds this file, per `--exclude` and `.cgrignore`.

        The redirect sweep already walks with the indexer's predicate (#1088),
        but the per-file declaration reads behind it did not, so a declaration
        in a file the user excluded still decided where an indexed module sits
        (issue #1100).
        """
        if not rel_parts:
            return False
        return not should_skip_rel_file(
            cs.SEPARATOR_SLASH.join(rel_parts),
            tuple(rel_parts[:-1]),
            exclude_paths=self._host.exclude_paths,
            unignore_paths=self._host.unignore_paths,
        )

    def _rust_is_auto_target_dir(self, dir_parts: list[str], stem: str) -> bool:
        # Cargo auto-target locations whose .rs files are their OWN crate
        # roots: src/bin/*.rs, and examples/tests/benches/*.rs plus
        # build.rs at a package root (Cargo.toml beside them). Explicit
        # manifest `path` overrides are checked SEPARATELY and only for
        # the file itself: a non-standard root's modules live in its
        # CONTAINING directory (rustc E0583 on a same-named subdir), so
        # the ancestor walk must never treat a directory as a file crate
        # just because a sibling file is an explicit target.
        if not dir_parts:
            return (
                stem == cs.RS_BUILD_STEM
                and cs.PKG_CARGO_TOML in self._rust_dir_entries(self.repo_path)
                and self._rust_has_auto_build(())
            )
        if len(dir_parts) >= 2 and dir_parts[-1] == cs.RS_BIN_DIR:
            if dir_parts[-2] == cs.LANG_SRC_DIR:
                return self._rust_auto_kind_enabled(
                    tuple(dir_parts[:-2]), cs.RS_MANIFEST_AUTOBINS_KEY
                )
        if dir_parts[-1] in cs.RS_AUTO_TARGET_DIRS:
            return cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*dir_parts[:-1])
            ) and self._rust_auto_kind_enabled(
                tuple(dir_parts[:-1]), cs.RS_AUTO_DIR_KEYS[dir_parts[-1]]
            )
        return False

    def _rust_is_explicit_target(self, dir_parts: list[str], stem: str) -> bool:
        # Whether <dir_parts>/<stem>.rs is an explicit target of its
        # package's manifest (`[[bin]] path = "src/cli.rs"`, or the
        # `[package] build` script override). The package is the NEAREST
        # ancestor directory holding a Cargo.toml.
        for i in range(len(dir_parts), -1, -1):
            pkg_parts = dir_parts[:i]
            if cs.PKG_CARGO_TOML not in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                continue
            relative = cs.SEPARATOR_SLASH.join([*dir_parts[i:], f"{stem}{cs.EXT_RS}"])
            return relative in self._rust_explicit_target_paths(tuple(pkg_parts))
        return False

    def _rust_importer_within_root_file(
        self, base_parts: list[str], base_qn: str, importer_qn: str
    ) -> bool:
        # Whether the importer's module chain is rooted in base_qn's FILE
        # itself: the file's own scope, or inline mods written inside it.
        # The tell is the first segment below the base: an inline mod has
        # no backing file, while a module-directory child is backed by
        # <base dir>/<seg>.rs or <seg>/mod.rs (cargo resolves those into
        # whichever crate DECLARES the module, not the root file).
        if importer_qn == base_qn:
            return True
        if not importer_qn.startswith(f"{base_qn}{cs.SEPARATOR_DOT}"):
            return False
        first = importer_qn[len(base_qn) + 1 :].split(cs.SEPARATOR_DOT, 1)[0]
        child_entries = self._rust_dir_entries(self.repo_path.joinpath(*base_parts))
        if f"{first}{cs.EXT_RS}" in child_entries:
            return False
        if cs.MOD_RS in self._rust_dir_entries(
            self.repo_path.joinpath(*base_parts, first)
        ):
            return False
        return True

    def _rust_has_auto_build(self, pkg_parts: tuple[str, ...]) -> bool:
        self._rust_explicit_target_paths(pkg_parts)
        return self._rust_auto_build_flags.get(pkg_parts, False)

    def _rust_default_target_path(
        self,
        pkg_parts: tuple[str, ...],
        section: str,
        entry: dict,
        manifest: dict,
    ) -> str | None:
        # Cargo resolves a pathless table by matching the target name
        # against its candidate files (src/main.rs for a package-name bin,
        # <kind dir>/<name>.rs, <kind dir>/<name>/main.rs) and errors on an
        # ambiguous pair, so only a single EXISTING candidate resolves here.
        if section == cs.RS_MANIFEST_LIB_SECTION:
            return cs.RS_DEFAULT_LIB_PATH
        kind_dir = cs.RS_MANIFEST_KIND_DIRS.get(section)
        name = entry.get(cs.RS_MANIFEST_NAME_KEY)
        if kind_dir is None or not isinstance(name, str) or not name:
            return None
        candidates = []
        if section == cs.RS_MANIFEST_BIN_SECTION:
            package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
            pkg_name = (
                package.get(cs.RS_MANIFEST_NAME_KEY)
                if isinstance(package, dict)
                else None
            )
            if name == pkg_name:
                candidates.append(cs.RS_DEFAULT_MAIN_PATH)
        candidates.append(f"{kind_dir}{cs.SEPARATOR_SLASH}{name}{cs.EXT_RS}")
        candidates.append(
            f"{kind_dir}{cs.SEPARATOR_SLASH}{name}{cs.SEPARATOR_SLASH}{cs.MAIN_RS}"
        )
        existing = [
            candidate
            for candidate in candidates
            if self._rust_pkg_relative_file_exists(pkg_parts, candidate)
        ]
        return existing[0] if len(existing) == 1 else None

    def _rust_pkg_relative_file_exists(
        self, pkg_parts: tuple[str, ...], relative: str
    ) -> bool:
        parts = relative.split(cs.SEPARATOR_SLASH)
        return parts[-1] in self._rust_dir_entries(
            self.repo_path.joinpath(*pkg_parts, *parts[:-1])
        )

    def _rust_src_auto_entry_flags(self, dir_parts: list[str]) -> tuple[bool, bool]:
        # (lib flag, main flag) for lib.rs/main.rs sitting in THIS directory.
        # The opt-outs govern cargo's auto locations: a package's src/
        # (autolib + autobins), and the MULTI-FILE auto target dirs whose
        # main.rs is the kind's target — src/bin/<name>/ (autobins) and
        # <kind>/<name>/ for examples/tests/benches. Elsewhere both stay
        # enabled.
        location = cargo.auto_location(dir_parts)
        if location is None:
            return True, True
        pkg_parts, key, nested = location
        if cs.PKG_CARGO_TOML not in self._rust_dir_entries(
            self.repo_path.joinpath(*pkg_parts)
        ):
            return True, True
        if key is None:
            # A package's src/: lib.rs follows autolib, main.rs autobins.
            return (
                self._rust_auto_kind_enabled(pkg_parts, cs.RS_MANIFEST_AUTOLIB_KEY),
                self._rust_auto_kind_enabled(pkg_parts, cs.RS_MANIFEST_AUTOBINS_KEY),
            )
        enabled = self._rust_auto_kind_enabled(pkg_parts, key)
        # A multi-file target compiles <name>/main.rs; a lib.rs there is never
        # a cargo target, so the lib flag is off in nested dirs. Directly in a
        # kind dir (or src/bin) every .rs is that kind's target, so both entry
        # stems follow the kind's flag.
        return (False, enabled) if nested else (enabled, enabled)

    def _rust_auto_kind_enabled(self, pkg_parts: tuple[str, ...], key: str) -> bool:
        # Cargo's per-kind discovery opt-outs (`autobins = false` and
        # siblings in [package]): a disabled kind's auto-location files are
        # plain non-target files unless an explicit manifest target names
        # them (issue #1030). Only an explicit false disables — unset and
        # true both mean auto-discovery.
        self._rust_explicit_target_paths(pkg_parts)
        return self._rust_auto_discovery_flags.get(pkg_parts, {}).get(key, True)

    def _rust_explicit_entry_files(self, dir_parts: tuple[str, ...]) -> set[str]:
        # File names of explicit manifest targets that sit DIRECTLY in
        # this directory (their declarations join the entry-declaration
        # map beside lib.rs/main.rs, keyed by stem). The package is the
        # nearest ancestor holding a Cargo.toml.
        for i in range(len(dir_parts), -1, -1):
            pkg_parts = dir_parts[:i]
            if cs.PKG_CARGO_TOML not in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                continue
            names: set[str] = set()
            for rel in self._rust_explicit_target_paths(tuple(pkg_parts)):
                full = [*pkg_parts, *rel.split(cs.SEPARATOR_SLASH)]
                if tuple(full[:-1]) == dir_parts and full[-1].endswith(cs.EXT_RS):
                    names.add(full[-1])
            return names
        return set()

    def _rust_explicit_target_paths(self, pkg_parts: tuple[str, ...]) -> frozenset[str]:
        cached = self._rust_explicit_targets.get(pkg_parts)
        if cached is not None:
            return cached
        paths: set[str] = set()
        manifest = self._rust_read_manifest(self.repo_path.joinpath(*pkg_parts))
        for section in cs.RS_MANIFEST_TARGET_SECTIONS:
            for entry in cargo.manifest_section_tables(manifest, section):
                if isinstance(path := entry.get(cs.RS_MANIFEST_PATH_KEY), str):
                    paths.add(cargo.norm_manifest_path(path))
                elif default := self._rust_default_target_path(
                    pkg_parts, section, entry, manifest
                ):
                    # A PATHLESS table is still an explicit target at its
                    # conventional location ([lib] means src/lib.rs), and an
                    # explicit target must survive its kind's auto-discovery
                    # opt-out (issue #1030).
                    paths.add(default)
        package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
        if not isinstance(package, dict):
            package = {}
        # `[package] build = "..."` overrides the build script location;
        # the named file is a crate root like any explicit target.
        build_value = package.get(cs.RS_MANIFEST_BUILD_KEY)
        if isinstance(build_value, str):
            paths.add(cargo.norm_manifest_path(build_value))
        # Unset AND `build = true` both mean auto-detection
        # (cargo-verified: `build = true` compiles build.rs exactly like
        # unset); a string names the script explicitly, false disables.
        self._rust_auto_build_flags[pkg_parts] = (
            build_value is None or build_value is True
        )
        self._rust_auto_discovery_flags[pkg_parts] = {
            key: False for key in cs.RS_MANIFEST_AUTO_KEYS if package.get(key) is False
        }
        result = frozenset(paths)
        self._rust_explicit_targets[pkg_parts] = result
        return result

    def _rust_read_manifest(self, directory: Path) -> dict:
        try:
            return tomllib.loads(
                (directory / cs.PKG_CARGO_TOML).read_text(
                    encoding=cs.RS_ENCODING_UTF8, errors="ignore"
                )
            )
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def _rust_workspace_crate_roots(
        self,
    ) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], str]]:
        """Workspace crate names -> (package dir, lib-root dir, entry stem).

        Cargo resolves a `use` head naming another crate through the
        [dependencies] graph; indexing has no lockfile, so the root
        manifest's workspace members (plus the root package itself, an
        implicit member that integration tests import by name) stand in:
        every member with a lib target is importable by its lib name
        ([package] name unless [lib] name overrides), hyphens spelled as
        underscores in code (issue #1033). The package dir feeds the
        per-importer dependency gate.
        """
        if self._rust_workspace_crates is not None:
            return self._rust_workspace_crates
        mapping: dict[str, tuple[tuple[str, ...], tuple[str, ...], str]] = {}
        for member in self._rust_workspace_member_dirs():
            manifest = self._rust_read_manifest(member)
            package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
            if not isinstance(package, dict):
                continue
            name = package.get(cs.RS_MANIFEST_NAME_KEY)
            # `[lib] name` overrides the import spelling: dependents must
            # write the lib target's name, not the package's.
            lib = manifest.get(cs.RS_MANIFEST_LIB_SECTION)
            if isinstance(lib, dict) and isinstance(
                lib_name := lib.get(cs.RS_MANIFEST_NAME_KEY), str
            ):
                name = lib_name
            if not isinstance(name, str) or not name:
                continue
            try:
                pkg = member.relative_to(self.repo_path).parts
            except ValueError:
                # A member outside the repo (`members = ["../shared"]`) has
                # no project qn to rewrite its crate name to.
                continue
            if (root := self._rust_member_lib_root(member, manifest)) is not None:
                key = name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE)
                mapping[key] = (pkg, *root)
        self._rust_workspace_crates = mapping
        return mapping

    def _rust_workspace_member_dirs(self) -> list[Path]:
        dirs = [self.repo_path]
        workspace = self._rust_read_manifest(self.repo_path).get(
            cs.RS_MANIFEST_WORKSPACE_KEY
        )
        members = (
            workspace.get(cs.RS_MANIFEST_MEMBERS_KEY)
            if isinstance(workspace, dict)
            else None
        )
        if not isinstance(members, list):
            return dirs
        for pattern in members:
            if not isinstance(pattern, str):
                continue
            # `"."` (or `"./"`) is the root package joining its own
            # workspace, and the root is already listed; Python 3.12's
            # `Path.glob` raises IndexError / AttributeError on those two
            # rather than ValueError (issue #3167).
            pattern = cargo.member_pattern(pattern)
            if not pattern:
                continue
            try:
                matches = sorted(self.repo_path.glob(pattern))
            except Exception as error:  # noqa: BLE001 -- an odd member pattern leaves one crate unmapped
                # One member the manifest spells oddly is one crate left
                # unmapped, never a failed sync.
                logger.debug(
                    ls.RUST_WORKSPACE_MEMBER_UNEXPANDED, pattern=pattern, error=error
                )
                continue
            dirs.extend(match for match in matches if match.is_dir())
        return dirs

    def _rust_member_lib_root(
        self, member: Path, manifest: dict
    ) -> tuple[tuple[str, ...], str] | None:
        # Only a lib target is importable by crate name; a bin-only member
        # cannot appear as a use head in compiling code.
        try:
            rel = member.relative_to(self.repo_path).parts
        except ValueError:
            return None
        lib = manifest.get(cs.RS_MANIFEST_LIB_SECTION)
        if isinstance(lib, dict) and isinstance(
            path := lib.get(cs.RS_MANIFEST_PATH_KEY), str
        ):
            parts = cargo.norm_manifest_path(path).split(cs.SEPARATOR_SLASH)
            if parts[-1].endswith(cs.EXT_RS):
                return (*rel, *parts[:-1]), parts[-1][: -len(cs.EXT_RS)]
        # A pathless [lib] table is still an explicit target at the default
        # src/lib.rs; only a genuinely undeclared library respects the
        # autolib opt-out (issue #1030).
        package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
        auto_lib = not (
            isinstance(package, dict)
            and package.get(cs.RS_MANIFEST_AUTOLIB_KEY) is False
        )
        if (auto_lib or isinstance(lib, dict)) and cs.LIB_RS in self._rust_dir_entries(
            member / cs.LANG_SRC_DIR
        ):
            return (*rel, cs.LANG_SRC_DIR), "lib"
        return None

    def _rust_enclosing_package(self, module_qn: str) -> tuple[str, ...] | None:
        # The nearest ancestor directory holding a Cargo.toml, from the
        # module's own directory upward (inline-mod qn segments probe
        # nonexistent dirs harmlessly on the way).
        parts = module_qn.split(cs.SEPARATOR_DOT)[1:]
        for i in range(len(parts), -1, -1):
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*parts[:i])
            ):
                return tuple(parts[:i])
        return None

    def _rust_package_deps(
        self, pkg_parts: tuple[str, ...]
    ) -> dict[str, tuple[str, ...] | None]:
        if (cached := self._rust_pkg_deps.get(pkg_parts)) is not None:
            return cached
        manifest = self._rust_read_manifest(self.repo_path.joinpath(*pkg_parts))
        deps: dict[str, tuple[str, ...] | None] = {}
        for key, value in cargo.manifest_dep_entries(manifest):
            name = key.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE)
            resolved = self._rust_dep_target_dir(pkg_parts, key, value)
            # A path target from any section wins over a registry
            # spelling elsewhere (version + path in one entry is the
            # common published-workspace shape).
            if resolved is not None or name not in deps:
                deps[name] = resolved
        self._rust_pkg_deps[pkg_parts] = deps
        return deps

    def _rust_dep_target_dir(
        self, pkg_parts: tuple[str, ...], key: str, value: str | dict[str, object]
    ) -> tuple[str, ...] | None:
        # Repo-relative dir of the package this dependency entry binds
        # to, or None when it resolves outside the repo (registry
        # version, git). A `package =` rename changes the name the entry
        # is spoken by, not where it lives: an entry carrying a path
        # still binds that directory.
        if not isinstance(value, dict):
            return None
        base = self.repo_path.joinpath(*pkg_parts)
        if value.get(cs.RS_MANIFEST_WORKSPACE_KEY) is True:
            entry = self._rust_workspace_dep_entry(key)
            if entry is None:
                return None
            value = entry
            base = self.repo_path
        path = value.get(cs.RS_MANIFEST_PATH_KEY)
        if not isinstance(path, str):
            return None
        try:
            return (
                (base / cargo.norm_manifest_path(path))
                .resolve()
                .relative_to(self.repo_path.resolve())
                .parts
            )
        except (OSError, ValueError):
            return None

    def _rust_member_rewrite_allowed(
        self, member_pkg: tuple[str, ...], module_qn: str
    ) -> bool:
        # Cargo binds a crate-name head only to a dependency the
        # importing package declares, or to the package's OWN lib (its
        # integration tests, benches, and bins import it by name): a
        # same-named registry dependency keeps the head external.
        importer_pkg = self._rust_enclosing_package(module_qn)
        if importer_pkg is None or importer_pkg == member_pkg:
            return True
        return member_pkg in self._rust_package_deps(importer_pkg).values()

    def rust_head_is_external_dep(self, head: str, module_qn: str) -> bool:
        """Whether the importer's manifest proves this head external.

        A dependency entry that resolves to no repo directory (registry
        version, git, renamed package) speaks for the name, so the call
        resolver can decide a drop instead of leaving the trie to guess
        (issue #1033).
        """
        pkg = self._rust_enclosing_package(module_qn)
        if pkg is None:
            return False
        deps = self._rust_package_deps(pkg)
        return head in deps and deps[head] is None

    def rust_crate_reaches(self, module_qn: str, target_qn: str) -> bool:
        """Whether code in `module_qn` can call into the crate of `target_qn`.

        Cargo lets a crate name items of itself and of the packages its
        manifest depends on, directly or through them, dev- and
        build-dependencies included, and nothing else. A name-based
        candidate in any other crate is impossible, however close its name
        (issue #2622: ripgrep's `globset` "called" into `ignore`, which
        depends on it). A side outside every package manifest proves
        nothing, so it reaches.
        """
        closure = self._rust_dependency_closure(self._rust_package_of(module_qn))
        if closure is None:
            return True
        target = self._rust_package_of(target_qn)
        return (
            target is None
            or target in closure
            or self._rust_dependency_closure(target) is None
        )

    def _rust_package_of(self, qn: str) -> tuple[str, ...] | None:
        if qn in self._rust_qn_packages:
            return self._rust_qn_packages[qn]
        package = self._rust_enclosing_package(qn)
        self._rust_qn_packages[qn] = package
        return package

    def _rust_dependency_closure(
        self, pkg: tuple[str, ...] | None
    ) -> frozenset[tuple[str, ...]] | None:
        if pkg is None:
            return None
        if pkg in self._rust_dep_closures:
            return self._rust_dep_closures[pkg]
        closure: frozenset[tuple[str, ...]] | None = None
        manifest = self._rust_read_manifest(self.repo_path.joinpath(*pkg))
        if isinstance(manifest.get(cs.RS_MANIFEST_PACKAGE_KEY), dict):
            seen = {pkg}
            pending = [pkg]
            while pending:
                for target in self._rust_dep_targets(pending.pop()):
                    if target not in seen:
                        seen.add(target)
                        pending.append(target)
            closure = frozenset(seen)
        self._rust_dep_closures[pkg] = closure
        return closure

    def _rust_dep_targets(self, pkg: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
        # The repo package each dependency of `pkg` fetches: a path entry's
        # dir, or the workspace member a pathless entry names, since a member
        # declared by version alone is still the member the repo holds (the
        # published-workspace shape, #1048).
        members = self._rust_dep_members_of(pkg)
        for name, target in self._rust_package_deps(pkg).items():
            found = target if target is not None else members.get(name)
            if found is not None:
                yield found

    def rust_head_is_member_dep(self, head: str, module_qn: str) -> bool:
        """Whether the importer's manifest fetches this head as a member crate.

        A pathless entry names the PACKAGE it fetches, by its key or by a
        `package =` rename that leaves the key as the name the code writes
        (`base-alias = { version = "1", package = "my-base" }`, then
        `base_alias::Meter`). Naming a workspace member, it binds the crate
        the repo holds, exactly as a version-only entry spelled with the
        member's own name does (issue #1048), though no lib is called
        `base_alias` (PR #2791).
        """
        pkg = self._rust_package_of(module_qn)
        return pkg is not None and head in self._rust_dep_members_of(pkg)

    def _rust_dep_members_of(self, pkg: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        if (cached := self._rust_dep_members.get(pkg)) is not None:
            return cached
        manifest = self._rust_read_manifest(self.repo_path.joinpath(*pkg))
        found: dict[str, tuple[str, ...]] = {}
        for key, value in cargo.manifest_dep_entries(manifest):
            if self._rust_dep_target_dir(pkg, key, value) is not None:
                continue
            member = self._rust_fetched_member(key, value)
            if member is not None:
                found.setdefault(
                    key.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE), member
                )
        self._rust_dep_members[pkg] = found
        return found

    def _rust_fetched_member(self, key: str, value: object) -> tuple[str, ...] | None:
        """The repo package a pathless dependency entry builds against.

        A crates.io entry stands for the workspace member of its package
        name, the published-workspace shape (#1048). A git, alternate
        registry or out-of-repo path entry fetches another copy of the
        package, which only a root `[patch.<source>]` path makes a repo
        package (PR #2791). A patch of the package decides for its
        source either way, the crates.io name rule included.
        `workspace = true` inherits the root's [workspace.dependencies]
        entry, source, rename and requirement included.
        """
        if isinstance(value, dict) and value.get(cs.RS_MANIFEST_WORKSPACE_KEY) is True:
            value = self._rust_workspace_dep_value(key)
        source = cargo.dep_source(value)
        if source is None:
            return None
        package = cargo.dep_package(key, value)
        if patches := self._rust_patch_entries(source, package):
            return self._rust_patch_target(patches, cargo.dep_requirement(value))
        if source != cs.RS_DEFAULT_REGISTRY:
            return None
        return self._rust_member_package_dirs().get(package)

    def _rust_patch_entries(
        self, source: str, package: str
    ) -> list[tuple[str, dict[str, object]]]:
        # Every entry of the root manifest's `[patch.<source>]` table (a git
        # URL as written, or a registry name) that patches `package`: one
        # table may list it at several versions, under its own name and
        # under other keys that name it through `package =`.
        patch = self._rust_read_manifest(self.repo_path).get(cs.RS_MANIFEST_PATCH_KEY)
        entries = patch.get(source) if isinstance(patch, dict) else None
        if not isinstance(entries, dict):
            return []
        return [
            (key, value)
            for key, value in entries.items()
            if isinstance(value, dict) and cargo.dep_package(key, value) == package
        ]

    def _rust_patch_target(
        self, patches: list[tuple[str, dict[str, object]]], requirement: str | None
    ) -> tuple[str, ...] | None:
        """The repo dir a patch puts in place of the dependency, if Cargo uses one.

        Only a path patch lands in the repo, and Cargo applies one only when
        the patched package's version meets the dependency's requirement;
        otherwise it warns "patch was not used" and builds against the
        source's own copy, which no local call reaches (PR #2791).
        Among several patched versions it takes the highest that qualifies,
        wherever the table lists it, a git or out-of-repo patch included
        (PR #2791). A dependency stating no requirement takes any of
        them; a version that cannot be read or checked never meets a
        requirement, so the dependency stays external.
        """
        local: list[tuple[str | None, tuple[str, ...]]] = []
        foreign: list[str | None] = []
        for key, value in patches:
            target = self._rust_dep_target_dir((), key, value)
            if target is None:
                own = cargo.dep_requirement(value)
                if cargo.patch_may_qualify(own, requirement):
                    foreign.append(own)
                continue
            version = self._rust_package_version(target)
            if requirement is None or (
                version is not None and cargo_semver.satisfies(version, requirement)
            ):
                local.append((version, target))
        return cargo.patch_winner(local, foreign)

    def _rust_package_version(self, pkg: tuple[str, ...]) -> str | None:
        # A package's `[package] version`, or the root `[workspace.package]`
        # one it inherits through `version.workspace = true`.
        package = self._rust_read_manifest(self.repo_path.joinpath(*pkg)).get(
            cs.RS_MANIFEST_PACKAGE_KEY
        )
        version = (
            package.get(cs.RS_MANIFEST_VERSION_KEY)
            if isinstance(package, dict)
            else None
        )
        if (
            isinstance(version, dict)
            and version.get(cs.RS_MANIFEST_WORKSPACE_KEY) is True
        ):
            workspace = self._rust_read_manifest(self.repo_path).get(
                cs.RS_MANIFEST_WORKSPACE_KEY
            )
            inherited = (
                workspace.get(cs.RS_MANIFEST_PACKAGE_KEY)
                if isinstance(workspace, dict)
                else None
            )
            version = (
                inherited.get(cs.RS_MANIFEST_VERSION_KEY)
                if isinstance(inherited, dict)
                else None
            )
        return version if isinstance(version, str) else None

    def _rust_member_package_dirs(self) -> dict[str, tuple[str, ...]]:
        # Workspace members with a lib target, by [package] name
        # (underscore-spelled) -> package dir. A dependency entry names the
        # package, never the lib target, whose `[lib] name` may differ.
        if self._rust_member_packages is not None:
            return self._rust_member_packages
        mapping: dict[str, tuple[str, ...]] = {}
        for member in self._rust_workspace_member_dirs():
            manifest = self._rust_read_manifest(member)
            package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
            name = (
                package.get(cs.RS_MANIFEST_NAME_KEY)
                if isinstance(package, dict)
                else None
            )
            if (
                isinstance(name, str)
                and name
                and self._rust_member_lib_root(member, manifest) is not None
            ):
                mapping.setdefault(
                    name.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE),
                    member.relative_to(self.repo_path).parts,
                )
        self._rust_member_packages = mapping
        return mapping

    def _rust_workspace_dep_entry(self, key: str) -> dict | None:
        # The root manifest's [workspace.dependencies] entry a member's
        # `key = { workspace = true }` inherits, when it is a table.
        entry = self._rust_workspace_dep_value(key)
        return entry if isinstance(entry, dict) else None

    def _rust_workspace_dep_value(self, key: str) -> object:
        # That entry as written: a table, a bare version string, or None.
        workspace = self._rust_read_manifest(self.repo_path).get(
            cs.RS_MANIFEST_WORKSPACE_KEY
        )
        deps = (
            workspace.get(cs.RS_MANIFEST_DEP_SECTIONS[0])
            if isinstance(workspace, dict)
            else None
        )
        return deps.get(key) if isinstance(deps, dict) else None

    def rust_head_is_repo_crate(self, head: str) -> bool:
        """Whether this use head names a lib crate the repo itself holds.

        A manifest says how a crate is FETCHED, which is a different
        question from whether the repo indexes it: a workspace sibling may
        be declared by version alone (the published-workspace shape), and
        the entry then resolves to no repo directory even though the
        crate's own sources sit right here (issue #1048).
        """
        return head in self._rust_workspace_crate_roots()

    def _rust_crate_root(self, module_qn: str) -> tuple[str, list[str]] | None:
        """The file's crate root: ("classic", dir parts) or ("file", qn parts).

        Classic roots are lib.rs/main.rs entries found by walking the file's
        ancestor directories (Cargo's default layout puts them under src/,
        but path overrides may place them anywhere: ripgrep roots at
        crates/core/main.rs). File roots are Cargo auto-targets
        (src/bin/tool.rs) that root their own crate; both their items and
        their submodules nest under the entry file's qn.
        """
        qn_parts = module_qn.split(cs.SEPARATOR_DOT)[1:]
        if not qn_parts:
            # A root-level explicit `path = "mod.rs"` target maps to the
            # bare project qn; nothing else roots there (issue #1031).
            if self._rust_is_mod_rs_target([]):
                return "dir_file", []
            return None
        dir_parts, stem = qn_parts[:-1], qn_parts[-1]
        if (own_kind := self._rust_own_target_kind(dir_parts, stem)) is not None:
            return own_kind, qn_parts
        if self._rust_is_mod_rs_target(qn_parts):
            # Cargo compiles src/bin/mod.rs (or an explicit target whose
            # path ends in mod.rs) as a target named `mod` whose crate root
            # is the file itself. The mod.rs spelling maps the module to its
            # DIRECTORY qn, so that qn is the file root and `mod x;` inside
            # it resolves beside it — file-root nesting whose declarations
            # come from mod.rs, not a sibling `<dir>.rs` shadow
            # (issue #1031).
            return "dir_file", qn_parts
        return self._rust_ancestor_crate_root(dir_parts)

    def _rust_own_target_kind(self, dir_parts: list[str], stem: str) -> str | None:
        """ "file"/"entry" when the file `<stem>.rs` is a target of its own.

        An explicit manifest target is a crate root like any entry file:
        its `mod` declarations resolve in the CONTAINING directory (the
        tests/common.rs idiom, cargo-verified), so it attaches classically
        with itself as the definitive entry stem. The same holds for an
        entry-stem FILE that is a target in its own right — by auto
        location (src/bin/main.rs beside src/bin/mod.rs) or by explicit
        manifest path — it keeps its own crate: its qn carries the stem
        only when a sibling claimed the dir qn, and the ancestor mod.rs
        check must not swallow it (issue #1031).
        """
        if f"{stem}{cs.EXT_RS}" not in self._rust_dir_entries(
            self.repo_path.joinpath(*dir_parts)
        ):
            return None
        if self._rust_is_auto_target_dir(dir_parts, stem):
            return "file"
        if self._rust_is_explicit_target(dir_parts, stem):
            return "entry"
        return None

    def _rust_ancestor_crate_root(
        self, dir_parts: list[str]
    ) -> tuple[str, list[str]] | None:
        for i in range(len(dir_parts), -1, -1):
            if i >= 1:
                name = dir_parts[i - 1]
                parent = dir_parts[: i - 1]
                if f"{name}{cs.EXT_RS}" in self._rust_dir_entries(
                    self.repo_path.joinpath(*parent)
                ) and self._rust_is_auto_target_dir(parent, name):
                    return "file", dir_parts[:i]
            if self._rust_is_mod_rs_target(dir_parts[:i]):
                # A DESCENDANT module of a mod.rs-backed target (declared
                # from src/bin/mod.rs) roots at that target's directory qn,
                # exactly like the target itself (issue #1031).
                return "dir_file", dir_parts[:i]
            if self._rust_is_crate_root_dir(dir_parts[:i]):
                return "classic", dir_parts[:i]
        return None

    def _rust_is_mod_rs_target(self, dir_parts: list[str]) -> bool:
        mod_stem = cs.MOD_RS[: -len(cs.EXT_RS)]
        return cs.MOD_RS in self._rust_dir_entries(
            self.repo_path.joinpath(*dir_parts)
        ) and (
            self._rust_is_auto_target_dir(dir_parts, mod_stem)
            or self._rust_is_explicit_target(dir_parts, mod_stem)
        )

    def _rust_is_crate_root_dir(self, dir_parts: list[str]) -> bool:
        # A directory is a crate root only when it holds an entry file AND is
        # not itself a MODULE directory of an enclosing tree: src/app/ with
        # an incidental module file main.rs is app.rs's module dir (the
        # sibling app.rs is the tell), not a crate (verified against rustc:
        # self::foo inside app::main is app::main::foo).
        entries = self._rust_dir_entries(self.repo_path.joinpath(*dir_parts))
        explicit_names = self._rust_explicit_entry_files(tuple(dir_parts))
        auto_lib, auto_bins = self._rust_src_auto_entry_flags(dir_parts)
        # A physical lib.rs/main.rs counts only while its kind's discovery
        # opt-out is unset, or when the manifest names it explicitly
        # (issue #1030).
        lib_roots = cs.LIB_RS in entries and (auto_lib or cs.LIB_RS in explicit_names)
        main_roots = cs.MAIN_RS in entries and (
            auto_bins or cs.MAIN_RS in explicit_names
        )
        if (
            not lib_roots
            and not main_roots
            and not any(name in entries for name in explicit_names)
        ):
            # An explicit manifest target is an entry too: a package whose
            # ONLY entry is `[[bin]] path = "src/cli.rs"` still roots its
            # declared submodules here (cargo-verified), with the entry
            # stem chosen by the declaring scan over the explicit stems.
            return False
        if cs.MOD_RS in entries and not self._rust_is_auto_target_dir(
            dir_parts, cs.MOD_RS[: -len(cs.EXT_RS)]
        ):
            # The mod.rs spelling of a module directory — except in a direct
            # auto-target location, where mod.rs is itself a target named
            # `mod` and must not stop its main.rs sibling from rooting the
            # directory (issue #1031).
            return False
        if dir_parts and f"{dir_parts[-1]}{cs.EXT_RS}" in self._rust_dir_entries(
            self.repo_path.joinpath(*dir_parts[:-1])
        ):
            return False
        return True

    def _rust_entry_stem(
        self, dir_parts: list[str], module_qn: str
    ) -> tuple[str, bool]:
        """Entry-file stem (lib/main) of the crate that contains module_qn.

        src/lib.rs + src/main.rs in one package is a standard layout and the
        two are DIFFERENT crates: the file's crate is the entry whose `mod`
        declarations reach the file's top-level module segment. An entry file
        is its own crate; ties prefer lib.rs. The second element is True when
        the choice is definitive (the importer IS an entry, or exactly one
        entry declares it): a definitive crate must never be overridden by
        the item tie-break in _rust_attach, because `crate::` in a module the
        other entry does not declare can never reach that other crate.
        """
        decls = self._rust_entry_decls(dir_parts)
        segments = module_qn.split(cs.SEPARATOR_DOT)[1 + len(dir_parts) :]
        top = segments[0] if segments else ""
        if top in decls and top in ("lib", "main"):
            # Only a real entry stem short-circuits: an explicit target's
            # stem in the map (its declarations feed _rust_attach and the
            # declaring scan below) must not claim the MODULE directory
            # sharing its name, whose files belong to whichever crate
            # declares that module (cargo-verified: src/cli/sub.rs builds
            # into the lib when lib.rs declares `mod cli;`).
            return top, True
        declaring = [stem for stem, entry in decls.items() if top in entry.mods]
        if declaring:
            return declaring[0], len(declaring) == 1
        for stem in ("lib", "main"):
            if stem in decls:
                return stem, False
        if (lone := self._rust_lone_explicit_stem(dir_parts, decls)) is not None:
            return lone, True
        return "lib", False

    def _rust_lone_explicit_stem(
        self, dir_parts: list[str], decls: dict[str, rs_scan.RustEntryDecls]
    ) -> str | None:
        entries = self._rust_dir_entries(self.repo_path.joinpath(*dir_parts))
        if (
            cs.LIB_RS not in entries
            and cs.MAIN_RS not in entries
            and not self._rust_is_auto_target_dir(list(dir_parts), "")
        ):
            # Never in an auto-target location: every .rs sibling there
            # is its own crate root by LOCATION, invisible to both the
            # manifest walk and the declaration map, so a lone explicit
            # stem can never prove itself the directory's only root
            # (tests/common/mod.rs belongs to whichever sibling declares
            # it; an undeclared module keeps the ambiguity phantom).
            build_file = f"{cs.RS_BUILD_STEM}{cs.EXT_RS}"
            build_hole = (
                cs.PKG_CARGO_TOML in entries
                and build_file in entries
                and self._rust_has_auto_build(tuple(dir_parts))
                and cs.RS_BUILD_STEM not in decls
            )
            present = sorted(
                name
                for name in self._rust_explicit_entry_files(tuple(dir_parts))
                if name in entries
            )
            # Stems that declare nothing beyond the universal `fn main`
            # cannot claim any module (a trivial build script), so they
            # do not block the lone target; an unreadable build.rs is a
            # hole exactly like an unreadable lib.rs and does.
            claiming = {
                stem
                for stem, entry in decls.items()
                if entry.mods or (entry.items - {"main"})
            }
            if (
                not build_hole
                and len(present) == 1
                and (stem := present[0][: -len(cs.EXT_RS)]) in decls
                and claiming <= {stem}
            ):
                # A genuinely explicit-only package with a single target
                # whose declarations actually loaded: fall back to that
                # stem, or _rust_attach's entry-declaration and
                # item-tie-break branches silently disable and only the
                # filesystem probe survives, binding sibling decoy
                # files. Every hole keeps the phantom fallback instead:
                # an unreadable lib.rs OR an unreadable second target
                # must not let the survivor claim the package (a
                # dangling phantom revives nothing; a definitive wrong
                # stem suppresses the tie-break too). Multi-target
                # no-declarer stays genuine ambiguity, likewise phantom.
                return stem
        return None

    def _rust_entry_decls(
        self, dir_parts: list[str]
    ) -> dict[str, rs_scan.RustEntryDecls]:
        """Per entry stem: mod declarations, item names, `#[path]` targets."""
        key = tuple(dir_parts)
        decls = self._rust_entry_mod_decls.setdefault(key, {})
        entries = self._rust_dir_entries(self.repo_path.joinpath(*dir_parts))
        # src/lib.rs and src/main.rs are auto-discovered only while their
        # kind's opt-out is unset; an explicit manifest target re-adds the
        # file below regardless (issue #1030).
        auto_lib, auto_bins = self._rust_src_auto_entry_flags(dir_parts)
        scan = [
            name
            for name, enabled in ((cs.LIB_RS, auto_lib), (cs.MAIN_RS, auto_bins))
            if enabled
        ]
        if cs.PKG_CARGO_TOML in entries and self._rust_has_auto_build(key):
            # build.rs beside the manifest is cargo's fifth auto crate
            # root, but only while [package] build is UNSET (a string
            # names the script explicitly and false disables it; either
            # way cargo never compiles the auto file). Its declarations
            # anchor its modules in the build-script crate, and its stem
            # in the map keeps the explicit-only fallback honest.
            scan.append(f"{cs.RS_BUILD_STEM}{cs.EXT_RS}")
        scan.extend(
            sorted(
                name
                for name in self._rust_explicit_entry_files(key)
                if name not in scan
            )
        )
        for entry in scan:
            if entry not in entries:
                continue
            if not self._rust_file_is_indexed([*dir_parts, entry]):
                # An excluded entry file's `mod` declarations shape crate path
                # and gate resolution for modules the graph does hold.
                continue
            stem = entry.rsplit(cs.SEPARATOR_DOT, 1)[0]
            if stem in decls:
                continue
            try:
                source = self.repo_path.joinpath(*dir_parts, entry).read_text(
                    encoding=cs.RS_ENCODING_UTF8, errors="ignore"
                )
            except OSError:
                # The listing says the entry exists but the read failed: a
                # storm's transient absence. Leave the stem unfilled so the
                # next access retries; caching EMPTY declarations here
                # flips definitive crate attributions to the item
                # tie-break's real but wrong answer.
                continue
            top_level = rs_scan.top_level_only(
                rs_scan.strip_comments_and_strings(source)
            )
            decls[stem] = rs_scan.entry_decls_of(top_level)
        return decls

    def _rust_module_decls(
        self,
        module_parts: list[str],
        dir_backed: bool = False,
        want_mods: bool = False,
    ) -> tuple[rs_scan.RustEntryDecls, list[str]] | None:
        """Declarations of the file backing a module, and its `#[path]` base.

        `src/engine.rs` and `src/engine/mod.rs` both back `src.engine`, and a
        redirect written in either counts from the directory the file sits in.
        Declaring both is a rustc error, so either may be preferred, EXCEPT
        when the caller already knows the module is directory-backed. None
        when no file backs the module: an inline `mod`, or a segment that was
        never a module at all. Only redirects survive the prescan below, so a
        caller reading any other field must ask for the full scan.
        """
        key = (tuple(module_parts), dir_backed, want_mods)
        if key in self._rust_module_mod_decls:
            return self._rust_module_mod_decls[key]
        if not module_parts and not dir_backed:
            # Only a dir-backed root can live at the bare project qn: a
            # root-level explicit `path = "mod.rs"` target (issue #1031).
            return None
        parent = module_parts[:-1]
        # A file the indexer skipped backs nothing: the graph holds no such
        # module, so its declarations must not decide where an indexed one
        # sits. Falling through lets mod.rs back the module when only the
        # sibling .rs was excluded (issue #1100).
        sibling = f"{module_parts[-1]}{cs.EXT_RS}" if module_parts else ""
        if (
            not dir_backed
            and module_parts
            and sibling in self._rust_dir_entries(self.repo_path.joinpath(*parent))
            and self._rust_file_is_indexed([*parent, sibling])
        ):
            path, base = (
                self.repo_path.joinpath(*parent, sibling),
                parent,
            )
        elif cs.MOD_RS in self._rust_dir_entries(
            self.repo_path.joinpath(*module_parts)
        ) and self._rust_file_is_indexed([*module_parts, cs.MOD_RS]):
            path, base = (
                self.repo_path.joinpath(*module_parts, cs.MOD_RS),
                module_parts,
            )
        else:
            self._rust_module_mod_decls[key] = None
            return None
        try:
            source = path.read_text(encoding=cs.RS_ENCODING_UTF8, errors="ignore")
        except OSError:
            # Uncached, so the next access retries: a storm's transient
            # absence must not bake in "this module declares nothing".
            return None
        if not want_mods and rs_scan.PATH_ATTRIBUTE_PATTERN.search(source) is None:
            # Only redirects are read here, and both the lexer and the
            # declaration scan behind them cost far more than this prescan
            # (the scan is superlinear in file length, so a 30k-line
            # generated module spends tens of seconds proving it declares no
            # redirect). Searching the RAW source overshoots by the
            # commented-out and quoted spellings, which merely fall through
            # to the real scan; nothing it misses can be a redirect.
            found = (rs_scan.RustEntryDecls(set(), set(), {}), list(base))
        else:
            found = (
                rs_scan.entry_decls_of(
                    rs_scan.top_level_only(rs_scan.strip_comments_and_strings(source))
                ),
                list(base),
            )
        self._rust_module_mod_decls[key] = found
        return found

    def _rust_module_is_declared(self, parts: list[str]) -> bool:
        """Whether this module's own physical neighbour declares it.

        A file that is BOTH declared where it sits and named by a `#[path]`
        elsewhere is an ordinary module of the tree its own declaration sits
        in: rustc compiles it into both, and only the neighbour's spelling
        keeps `super::` inside it pointing where the file physically is.
        """
        if not parts:
            return False
        parent, name = parts[:-1], parts[-1]
        found = self._rust_module_decls(parent, want_mods=True)
        if found is not None and name in found[0].mods:
            return True
        return any(
            name in decls.mods for decls in self._rust_entry_decls(parent).values()
        )

    def _rust_redirect_parent_map(self) -> dict[str, str]:
        """Every moved module's qn, mapped to the module declaring it.

        Built by one sweep of the repository's Rust sources: a file cannot say
        who declares it, and the declaration may sit in any file at all. The
        prescan is what keeps that affordable, since a source with no
        `#[path = "..."]` anywhere in it costs one regex search and no more.
        """
        if self._rust_redirect_parents is not None:
            return self._rust_redirect_parents
        parents: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(self.repo_path):
            try:
                here = Path(dirpath).relative_to(self.repo_path).parts
            except ValueError:
                continue
            # Sorted so the tie-break below is the same on every filesystem,
            # and pruned by the very predicate the indexer walks with: a
            # declaration in a subtree the user excluded must claim nothing,
            # and one in a subtree `.cgrignore` rescued must be read, or
            # `super::` in the target counts from the wrong module (issue
            # #1088). Cargo's src/bin/ carve-out rides along (issue #1085).
            dir_prefix = f"{'/'.join(here)}/" if here else ""
            dirnames[:] = sorted(
                name
                for name in dirnames
                if should_keep_dir(
                    name,
                    dir_prefix,
                    self._host.exclude_paths,
                    self._host.unignore_paths,
                )
            )
            for path in self._swept_rust_files(dirpath, dir_prefix, here, filenames):
                for key, declarer in self._rust_redirects_in(path, list(here)):
                    # Several files may name one target (a helper shared by
                    # two binaries), and rustc compiles it into each tree.
                    # The graph keys it once, so the walk order decides.
                    parents.setdefault(key, declarer)
        self._rust_redirect_parents = parents
        return parents

    def _swept_rust_files(
        self,
        dirpath: str,
        dir_prefix: str,
        here: tuple[str, ...],
        filenames: list[str],
    ) -> Iterator[Path]:
        """One directory's Rust sources the indexer would hold, sorted."""
        for filename in sorted(filenames):
            if not filename.endswith(cs.EXT_RS):
                continue
            if should_skip_rel_file(
                f"{dir_prefix}{filename}",
                here,
                exclude_paths=self._host.exclude_paths,
                unignore_paths=self._host.unignore_paths,
            ):
                continue
            yield Path(dirpath, filename)

    def _rust_redirects_in(
        self, path: Path, dir_parts: list[str]
    ) -> list[tuple[str, str]]:
        """Modules one file's `#[path]` attributes move, each with its declarer.

        A target its own physical neighbour also declares is left out: rustc
        compiles such a file into both trees, and the neighbour's spelling is
        the one `super::` inside it counts from.
        """
        try:
            source = path.read_text(encoding=cs.RS_ENCODING_UTF8, errors="ignore")
        except OSError:
            return []
        if rs_scan.PATH_ATTRIBUTE_PATTERN.search(source) is None:
            return []
        # A mod.rs IS its directory's module, so it contributes no segment of
        # its own; every other file contributes its stem.
        declarer = cs.SEPARATOR_DOT.join(
            [self.project_name, *dir_parts]
            if path.stem == cs.INDEX_MOD
            else [self.project_name, *dir_parts, path.stem]
        )
        decls = rs_scan.entry_decls_of(
            rs_scan.top_level_only(rs_scan.strip_comments_and_strings(source))
        )
        moved = []
        for redirect in decls.redirects.values():
            target = rs_utils.path_attribute_qn_parts(dir_parts, redirect)
            if target is None or self._rust_module_is_declared(target):
                continue
            moved.append(
                (cs.SEPARATOR_DOT.join([self.project_name, *target]), declarer)
            )
        return moved

    def _rust_logical_parent(self, module_qn: str) -> str | None:
        """The module that declares this one, when `#[path]` moved its file.

        The attribute moves the FILE, not the module's place in the tree, so
        `super::` written inside the target means the DECLARING module rather
        than a sibling of the file (issue #1083). An ordinary module answers
        None, as does anything below a moved one: an inline `mod` inside the
        target keeps its own place, and the climb reaches the declarer by
        stepping through the file's module first.
        """
        return self._rust_redirect_parent_map().get(module_qn)

    def rust_super_base(self, module_qn: str, depth: int) -> str:
        """Climb `depth` module parents from a module.

        Each step asks who DECLARES the module rather than which directory
        holds its file, and the declaring module may itself have been moved
        by a `#[path]` of its own. Where none was, the two are the same.
        """
        parts = module_qn.split(cs.SEPARATOR_DOT)
        for _ in range(depth):
            if (
                logical := self._rust_logical_parent(cs.SEPARATOR_DOT.join(parts))
            ) is not None:
                parts = logical.split(cs.SEPARATOR_DOT)
            elif len(parts) > 1:
                parts = parts[:-1]
        return cs.SEPARATOR_DOT.join(parts)

    def _rust_walk_mods(
        self, parts: list[str], rest: list[str], dir_backed: bool = False
    ) -> list[str] | None:
        """Walk a path tail from a resolved module prefix, honouring `#[path]`.

        Only the crate ENTRY's redirects are read above, and the tail used to
        attach by name straight past a redirect declared in any other file,
        landing on a qn no module owns or on an undeclared shadow file that
        happens to sit where the declared name points (issue #1065).
        """
        out = list(parts)
        for index, segment in enumerate(rest):
            found = self._rust_module_decls(out, dir_backed)
            if found is None:
                # Past the module tree and into items, whose names back no
                # file and so can declare nothing.
                out.extend(rest[index:])
                break
            decls, base = found
            redirect = decls.redirects.get(segment)
            if redirect is None:
                out, dir_backed = [*out, segment], False
                continue
            target = rs_utils.path_attribute_qn_parts(base, redirect)
            if target is None:
                # A #[path] the qn scheme cannot key names a file outside the
                # indexed tree: no module qn is its referent, so decline
                # rather than attach the name-derived sibling shadow (#1082).
                return None
            # A redirect onto a `mod.rs` names the DIRECTORY, and the qn
            # scheme drops the `mod` segment, so the resulting parts are
            # indistinguishable from a plain file module: without this the
            # next hop reads the sibling `<parts>.rs`, an undeclared shadow
            # rustc never compiles into this path.
            out = target
            dir_backed = redirect.rsplit(cs.SEPARATOR_SLASH, 1)[-1] == cs.MOD_RS
        return out

    def _rust_join_mods(
        self, parts: list[str], rest: list[str], dir_backed: bool = False
    ) -> str:
        walked = self._rust_walk_mods(parts, rest, dir_backed)
        if walked is None:
            return cs.RUST_UNRESOLVABLE_QN
        return cs.SEPARATOR_DOT.join([self.project_name, *walked])

    def _rust_attach(
        self, dir_parts: list[str], stem: str, rest: list[str], definitive: bool
    ) -> str:
        # A crate-root-relative path: the first segment names either a
        # submodule FILE/directory beside the entry point (crate::flags ->
        # src.flags) or an item/inline mod declared IN the entry file
        # (crate::Config -> src.main.Config). The entry's OWN declarations
        # outrank the filesystem probe: a sibling file the entry never
        # declares belongs to the other crate, and an inline `mod sys` in
        # lib.rs owns crate::sys even when a bin-crate src/sys.rs exists.
        directory = self.repo_path.joinpath(*dir_parts)
        entries = self._rust_dir_entries(directory)
        chosen = self._rust_entry_decls(dir_parts).get(stem)
        if (
            rest
            and chosen is not None
            and (declared := self._rust_attach_declared(dir_parts, stem, rest, chosen))
            is not None
        ):
            return declared
        if rest and (
            f"{rest[0]}{cs.EXT_RS}" in entries
            or (rest[0] in entries and (directory / rest[0]).is_dir())
        ):
            return self._rust_join_mods([*dir_parts, rest[0]], rest[1:])
        if rest and not definitive and chosen is not None:
            # When a file compiles into BOTH crates (lib.rs and main.rs each
            # declare its module), the path can only mean the entry that
            # DECLARES the item; the chosen entry declaring it returned above.
            stem = next(
                (
                    other
                    for other, other_decls in self._rust_entry_decls(dir_parts).items()
                    if other != stem and rest[0] in other_decls.items
                ),
                stem,
            )
        return cs.SEPARATOR_DOT.join([self.project_name, *dir_parts, stem, *rest])

    def _rust_attach_declared(
        self,
        dir_parts: list[str],
        stem: str,
        rest: list[str],
        chosen: rs_scan.RustEntryDecls,
    ) -> str | None:
        """Attach through the chosen entry's own declarations, if any match."""
        if redirect := chosen.redirects.get(rest[0]):
            # The declaration names its backing file outright, and the
            # module keys under that file, so the declared name never
            # appears in the qn at all (issue #1035). A target the qn
            # scheme cannot key names a file outside the indexed tree:
            # decline rather than fall through to the name-derived
            # sibling shadow below (issue #1082).
            target = rs_utils.path_attribute_qn_parts(dir_parts, redirect)
            if target is None:
                return cs.RUST_UNRESOLVABLE_QN
            return self._rust_join_mods(
                target,
                rest[1:],
                redirect.rsplit(cs.SEPARATOR_SLASH, 1)[-1] == cs.MOD_RS,
            )
        if rest[0] in chosen.mods:
            return self._rust_join_mods([*dir_parts, rest[0]], rest[1:])
        if rest[0] in chosen.items:
            return cs.SEPARATOR_DOT.join([self.project_name, *dir_parts, stem, *rest])
        return None

    def rust_resolve_relative(
        self, base_qn: str, rest: list[str], importer_qn: str
    ) -> str:
        """Attach path segments to a super::/self:: base module.

        The base may be an ordinary file module (children append: src/foo.rs
        -> src.foo.bar), a mod.rs directory (same), the crate root DIRECTORY
        (a super:: chain popped every file segment), or the entry module
        itself (self:: in lib.rs) -- for the last two, children are FILES
        beside the entry point, so route through _rust_attach.
        """
        parts = base_qn.split(cs.SEPARATOR_DOT)[1:]
        if self._rust_is_mod_rs_target(parts):
            # The base qn IS a mod.rs-backed target (src/bin/mod.rs): its
            # self:: — and super:: chains landing on it — attach beside
            # mod.rs, never through a sibling entry stem like src/bin's
            # main.rs (issue #1031).
            return self._rust_join_mods(parts, rest, dir_backed=True)
        if self._rust_is_crate_root_dir(parts):
            stem, definitive = self._rust_entry_stem(parts, importer_qn)
            return self._rust_attach(parts, stem, rest, definitive)
        if (
            parts
            and self._rust_importer_within_root_file(parts, base_qn, importer_qn)
            and parts[-1] not in cs.RS_ENTRY_STEMS
            and f"{parts[-1]}{cs.EXT_RS}"
            in self._rust_dir_entries(self.repo_path.joinpath(*parts[:-1]))
            and self._rust_is_explicit_target(parts[:-1], parts[-1])
        ):
            # In a crate root module `self::` IS `crate::`: an explicit
            # target attaches beside itself, exactly as its crate:: paths
            # do, but only when the asker lives IN the root file (the root
            # itself or an inline mod written inside it, which has no
            # backing file). A file-backed submodule walking super:: up
            # onto this qn means the MODULE of the same name, whose
            # children live in its directory (both cargo-verified).
            # Auto-targets keep their entry-qn nesting.
            return self._rust_attach(parts[:-1], parts[-1], rest, definitive=True)
        if (
            parts
            and parts[-1] in ("lib", "main")
            and f"{parts[-1]}{cs.EXT_RS}"
            in self._rust_dir_entries(self.repo_path.joinpath(*parts[:-1]))
            and self._rust_is_crate_root_dir(parts[:-1])
        ):
            # The base IS a crate entry module (self:: in lib.rs); a module
            # merely NAMED main/lib deeper in the tree attaches normally.
            return self._rust_attach(parts[:-1], parts[-1], rest, definitive=True)
        if not rest:
            return base_qn
        walked = self._rust_walk_mods(parts, rest)
        if walked is None:
            return cs.RUST_UNRESOLVABLE_QN
        if walked == [*parts, *rest]:
            # No redirect fired, so keep base_qn verbatim rather than
            # rebuilding it from project_name, which re-splits differently
            # when the repository directory name itself contains a dot.
            return cs.SEPARATOR_DOT.join([base_qn, *rest])
        return cs.SEPARATOR_DOT.join([self.project_name, *walked])

    def rewrite_rust_local_use_path(
        self,
        full_path: str,
        module_qn: str,
        local_mods: frozenset[str] = frozenset(),
    ) -> str:
        """Rewrite a crate::/super::/self:: use path to a project qn.

        Stored raw, these paths resolve nowhere: class resolution hands them
        to the deferred-inherit pass, which externalises them into phantom
        ExternalModule nodes (crate.flags.Flag) and the override pass never
        links impl methods to their trait (issue #1007). module_qn must be
        the EFFECTIVE module of the use declaration, including inline `mod`
        blocks. A head naming a workspace member crate rewrites the same
        way, unless a `mod` in the declaring scope (local_mods) claims it:
        rustc binds the local module first (issue #1033). Such a head is the
        2018-edition spelling of `self::` (`pub mod get; pub use get::Get;`),
        so it resolves the same way; kept raw it read as an external crate
        named `get`, and every re-export written like that dead-ended
        outside the project (issue #2542). External paths (std::fmt) pass
        through unchanged.
        """
        parts = full_path.split(cs.SEPARATOR_DOUBLE_COLON)
        head = parts[0]
        if head == cs.RUST_CRATE_KEYWORD:
            return self._rust_rewrite_crate_path(parts[1:], module_qn)
        if head == cs.KEYWORD_SELF:
            return self.rust_resolve_relative(module_qn, parts[1:], module_qn)
        if head == cs.KEYWORD_SUPER:
            depth = 0
            while depth < len(parts) and parts[depth] == cs.KEYWORD_SUPER:
                depth += 1
            base = self.rust_super_base(module_qn, depth)
            return self.rust_resolve_relative(base, parts[depth:], module_qn)
        if head in local_mods:
            return self.rust_resolve_relative(module_qn, parts, module_qn)
        if (
            root := self._rust_workspace_crate_roots().get(head)
        ) is not None and self._rust_member_rewrite_allowed(root[0], module_qn):
            _pkg, dir_parts, stem = root
            return self._rust_attach(list(dir_parts), stem, parts[1:], definitive=True)
        return full_path

    def _rust_rewrite_crate_path(self, rest: list[str], module_qn: str) -> str:
        root = self._rust_crate_root(module_qn)
        if root is None:
            return (
                cs.SEPARATOR_DOT.join([self.project_name, *rest])
                if rest
                else self.project_name
            )
        kind, root_parts = root
        if kind == "file":
            # An auto-target crate (src/bin/tool.rs): items and
            # submodules both nest under the entry file's qn.
            return self._rust_join_mods(root_parts, rest)
        if kind == "dir_file":
            # A mod.rs-backed target (src/bin/mod.rs): same nesting, but
            # the root's declarations live in mod.rs, so the walk must not
            # read a sibling `<dir>.rs` shadow.
            return self._rust_join_mods(root_parts, rest, dir_backed=True)
        if kind == "entry":
            # An explicit manifest target: the root file IS the entry,
            # so submodule files sit beside it and items nest in it.
            return self._rust_attach(
                root_parts[:-1], root_parts[-1], rest, definitive=True
            )
        stem, definitive = self._rust_entry_stem(root_parts, module_qn)
        return self._rust_attach(root_parts, stem, rest, definitive)
