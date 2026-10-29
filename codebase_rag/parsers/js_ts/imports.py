"""JS/TS's import pass: ES `import` and re-export statements and CommonJS
`require` bindings into the shared import map, each module's export table,
and the tsconfig `paths`/`baseUrl` and workspace-package layout that resolve
a specifier to a project module.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a JS/TS
file's imports to the `JsTsImporter` it builds."""

import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as ls
from ...node_text import safe_decode_text, safe_decode_with_fallback
from ...types_defs import FunctionRegistryTrieProtocol, PropertyDict
from ...utils.path_utils import cached_relative_path
from .commonjs_exports import commonjs_export_bindings, is_commonjs_module
from .module_paths import discover_js_workspace_packages, resolve_js_workspace_import
from .reexports import JsExport, follow_js_reexports
from .tsconfig import (
    load_ts_base_urls,
    load_ts_path_aliases,
    ts_alias_candidates,
    ts_alias_target,
)

_JS_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9.+-]*):")


def _js_destructured_names(pattern: Node) -> list[tuple[str, str]]:
    # (local, imported) name pairs bound by an object destructuring pattern:
    # `{ writeFileSync }` -> (writeFileSync, writeFileSync); `{ x: y }` -> (y, x).
    out: list[tuple[str, str]] = []
    for child in pattern.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            if name := safe_decode_text(child):
                out.append((name, name))
        elif child.type == cs.TS_PAIR_PATTERN and (pair := _js_pair_binding(child)):
            out.append(pair)
    return out


def _js_pair_binding(pair: Node) -> tuple[str, str] | None:
    # `{ x: y }` binds local `y` to imported `x`; any other value shape
    # (a nested pattern, a default) binds nothing this reader tracks.
    key = pair.child_by_field_name(cs.FIELD_KEY)
    value = pair.child_by_field_name(cs.FIELD_VALUE)
    if key is None or value is None or value.type != cs.TS_IDENTIFIER:
        return None
    imported = safe_decode_text(key)
    local = safe_decode_text(value)
    if imported and local:
        return local, imported
    return None


def _js_default_export_local(statement: Node) -> str | None:
    # The local name `export default` publishes: a named function or class
    # declaration, or a bare identifier. An anonymous function or any other
    # expression is known by no name a binding could hold.
    declaration = statement.child_by_field_name(cs.FIELD_DECLARATION)
    if declaration is not None:
        return safe_decode_text(declaration.child_by_field_name(cs.FIELD_NAME))
    value = statement.child_by_field_name(cs.FIELD_VALUE)
    if value is not None and value.type == cs.TS_IDENTIFIER:
        return safe_decode_text(value)
    return None


def _js_export_specifiers(statement: Node) -> Iterator[tuple[str, str]]:
    # The (local, exported) names of each `export { local as exported }`
    # specifier; a comment inside the braces is no specifier.
    for clause in statement.named_children:
        if clause.type != cs.TS_EXPORT_CLAUSE:
            continue
        for specifier in clause.named_children:
            local = safe_decode_text(specifier.child_by_field_name(cs.FIELD_NAME))
            name = (
                safe_decode_text(specifier.child_by_field_name(cs.FIELD_ALIAS)) or local
            )
            if local and name:
                yield local, name


def _has_aliased_scheme(specifier: str) -> bool:
    # True for a JS/TS specifier with a non-standard scheme (`ext:deno_node/x`),
    # which names first-party code under a non-file-path alias. Standard external
    # schemes (node:/npm:/jsr:/http(s):) and bare/scoped package names (`lodash`,
    # `@scope/pkg`) are NOT aliased, so they stay externally suppressed. A tsconfig
    # `paths` alias (`@/util`) has no scheme and is not exempted here (it would be
    # indistinguishable from a scoped package `@scope/pkg`); it is instead resolved
    # PRECISELY to its real module upstream by resolve_js_module_path via
    # load_ts_path_aliases, so no trie fallback is needed for it.
    match = _JS_SCHEME_RE.match(specifier)
    return bool(match) and match.group(1).lower() not in cs.JS_EXTERNAL_IMPORT_SCHEMES


class JsImportTarget(NamedTuple):
    """The module qn a JS/TS specifier resolves to, and whether it is a package."""

    module_qn: str
    # A bare or scoped specifier no tsconfig alias or workspace package maps
    # to a project file. Its qn is the specifier itself, which can spell the
    # project's own name (`react` in a repo named react) or a local module's
    # path (`react` beside a repo-root `react.tsx`) without naming either, so
    # only this flag, recorded while resolution still knows, marks it external.
    is_package: bool


def _js_binds_whole_module(site: PropertyDict | None) -> bool:
    # A JS/TS binding that names one export records it as the site's imported
    # name; a namespace import and `export *` record the wildcard, and a bare
    # `require` or the CommonJS fallback (no site) record none.
    if site is None:
        return True
    return site.get(cs.KEY_IMPORTED_NAME) in (None, cs.IMPORTED_NAME_WILDCARD)


def _is_known_js_module(qn: str, known_module_paths: Mapping[str, str]) -> bool:
    # A registered module whose FILE is JS/TS. Another language can own the
    # same qn (`pkg/__init__.py` is `proj.pkg`), and a JS/TS import must never
    # bind to it.
    path = known_module_paths.get(qn)
    return path is not None and path.endswith(cs.JS_TS_MODULE_EXTENSIONS)


class JsTsImportHost(Protocol):
    """What the JS/TS importer calls on the import processor that builds it:
    the import-site record every language's bindings go through, whose sites
    ride on the IMPORTS edges the processor's flush emits."""

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class JsTsImporter:
    __slots__ = (
        "_host",
        "repo_path",
        "project_name",
        "import_mapping",
        "module_qn_to_file_path",
        "unresolved_specifiers",
        "js_export_bindings",
        "js_ts_bare_imports",
        "js_ts_package_imports",
        "js_path_aliases",
        "js_base_urls",
        "_map_js_workspace_import",
    )

    def __init__(
        self,
        host: JsTsImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
        module_qn_to_file_path: dict[str, Path],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: an import binds
        # its names in the map every later pass reads.
        self.import_mapping = import_mapping
        # The factory's shared module -> file map, filled before a file's
        # imports are parsed. A JS/TS relative specifier resolves against the
        # importer's DIRECTORY, which only the file path spells reliably: a
        # dotted stem (`url.test.ts`) keeps its dots in the qn (issue #2565).
        self.module_qn_to_file_path = module_qn_to_file_path
        # Per module qn: the LITERAL relative specifiers ("./index") that named
        # no file on disk when this module was parsed. A relative JS/TS import
        # of a path that does not exist yet has its IMPORTS edge dropped at
        # flush as an unverifiable internal target, so the target-side lookup
        # that finds waiting importers has no row to match and the waiter is
        # never re-parsed when the file is finally created (issue #1714).
        # Recorded here so the importer stays findable from the path alone.
        self.unresolved_specifiers: dict[str, set[str]] = {}
        # JS/TS module qn -> {exported name: what it names}, from every
        # `export { ... }` (with or without a `from`) and `export default`,
        # and from what a CommonJS module publishes (`module.exports = {...}`).
        # An importer's map holds `<module>.strip`, which names nothing
        # registered; the call resolver follows it through this table, and
        # only through it, since a module's own imports are not its exports
        # (issue #2464).
        self.js_export_bindings: dict[str, dict[str, JsExport]] = {}
        # Local names brought in by a JS/TS import with a NON-STANDARD scheme
        # (`ext:deno_node/y`; see _has_aliased_scheme), keyed by module. Such a
        # specifier aliases first-party code but does not resolve to a file-path
        # module qn, so the target is unregistered and would be judged external,
        # dropping the call. These names defer to the simple-name trie instead of
        # being suppressed. Ordinary package specifiers (bare, scoped, node:/npm:)
        # are excluded, so genuine external calls stay suppressed.
        self.js_ts_bare_imports: dict[str, set[str]] = {}
        # Local names a JS/TS import or `require` binds to a package (see
        # JsImportTarget.is_package), keyed by module: what they name lives in
        # node_modules, so a member of one never binds a first-party symbol.
        self.js_ts_package_imports: dict[str, set[str]] = {}
        # tsconfig `paths` aliases (match_prefix, target_prefix, is_wildcard), parsed
        # once from the repo-root tsconfig so `@/util` imports resolve to the real
        # first-party module instead of being dropped as external.
        self.js_path_aliases: list[tuple[str, str, bool]] = load_ts_path_aliases(
            repo_path
        )
        self.js_base_urls: tuple[tuple[str, str | None], ...] = load_ts_base_urls(
            repo_path
        )

        # A JS/TS monorepo imports its own packages by manifest NAME
        # (`@acme/sdk/admin`), which no relative-path arithmetic resolves, so
        # map every first-party package.json name to its directory once, the
        # same way the import processor maps go.mod module directives
        # (issue #945).
        js_workspace_packages = discover_js_workspace_packages(repo_path)

        @lru_cache(maxsize=4096)
        def _map_js_workspace_import_cached(
            import_path: str, require: bool = False
        ) -> str | None:
            return resolve_js_workspace_import(
                js_workspace_packages, import_path, repo_path, require
            )

        self._map_js_workspace_import = _map_js_workspace_import_cached

    def parse_imports(
        self, captures: dict[str, list[Node]], module_qn: str, root_node: Node
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_STATEMENT:
                self._parse_js_import_statement(import_node, module_qn)

            elif import_node.type in (
                cs.TS_LEXICAL_DECLARATION,
                cs.TS_VARIABLE_DECLARATION,
            ):
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == cs.TS_EXPORT_STATEMENT:
                self._parse_js_reexport(import_node, module_qn)
                self._record_js_export_bindings(import_node, module_qn)
        self._record_commonjs_export_bindings(root_node, module_qn)

    def _record_commonjs_export_bindings(self, root_node: Node, module_qn: str) -> None:
        """Record what a CommonJS module publishes as its export table.

        `module.exports = { Svc }` is such a module's whole API, as its export
        statements are an ES module's, so the re-export walk follows only the
        names it publishes, never a binding it keeps to itself (CodeRabbit,
        PR #3355). A module with a value ES export keeps that table alone
        (`is_commonjs_module`).
        """
        if not is_commonjs_module(root_node):
            return
        # A name only a top-level `require` or `import` binds leads straight
        # to what it imports, so a nested declaration of the same name, which
        # registers under the module's qn too, cannot stand in for it. The
        # reader has already dropped a name anything else imports or declares.
        imports = self.import_mapping.get(module_qn, {})
        published: dict[str, JsExport] = {}
        for name, found in commonjs_export_bindings(root_node).items():
            if not found.imported:
                published[name] = JsExport(
                    f"{module_qn}{cs.SEPARATOR_DOT}{found.binding}", local=True
                )
            elif (target := imports.get(found.binding)) is not None:
                published[name] = JsExport(target, local=False)
        if published:
            self.js_export_bindings.setdefault(module_qn, {}).update(published)

    def _ts_alias_module_qn(self, import_path: str) -> str | None:
        # Resolve a tsconfig `paths` alias (`@/util` -> `src/util`) to the
        # first-party module qn, so the call binds to the real file instead of
        # being dropped as external. Precise (maps to the actual path), so no
        # trie-fallback collision risk. Longest matching prefix wins.
        # Collect every matching alias (a monorepo may define `@/` in several
        # tsconfigs pointing at different package dirs), then accept the first,
        # longest-prefix one whose target is a real first-party file on disk. The
        # disk check both disambiguates siblings and blocks a catch-all alias
        # (`"*": ["src/*"]`) from capturing bare package imports (`lodash` ->
        # `proj.src.lodash`) and rebinding them to same-named locals (#580).
        candidates = sorted(
            ts_alias_candidates(import_path, self.js_path_aliases),
            key=lambda c: c[0],
            reverse=True,
        )
        for _prefix_len, raw_path in candidates:
            normalized = ts_alias_target(raw_path)
            if normalized is None:
                continue
            module_rel = self._js_module_rel_on_disk(normalized)
            if module_rel is None:
                continue
            dotted = module_rel.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
            return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        return None

    @staticmethod
    def _strip_js_extension(import_path: str) -> str:
        # An ESM RELATIVE specifier may carry an explicit extension
        # (`./b.js`); the module qn never does, so keeping it poisons the
        # import map AND the IMPORTS edge with a phantom `.js` segment
        # (issue #652). Bare package specifiers are exempt: a package can
        # legitimately be NAMED with the extension (p5.js, highlight.js).
        for ext in cs.JS_TS_ALL_EXTENSIONS:
            if import_path.endswith(ext) and len(import_path) > len(ext):
                return import_path[: -len(ext)]
        return import_path

    def _js_base_url_names_file(self, import_path: str, current_module: str) -> bool:
        # A bare specifier the importing file's own `baseUrl` resolves to a
        # project file is first-party even though no `paths` alias maps it.
        # Its qn stays the bare path it always had; only the package flag
        # needs the disk's answer, so resolution beyond this flag is unchanged.
        base = self._js_governing_base_url(current_module)
        return (
            base is not None
            and self._js_module_rel_on_disk(f"{base}{import_path}") is not None
        )

    def _js_governing_base_url(self, current_module: str) -> str | None:
        # The `baseUrl` of the nearest tsconfig whose directory holds the
        # module; module qns spell directories as dotted segments, as the
        # relative-specifier resolver below reads them.
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        relative = current_module.removeprefix(project_prefix)
        return next(
            (
                base
                for directory, base in self.js_base_urls
                if relative.startswith(directory)
            ),
            None,
        )

    def _js_module_rel_on_disk(self, normalized: str) -> str | None:
        """The repo-relative module this path names on disk, or None.

        A JS/TS specifier names either the file itself (with any of the module
        extensions) or a directory holding an `index` entry point. Shared by
        the tsconfig-alias resolver and the unresolved-specifier probe so the
        two cannot disagree about what "exists" means.
        """
        if any(
            (self.repo_path / f"{normalized}{ext}").is_file()
            for ext in cs.JS_TS_MODULE_EXTENSIONS
        ):
            return normalized
        directory = self.repo_path / normalized
        if directory.is_dir() and any(
            (directory / f"{cs.JS_INDEX_STEM}{ext}").is_file()
            for ext in cs.JS_TS_MODULE_EXTENSIONS
        ):
            return f"{normalized}{cs.SEPARATOR_SLASH}{cs.JS_INDEX_STEM}"
        return None

    def _note_unresolved_js_specifier(self, module_qn: str, specifier: str) -> None:
        """Record a RELATIVE specifier that names nothing on disk (issue #1714).

        Only relative ones: a bare specifier (`lodash`) is an external package
        and becomes a real IMPORTS edge to an ExternalModule, which the
        target-side lookup can already find. A relative path that resolves
        nowhere is the case with no persisted row at all.

        The literal text is stored, not the derived qn: the qn is what the
        dropped edge would have used, while the specifier is what the source
        wrote and what a later-created file must be matched against.
        """
        if not specifier.startswith(cs.PATH_CURRENT_DIR):
            return
        parts = self._js_relative_target_parts(specifier, module_qn)
        if len(parts) < 2 or parts[0] != self.project_name:
            # Escapes the project (`../../outside`), so no file this repo can
            # create would ever satisfy it.
            return
        # Joined from the path segments, not re-split from the dotted qn:
        # `users.service` is one file name, not a `users/` directory.
        rel = cs.SEPARATOR_SLASH.join(parts[1:])
        if self._js_module_rel_on_disk(rel) is not None:
            return
        self.unresolved_specifiers.setdefault(module_qn, set()).add(specifier)

    def _js_module_directory_parts(self, module_qn: str) -> list[str]:
        """The project name plus the importer's directory, one part per segment.

        Read from the module's FILE when it is known. A dotted stem keeps its
        dots in the qn (`url.test.ts` is `proj.src.url.test`, as a dotted C#
        stem is under `module_directory_qn`), so dropping the qn's last
        segment lands inside the file name (`proj.src.url`) and every
        relative specifier resolved from there misses (issue #2565). The path
        also keeps a dotted DIRECTORY (`v1.2/`) as the single segment the
        on-disk probe needs.
        """
        file_path = self.module_qn_to_file_path.get(module_qn)
        if file_path is not None:
            try:
                rel_dir = cached_relative_path(file_path, self.repo_path).parent
            except ValueError:
                rel_dir = None
            if rel_dir is not None:
                return [self.project_name, *rel_dir.parts]
        return module_qn.split(cs.SEPARATOR_DOT)[:-1]

    def _js_relative_target_parts(self, import_path: str, module_qn: str) -> list[str]:
        # A relative specifier applied to the importer's directory; the first
        # part is the project name unless `..` climbed out of the project.
        current_parts = self._js_module_directory_parts(module_qn)
        for part in self._strip_js_extension(import_path).split(cs.SEPARATOR_SLASH):
            if part == cs.PATH_CURRENT_DIR:
                continue
            if part == cs.PATH_PARENT_DIR:
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)
        return current_parts

    def resolve_js_module_path(
        self, import_path: str, current_module: str, require: bool = False
    ) -> str:
        return self._resolve_js_import_target(
            import_path, current_module, require
        ).module_qn

    def _resolve_js_import_target(
        self, import_path: str, current_module: str, require: bool = False
    ) -> JsImportTarget:
        if not import_path.startswith(cs.PATH_CURRENT_DIR):
            if aliased := self._ts_alias_module_qn(import_path):
                return JsImportTarget(aliased, False)
            if workspace := self._map_js_workspace_import(import_path, require):
                dotted = workspace.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
                return JsImportTarget(
                    f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}", False
                )
            return JsImportTarget(
                import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT),
                not _has_aliased_scheme(import_path)
                and not self._js_base_url_names_file(import_path, current_module),
            )
        return JsImportTarget(
            cs.SEPARATOR_DOT.join(
                self._js_relative_target_parts(import_path, current_module)
            ),
            False,
        )

    def _parse_js_import_clause(
        self,
        clause_node: Node,
        source_module: str,
        current_module: str,
        is_aliased_scheme: bool = False,
        is_package: bool = False,
    ) -> None:
        # The clause's parent is the import statement whose span the edge
        # records; the clause alone would drop the `from '...'` half.
        statement = clause_node.parent or clause_node
        for child in clause_node.children:
            if child.type == cs.TS_IDENTIFIER:
                imported_name = safe_decode_with_fallback(child)
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}{cs.IMPORT_DEFAULT_SUFFIX}"
                )
                self._host._record_import_site(
                    current_module, imported_name, statement, cs.TS_EXPORT_DEFAULT
                )
                self._note_js_bare_import(
                    current_module, imported_name, is_aliased_scheme
                )
                self._note_js_package_import(current_module, imported_name, is_package)
                logger.debug(
                    ls.IMP_JS_DEFAULT, name=imported_name, module=source_module
                )

            elif child.type == cs.TS_NAMED_IMPORTS:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPECIFIER:
                        self._record_js_named_import(
                            grandchild,
                            source_module,
                            current_module,
                            statement,
                            is_aliased_scheme,
                            is_package,
                        )

            elif child.type == cs.TS_NAMESPACE_IMPORT:
                self._record_js_namespace_import(
                    child, source_module, current_module, statement, is_package
                )

    def _note_js_bare_import(
        self, current_module: str, local_name: str, is_aliased_scheme: bool
    ) -> None:
        if is_aliased_scheme:
            self.js_ts_bare_imports.setdefault(current_module, set()).add(local_name)

    def _note_js_package_import(
        self, current_module: str, local_name: str, is_package: bool
    ) -> None:
        # The last binding of a name wins, as it does in import_mapping: a
        # second `var UI = require("./ui")` makes `UI` the project module's.
        if is_package:
            self.js_ts_package_imports.setdefault(current_module, set()).add(local_name)
        elif names := self.js_ts_package_imports.get(current_module):
            names.discard(local_name)

    def _record_js_named_import(
        self,
        specifier: Node,
        source_module: str,
        current_module: str,
        statement: Node,
        is_aliased_scheme: bool,
        is_package: bool,
    ) -> None:
        name_node = specifier.child_by_field_name(cs.FIELD_NAME)
        if not name_node:
            return
        alias_node = specifier.child_by_field_name(cs.FIELD_ALIAS)
        imported_name = safe_decode_with_fallback(name_node)
        local_name = (
            safe_decode_with_fallback(alias_node) if alias_node else imported_name
        )
        self.import_mapping[current_module][local_name] = (
            f"{source_module}{cs.SEPARATOR_DOT}{imported_name}"
        )
        self._host._record_import_site(
            current_module, local_name, statement, imported_name
        )
        self._note_js_bare_import(current_module, local_name, is_aliased_scheme)
        self._note_js_package_import(current_module, local_name, is_package)
        logger.debug(
            ls.IMP_JS_NAMED,
            local=local_name,
            module=source_module,
            name=imported_name,
        )

    def _record_js_namespace_import(
        self,
        namespace_node: Node,
        source_module: str,
        current_module: str,
        statement: Node,
        is_package: bool,
    ) -> None:
        identifier = next(
            (c for c in namespace_node.children if c.type == cs.TS_IDENTIFIER), None
        )
        if identifier is None:
            return
        namespace_name = safe_decode_with_fallback(identifier)
        self.import_mapping[current_module][namespace_name] = source_module
        self._host._record_import_site(
            current_module,
            namespace_name,
            statement,
            cs.IMPORTED_NAME_WILDCARD,
        )
        self._note_js_package_import(current_module, namespace_name, is_package)
        logger.debug(
            ls.IMP_JS_NAMESPACE,
            name=namespace_name,
            module=source_module,
        )

    def _parse_js_require(self, decl_node: Node, current_module: str) -> None:
        for declarator in decl_node.children:
            if declarator.type != cs.TS_VARIABLE_DECLARATOR:
                continue
            name_node = declarator.child_by_field_name(cs.FIELD_NAME)
            value_node = declarator.child_by_field_name(cs.FIELD_VALUE)
            if (
                name_node is None
                or value_node is None
                or value_node.type != cs.TS_CALL_EXPRESSION
            ):
                continue
            func_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
            args_node = value_node.child_by_field_name(cs.FIELD_ARGUMENTS)
            if (
                func_node is None
                or args_node is None
                or func_node.type != cs.TS_IDENTIFIER
                or safe_decode_text(func_node) != cs.IMPORT_REQUIRE
            ):
                continue
            arg = next((a for a in args_node.children if a.type == cs.TS_STRING), None)
            if arg is not None:
                self._bind_js_require(name_node, arg, decl_node, current_module)

    def _bind_js_require(
        self, name_node: Node, arg: Node, decl_node: Node, current_module: str
    ) -> None:
        # A CommonJS `require()` reads a dual-package exports map from the
        # require side.
        require_text = safe_decode_with_fallback(arg).strip("'\"")
        target = self._resolve_js_import_target(require_text, current_module, True)
        resolved_module = target.module_qn
        self._note_unresolved_js_specifier(current_module, require_text)
        if name_node.type == cs.TS_IDENTIFIER:
            # `const fs = require('fs')`: bind the whole module.
            var_name = safe_decode_with_fallback(name_node)
            self.import_mapping[current_module][var_name] = resolved_module
            self._host._record_import_site(current_module, var_name, decl_node)
            self._note_js_package_import(current_module, var_name, target.is_package)
            logger.debug(ls.IMP_JS_REQUIRE, var=var_name, module=resolved_module)
        elif name_node.type == cs.TS_OBJECT_PATTERN:
            # `const { writeFileSync } = require('fs')` / `{ x: y }`: bind each
            # local to module.imported, mirroring how ESM named imports resolve.
            for local, imported in _js_destructured_names(name_node):
                full = f"{resolved_module}{cs.SEPARATOR_DOT}{imported}"
                self.import_mapping[current_module][local] = full
                self._host._record_import_site(
                    current_module, local, decl_node, imported
                )
                self._note_js_package_import(current_module, local, target.is_package)
                logger.debug(ls.IMP_JS_REQUIRE, var=local, module=full)

    def _js_statement_source(
        self, statement: Node, module_qn: str
    ) -> tuple[str, JsImportTarget] | None:
        # The statement's source string (`from './x'`) and what it resolves
        # to; None when the statement names no source at all.
        for child in statement.children:
            if child.type == cs.TS_STRING:
                source_text = safe_decode_with_fallback(child).strip("'\"")
                target = self._resolve_js_import_target(source_text, module_qn)
                self._note_unresolved_js_specifier(module_qn, source_text)
                return source_text, target
        return None

    def _parse_js_import_statement(self, import_node: Node, module_qn: str) -> None:
        source = self._js_statement_source(import_node, module_qn)
        if source is None:
            return
        source_text, target = source
        if not target.module_qn:
            return
        is_aliased_scheme = _has_aliased_scheme(source_text)
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_CLAUSE:
                self._parse_js_import_clause(
                    child,
                    target.module_qn,
                    module_qn,
                    is_aliased_scheme,
                    target.is_package,
                )

    def _parse_js_reexport(self, export_node: Node, current_module: str) -> None:
        source = self._js_statement_source(export_node, current_module)
        source_module = source[1].module_qn if source is not None else None
        if not source_module:
            return

        for child in export_node.children:
            if child.type == cs.TS_JS_STAR:
                wildcard_key = f"*{source_module}"
                self.import_mapping[current_module][wildcard_key] = source_module
                self._host._record_import_site(
                    current_module, wildcard_key, export_node, cs.IMPORTED_NAME_WILDCARD
                )
                logger.debug(ls.IMP_JS_NAMESPACE_REEXPORT, module=source_module)
            elif child.type == cs.TS_EXPORT_CLAUSE:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_EXPORT_SPECIFIER:
                        self._record_js_reexport_specifier(
                            grandchild, export_node, current_module, source_module
                        )

    def _record_js_reexport_specifier(
        self,
        specifier: Node,
        export_node: Node,
        current_module: str,
        source_module: str,
    ) -> None:
        name_node = specifier.child_by_field_name(cs.FIELD_NAME)
        if not name_node:
            return
        alias_node = specifier.child_by_field_name(cs.FIELD_ALIAS)
        original_name = safe_decode_with_fallback(name_node)
        exported_name = (
            safe_decode_with_fallback(alias_node) if alias_node else original_name
        )
        self.import_mapping[current_module][exported_name] = (
            f"{source_module}{cs.SEPARATOR_DOT}{original_name}"
        )
        self._host._record_import_site(
            current_module,
            exported_name,
            export_node,
            original_name,
        )
        logger.debug(
            ls.IMP_JS_REEXPORT,
            exported=exported_name,
            module=source_module,
            original=original_name,
        )

    def follow_js_reexports(
        self,
        qn: str,
        module_paths: Mapping[str, Path],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> str:
        """Where a JS/TS import of `qn` leads through the barrels re-exporting
        it; see `js_ts.reexports.follow_js_reexports`."""
        return follow_js_reexports(
            qn,
            self.import_mapping,
            self.js_export_bindings,
            module_paths,
            function_registry,
        )

    def follow_js_default_import(
        self,
        qn: str,
        module_paths: Mapping[str, Path],
        function_registry: FunctionRegistryTrieProtocol,
    ) -> str:
        """The definition a default import names, or `qn` unchanged.

        `import Store from "./Store"` maps `Store` to `<module>.default`,
        where only an anonymous `export default class {}` is registered; the
        module's export table says which named class or function is its
        default. Read as `<module>.default`, a default-imported class typed
        nothing and was visible to no fallback (issue #3179).
        """
        if not qn.endswith(cs.IMPORT_DEFAULT_SUFFIX) or qn in function_registry:
            return qn
        followed = self.follow_js_reexports(qn, module_paths, function_registry)
        return followed if followed in function_registry else qn

    def _record_js_export_bindings(self, statement: Node, module_qn: str) -> None:
        """Record what each name an `export` statement publishes stands for.

        `export { trim as strip }` and `export default upper` publish a local
        binding under a name no definition is registered under, and
        `export { add as plus } from "./add"` another module's export
        (issue #2464). Recording only exports keeps a barrel's private import
        of `foo` from standing in for the `foo` it exports through
        `export *`.
        """
        reexport = statement.child_by_field_name(cs.FIELD_SOURCE) is not None
        exported: dict[str, JsExport] = {}
        if not reexport and any(
            child.type == cs.TS_EXPORT_DEFAULT for child in statement.children
        ):
            if local := _js_default_export_local(statement):
                exported[cs.TS_EXPORT_DEFAULT] = JsExport(
                    f"{module_qn}{cs.SEPARATOR_DOT}{local}", local=True
                )
        # `_parse_js_reexport` has just mapped this statement's re-exports.
        mapped = self.import_mapping.get(module_qn, {})
        for local, name in _js_export_specifiers(statement):
            if not reexport:
                exported[name] = JsExport(
                    f"{module_qn}{cs.SEPARATOR_DOT}{local}", local=True
                )
            elif (target := mapped.get(name)) is not None:
                exported[name] = JsExport(target, local=False)
        if exported:
            self.js_export_bindings.setdefault(module_qn, {}).update(exported)

    def resolve_js_internal_module(
        self,
        full_name: str,
        known_module_paths: Mapping[str, str] | None = None,
        site: PropertyDict | None = None,
    ) -> str:
        if full_name.endswith(cs.IMPORT_DEFAULT_SUFFIX):
            return full_name[: -len(cs.IMPORT_DEFAULT_SUFFIX)]

        # A namespace import, `export *` or bare `require` binds the module
        # itself, so a real module under the full name is the target. Probing
        # a shorter prefix first would send `* as c from './app.config'` to
        # `app.ts`, since the dotted stem spells `app` plus one more segment.
        known = known_module_paths or {}
        if _is_known_js_module(full_name, known) and _js_binds_whole_module(site):
            return full_name

        parts = full_name.split(cs.SEPARATOR_DOT)
        if len(parts) <= 2:
            return full_name

        potential_module = cs.SEPARATOR_DOT.join(parts[:-1])
        relative_path = cs.SEPARATOR_SLASH.join(parts[1:-1])

        # Where the module ends and the imported symbol begins is decided by
        # asking whether the shorter path names a module ON DISK. That question
        # already has an owner, and this site used to answer it again with a
        # restated `(.js, .ts, .jsx, .tsx)` -- a four-element subset of the ten
        # in `JS_TS_MODULE_EXTENSIONS`. For a module written in any of the
        # other six the probe found nothing, the symbol segment was never
        # stripped, and the unstripped `proj.pkg.go` reached verification,
        # matched no Module node and was dropped: the IMPORTS edge vanished
        # silently. `.d.ts` is how it was found (#1720), but `.mjs`, `.cjs`,
        # `.mts` and `.cts` were broken by the same omission and have nothing
        # to do with declarations.
        #
        # Same shape as the `.mts`/`.cts` gap in `DIRECTORY_MODULE_STEM_BY_EXT`
        # from the #1682 review: a canonical set restated at a second site and
        # drifting from it. Delegating is what stops it recurring here.
        if self._js_module_rel_on_disk(relative_path) is not None:
            return potential_module

        # The slash-join above splits a dotted stem or directory
        # (`users.service.ts`, `v1.2/`) into two path segments, so the disk
        # probe misses it. The registry of real module qns spells it the way
        # the indexer did (issue #2565); a directory is found by its `index`
        # entry point, as the disk probe finds `v1/`.
        if _is_known_js_module(potential_module, known) or _is_known_js_module(
            f"{potential_module}{cs.SEPARATOR_DOT}{cs.JS_INDEX_STEM}", known
        ):
            return potential_module

        return full_name
