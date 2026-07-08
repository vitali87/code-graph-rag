import json
import os
import posixpath
import re
from functools import lru_cache
from pathlib import Path

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import (
    DeferredImportEdge,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
)
from .cpp_frontend.qn import build_module_qn_map
from .go import discover_go_module_paths, resolve_go_import_path
from .lua import utils as lua_utils
from .python_source_roots import discover_python_source_roots, resolve_via_source_roots
from .rs import utils as rs_utils
from .stdlib_extractor import (
    StdlibCacheStats,
    StdlibExtractor,
    clear_stdlib_cache,
    flush_stdlib_cache,
    get_stdlib_cache_stats,
    load_persistent_cache,
    save_persistent_cache,
)
from .utils import (
    get_query_cursor,
    safe_decode_text,
    safe_decode_with_fallback,
    sorted_captures,
)

_JS_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9.+-]*):")
_JSONC_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JSONC_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _load_jsonc(path: Path) -> dict | None:
    # (H) tsconfig.json is JSONC (comments, trailing commas). Try strict JSON first
    # (H) (comment-free configs), then fall back to stripping comments/trailing
    # (H) commas. The naive strip can mangle `//` inside string values, so it is only
    # (H) a fallback; on any failure return None (aliases simply stay unresolved).
    try:
        text = path.read_text(encoding=cs.ENCODING_UTF8)
    except OSError:
        return None
    for candidate in (text, None):
        source = candidate
        if source is None:
            source = _JSONC_BLOCK_COMMENT_RE.sub("", text)
            source = _JSONC_LINE_COMMENT_RE.sub("", source)
            source = _JSONC_TRAILING_COMMA_RE.sub(r"\1", source)
        try:
            parsed = json.loads(source)
        except (json.JSONDecodeError, ValueError):
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _child_dirs(path: Path) -> list[Path]:
    # (H) Immediate subdirectories worth searching, pruning dependency/build/VCS
    # (H) trees at traversal time so we never stat into node_modules (thousands of
    # (H) package tsconfigs) or hidden dirs.
    try:
        return sorted(
            child
            for child in path.iterdir()
            if child.is_dir()
            and child.name not in cs.TS_ALIAS_SKIP_DIRS
            and not child.name.startswith(cs.PATH_CURRENT_DIR)
        )
    except OSError:
        return []


def _find_tsconfig_files(repo_path: Path) -> list[Path]:
    # (H) tsconfig can live at the repo root OR in a subdirectory (a monorepo's
    # (H) `frontend/`, `packages/*`), so search the root and up to two levels down.
    # (H) Root first so its aliases win prefix-length ties.
    level_one = _child_dirs(repo_path)
    search_dirs = [repo_path, *level_one]
    for child in level_one:
        search_dirs.extend(_child_dirs(child))
    found: list[Path] = []
    for directory in search_dirs:
        for name in cs.TSCONFIG_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                found.append(candidate)
    return found


def _parse_tsconfig_aliases(data: dict, dir_prefix: str) -> list[tuple[str, str, bool]]:
    # (H) Parse one tsconfig's `compilerOptions.paths` into (match_prefix,
    # (H) target_prefix, is_wildcard) tuples, folding baseUrl and the tsconfig's own
    # (H) directory into the target so targets are repo-root-relative. A `@/*`->`src/*`
    # (H) entry in `frontend/tsconfig.json` yields ("@/", "frontend/src/", True); an
    # (H) exact `~lib`->`src/lib/index.ts` yields ("~lib", ".../src/lib/index.ts",
    # (H) False). `extends` chains are not followed.
    options = data.get(cs.TS_COMPILER_OPTIONS_KEY)
    if not isinstance(options, dict):
        return []
    paths = options.get(cs.TS_PATHS_KEY)
    if not isinstance(paths, dict):
        return []
    base = options.get(cs.TS_BASE_URL_KEY) or cs.PATH_CURRENT_DIR
    base = str(base).strip(cs.SEPARATOR_SLASH)
    base_prefix = "" if base in ("", cs.PATH_CURRENT_DIR) else base + cs.SEPARATOR_SLASH
    aliases: list[tuple[str, str, bool]] = []
    for pattern, targets in paths.items():
        if not isinstance(targets, list) or not targets:
            continue
        target = targets[0]
        if not isinstance(pattern, str) or not isinstance(target, str):
            continue
        if pattern.endswith(cs.GLOB_ALL) and cs.GLOB_ALL in target:
            aliases.append(
                (
                    pattern[: -len(cs.GLOB_ALL)],
                    dir_prefix + base_prefix + target[: target.index(cs.GLOB_ALL)],
                    True,
                )
            )
        elif cs.GLOB_ALL not in pattern:
            aliases.append((pattern, dir_prefix + base_prefix + target, False))
    return aliases


def _load_ts_path_aliases(repo_path: Path) -> list[tuple[str, str, bool]]:
    # (H) Aggregate `paths` aliases from every tsconfig at or below the repo root,
    # (H) each target prefixed by the tsconfig's own directory so `@/util` resolves
    # (H) against the config that defines it (a subdir `frontend/tsconfig.json` maps
    # (H) `@/` to `frontend/src/`). _ts_alias_module_qn keeps only aliases whose
    # (H) target exists on disk, so same-prefix aliases from sibling packages do not
    # (H) collide.
    aliases: list[tuple[str, str, bool]] = []
    for cfg in _find_tsconfig_files(repo_path):
        data = _load_jsonc(cfg)
        if not data:
            continue
        parent = cfg.parent
        dir_prefix = (
            ""
            if parent == repo_path
            else parent.relative_to(repo_path).as_posix() + cs.SEPARATOR_SLASH
        )
        aliases.extend(_parse_tsconfig_aliases(data, dir_prefix))
    return aliases


def _has_aliased_scheme(specifier: str) -> bool:
    # (H) True for a JS/TS specifier with a non-standard scheme (`ext:deno_node/x`),
    # (H) which names first-party code under a non-file-path alias. Standard external
    # (H) schemes (node:/npm:/jsr:/http(s):) and bare/scoped package names
    # (H) (`lodash`, `@scope/pkg`) are NOT aliased -> they stay externally suppressed.
    # (H) A tsconfig `paths` alias (`@/util`) has no scheme and is not exempted here
    # (H) (it would be indistinguishable from a scoped package `@scope/pkg`); it is
    # (H) instead resolved PRECISELY to its real module upstream by
    # (H) _resolve_js_module_path via _load_ts_path_aliases, so no trie fallback is
    # (H) needed for it.
    match = _JS_SCHEME_RE.match(specifier)
    return bool(match) and match.group(1).lower() not in cs.JS_EXTERNAL_IMPORT_SCHEMES


class ImportProcessor:
    __slots__ = (
        "repo_path",
        "project_name",
        "ingestor",
        "function_registry",
        "import_mapping",
        "php_function_imports",
        "js_ts_bare_imports",
        "js_path_aliases",
        "stdlib_extractor",
        "_is_local_module_cached",
        "_is_local_java_import_cached",
        "_map_py_source_root",
        "_map_go_import_path",
        "_cpp_module_qn_map",
        "_cpp_qn_to_rel",
        "_deferred_import_edges",
        "_cpp_declaration_mappings",
    )

    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: IngestorProtocol | None = None,
        function_registry: FunctionRegistryTrieProtocol | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        self.import_mapping: dict[str, dict[str, str]] = {}
        # (H) Lazy: replayed walk of every eligible repo file, built on the first
        # (H) C++ include so non-C++ projects never pay for it.
        self._cpp_module_qn_map: dict[str, str] | None = None
        self._cpp_qn_to_rel: dict[str, str] = {}
        # (H) IMPORTS edges held back until every file is parsed, so internal
        # (H) targets verify against the full module registry (issue #652).
        self._deferred_import_edges: list[DeferredImportEdge] = []
        # (H) Import-map entries registered by C++20 module DECLARATIONS
        # (H) (`module X;`, `export module X;`, `import :partition;`). They
        # (H) exist for name resolution only; a declaration is not an import,
        # (H) so no IMPORTS edge may be emitted for them.
        self._cpp_declaration_mappings: set[tuple[str, str]] = set()
        # (H) Local names brought in by a PHP `use function A\B\c` import, keyed by
        # (H) module. A PHP namespace path never matches cgr's file-path qualified
        # (H) name (a global helper declares `namespace Illuminate\Support` from
        # (H) Collections/functions.php), so these must resolve by simple name via
        # (H) the trie rather than being judged external-import and suppressed.
        self.php_function_imports: dict[str, set[str]] = {}
        # (H) Local names brought in by a JS/TS import with a NON-STANDARD scheme
        # (H) (`ext:deno_node/y`; see _has_aliased_scheme), keyed by module. Such a
        # (H) specifier aliases first-party code but does not resolve to a file-path
        # (H) module qn, so the target is unregistered and would be judged external,
        # (H) dropping the call. These names defer to the simple-name trie (like a
        # (H) relative import that misses) instead of being suppressed. Ordinary
        # (H) package specifiers (bare, scoped, node:/npm:) are excluded, so genuine
        # (H) external calls stay suppressed.
        self.js_ts_bare_imports: dict[str, set[str]] = {}
        # (H) tsconfig `paths` aliases (match_prefix, target_prefix, is_wildcard),
        # (H) parsed once from the repo-root tsconfig so `@/util` imports resolve to
        # (H) the real first-party module instead of being dropped as external.
        self.js_path_aliases: list[tuple[str, str, bool]] = _load_ts_path_aliases(
            repo_path
        )
        self.stdlib_extractor = StdlibExtractor(
            function_registry, repo_path, project_name
        )

        repo_is_package = (repo_path / cs.INIT_PY).is_file()

        # (H) Python packages under nested source roots (src-layout, monorepo
        # (H) packages, pyproject package-dir remaps) are importable by a name that
        # (H) differs from their repo-relative path, so absolute imports of them
        # (H) cannot resolve by the import-name == path assumption. Discover the
        # (H) name -> dotted-path map once so those imports resolve first-party.
        py_source_roots = discover_python_source_roots(repo_path)

        @lru_cache(maxsize=4096)
        def _map_py_source_root_cached(module_name: str) -> str | None:
            return resolve_via_source_roots(repo_path, py_source_roots, module_name)

        self._map_py_source_root = _map_py_source_root_cached

        # (H) Go import paths are module-path-prefixed (github.com/acme/tool/pkg),
        # (H) never repo-relative, so no local Go import resolves by the
        # (H) name == path assumption. Map each go.mod module directive to its
        # (H) directory once so local imports rewrite to project-prefixed qns and
        # (H) unmapped (external) paths stay recognizably slash-separated.
        go_module_paths = discover_go_module_paths(repo_path)

        @lru_cache(maxsize=4096)
        def _map_go_import_path_cached(import_path: str) -> str | None:
            return resolve_go_import_path(go_module_paths, import_path)

        self._map_go_import_path = _map_go_import_path_cached

        @lru_cache(maxsize=4096)
        def _is_local_module_cached(module_name: str) -> bool:
            # (H) When the repo root is itself a package, its children are importable
            # (H) only under the package name (project_name.child), never as bare
            # (H) top-level names, so a bare top-level import resolves externally.
            if repo_is_package:
                return module_name == project_name
            return (
                (repo_path / module_name).is_dir()
                or (repo_path / f"{module_name}{cs.EXT_PY}").is_file()
                or (repo_path / module_name / cs.INIT_PY).is_file()
            )

        @lru_cache(maxsize=4096)
        def _is_local_java_import_cached(import_path: str) -> bool:
            top_level = import_path.split(cs.SEPARATOR_DOT)[0]
            return (repo_path / top_level).is_dir()

        self._is_local_module_cached = _is_local_module_cached
        self._is_local_java_import_cached = _is_local_java_import_cached

        load_persistent_cache()

    def __del__(self) -> None:
        try:
            save_persistent_cache()
        except Exception:
            pass

    @staticmethod
    def flush_stdlib_cache() -> None:
        flush_stdlib_cache()

    @staticmethod
    def clear_stdlib_cache() -> None:
        clear_stdlib_cache()

    @staticmethod
    def get_stdlib_cache_stats() -> StdlibCacheStats:
        return get_stdlib_cache_stats()

    def parse_imports(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        pre_captures: dict | None = None,
    ) -> None:
        if language not in queries:
            return
        imports_query = queries[language]["imports"]
        if not imports_query:
            return

        lang_config = queries[language]["config"]

        self.import_mapping[module_qn] = {}
        # (H) Reset per-module PHP use-function state too, so a re-index that drops a
        # (H) `use function` import does not leave a stale exemption behind.
        self.php_function_imports.pop(module_qn, None)
        self.js_ts_bare_imports.pop(module_qn, None)

        try:
            if pre_captures is not None:
                captures = pre_captures
            else:
                cursor = get_query_cursor(imports_query)
                captures = sorted_captures(cursor, root_node)

            match language:
                case cs.SupportedLanguage.PYTHON:
                    self._parse_python_imports(captures, module_qn)
                case (
                    cs.SupportedLanguage.JS
                    | cs.SupportedLanguage.TS
                    | cs.SupportedLanguage.TSX
                ):
                    self._parse_js_ts_imports(captures, module_qn)
                case cs.SupportedLanguage.JAVA:
                    self._parse_java_imports(captures, module_qn)
                case cs.SupportedLanguage.RUST:
                    self._parse_rust_imports(captures, module_qn)
                case cs.SupportedLanguage.GO:
                    self._parse_go_imports(captures, module_qn)
                case cs.SupportedLanguage.CPP:
                    self._parse_cpp_imports(captures, module_qn)
                case cs.SupportedLanguage.LUA:
                    self._parse_lua_imports(captures, module_qn)
                case cs.SupportedLanguage.PHP:
                    self._parse_php_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                ls.IMP_PARSED_COUNT,
                count=len(self.import_mapping[module_qn]),
                module=module_qn,
            )

            if self.ingestor:
                # (H) Hold the edges back: an internal target is only real if
                # (H) some file yields that module qn, which is known only after
                # (H) every file is parsed (flush_deferred_import_edges).
                for full_name in self.import_mapping[module_qn].values():
                    if (module_qn, full_name) in self._cpp_declaration_mappings:
                        continue
                    self._deferred_import_edges.append(
                        DeferredImportEdge(
                            module_qn=module_qn,
                            full_name=full_name,
                            language=language,
                        )
                    )

        except Exception as e:
            logger.warning(ls.IMP_PARSE_FAILED, module=module_qn, error=e)

    def flush_deferred_import_edges(self, known_module_qns: set[str]) -> int:
        """Emit IMPORTS edges now that every file is parsed.

        An external target gets its ExternalModule node as before. An internal
        target must verify against the real module qns; a guess that resolves
        nowhere (a broken import, a directory with no index module, a crate
        path resolved from the wrong root) emits no edge, because the phantom
        endpoint is silently dropped by the database anyway.
        """
        deferred = self._deferred_import_edges
        if not deferred or self.ingestor is None:
            return 0
        self._deferred_import_edges = []
        module_aliases = self._module_alias_map(known_module_qns)
        emitted = 0
        for entry in deferred:
            module_path = self._resolve_module_path(
                entry.full_name, entry.module_qn, entry.language
            )
            target_label = self._module_label(module_path)
            if target_label == cs.NodeLabel.EXTERNAL_MODULE:
                # (H) An external import target has no file pass to create its
                # (H) node; without one here the IMPORTS edge MERGEs against
                # (H) nothing and is silently dropped (issue #652).
                self._ensure_external_module_node(module_path, entry.full_name)
            else:
                verified = self._verify_internal_import_target(
                    module_path, known_module_qns, module_aliases
                )
                if verified is None and entry.language == cs.SupportedLanguage.PYTHON:
                    # (H) A package-anchored guess that names no sibling module
                    # (H) is an ABSOLUTE import in Python semantics (`import
                    # (H) sys` inside a package); re-resolve it as one.
                    if absolute := self._python_absolute_fallback(
                        module_path, entry.module_qn
                    ):
                        self._ensure_external_module_node(absolute, entry.full_name)
                        target_label = cs.NodeLabel.EXTERNAL_MODULE
                        verified = absolute
                if verified is None:
                    logger.debug(
                        ls.IMP_DROPPED_PHANTOM_TARGET,
                        from_module=entry.module_qn,
                        to_module=module_path,
                    )
                    continue
                module_path = verified
            self.ingestor.ensure_relationship_batch(
                (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn),
                cs.RelationshipType.IMPORTS,
                (target_label, cs.KEY_QUALIFIED_NAME, module_path),
            )
            emitted += 1
            logger.debug(
                ls.IMP_CREATED_RELATIONSHIP,
                from_module=entry.module_qn,
                to_module=module_path,
                full_name=entry.full_name,
            )
        return emitted

    def _module_alias_map(self, known_module_qns: set[str]) -> dict[str, str]:
        # (H) A module reached through its container's name: pkg/__init__.py,
        # (H) shared/index.js, utils/mod.rs. Importers write the container qn;
        # (H) the real Module node lives at the index-file leaf.
        aliases: dict[str, str] = {}
        for qn in known_module_qns:
            base, _, leaf = qn.rpartition(cs.SEPARATOR_DOT)
            if base and leaf in cs.MODULE_INDEX_FILE_STEMS:
                aliases[base] = qn
        return aliases

    def _python_absolute_fallback(self, module_path: str, module_qn: str) -> str | None:
        package_qn = module_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        prefix = f"{package_qn}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        written = module_path[len(prefix) :]
        if not written:
            return None
        absolute = self.stdlib_extractor.extract_module_path(
            written, cs.SupportedLanguage.PYTHON
        )
        project_prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not absolute or absolute.startswith(project_prefix):
            return None
        return absolute

    def _verify_internal_import_target(
        self,
        module_path: str,
        known_module_qns: set[str],
        module_aliases: dict[str, str],
    ) -> str | None:
        if module_path in known_module_qns:
            return module_path
        if alias := module_aliases.get(module_path):
            return alias
        # (H) A path resolved from the wrong root (`use crate::utils` written
        # (H) outside src/) still names a unique real module; a whole-segment
        # (H) suffix match recovers it. Ambiguity means no edge, not a guess.
        prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        tail = module_path[len(prefix) :]
        if not tail:
            return None
        suffix = f"{cs.SEPARATOR_DOT}{tail}"
        matches = {qn for qn in known_module_qns if qn.endswith(suffix)}
        matches.update(
            real for base, real in module_aliases.items() if base.endswith(suffix)
        )
        if len(matches) == 1:
            return matches.pop()
        return None

    def _parse_python_imports(self, captures: dict, module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)

    def _handle_python_import_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        for child in import_node.named_children:
            match child.type:
                case cs.TS_DOTTED_NAME:
                    self._handle_dotted_name_import(child, module_qn)
                case cs.TS_ALIASED_IMPORT:
                    self._handle_aliased_import(child, module_qn)

    def _handle_dotted_name_import(self, child: Node, module_qn: str) -> None:
        module_name = safe_decode_text(child) or ""
        local_name = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, local_name)
        self.import_mapping[module_qn][local_name] = full_name
        logger.debug(ls.IMP_IMPORT, local=local_name, full=full_name)

    def _handle_aliased_import(self, child: Node, module_qn: str) -> None:
        module_name_node = child.child_by_field_name(cs.FIELD_NAME)
        alias_node = child.child_by_field_name(cs.FIELD_ALIAS)
        if not module_name_node or not alias_node:
            return

        module_name = safe_decode_text(module_name_node)
        alias = safe_decode_text(alias_node)
        if not module_name or not alias:
            return

        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        full_name = self._resolve_import_full_name(module_name, top_level)
        self.import_mapping[module_qn][alias] = full_name
        logger.debug(ls.IMP_ALIASED_IMPORT, alias=alias, full=full_name)

    def _resolve_import_full_name(self, module_name: str, top_level: str) -> str:
        if module_name == self.project_name or module_name.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            return module_name
        if self._is_local_module(top_level):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        if mapped := self._map_py_source_root(module_name):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
        return module_name

    def _is_local_module(self, module_name: str) -> bool:
        return self._is_local_module_cached(module_name)

    def _is_local_java_import(self, import_path: str) -> bool:
        return self._is_local_java_import_cached(import_path)

    def _resolve_java_import_path(self, import_path: str) -> str:
        if self._is_local_java_import(import_path):
            return f"{self.project_name}{cs.SEPARATOR_DOT}{import_path}"
        return import_path

    def _is_local_js_import(self, full_name: str) -> bool:
        return full_name.startswith(self.project_name + cs.SEPARATOR_DOT)

    def _resolve_js_internal_module(self, full_name: str) -> str:
        if full_name.endswith(cs.IMPORT_DEFAULT_SUFFIX):
            return full_name[: -len(cs.IMPORT_DEFAULT_SUFFIX)]

        parts = full_name.split(cs.SEPARATOR_DOT)
        if len(parts) <= 2:
            return full_name

        potential_module = cs.SEPARATOR_DOT.join(parts[:-1])
        relative_path = cs.SEPARATOR_SLASH.join(parts[1:-1])

        for ext in (cs.EXT_JS, cs.EXT_TS, cs.EXT_JSX, cs.EXT_TSX):
            if (self.repo_path / f"{relative_path}{ext}").is_file():
                return potential_module
            index_path = self.repo_path / relative_path / f"{cs.INDEX_INDEX}{ext}"
            if index_path.is_file():
                return potential_module

        return full_name

    def _is_local_rust_import(self, import_path: str) -> bool:
        return import_path.startswith(cs.RUST_CRATE_PREFIX)

    def _module_label(self, module_path: str) -> cs.NodeLabel:
        # (H) #498: import targets outside the project prefix live under the
        # (H) dedicated ExternalModule label (mirroring Package/ExternalPackage).
        if module_path == self.project_name or module_path.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            return cs.NodeLabel.MODULE
        return cs.NodeLabel.EXTERNAL_MODULE

    def _ensure_external_module_node(self, module_path: str, full_name: str) -> None:
        if not self.ingestor or not module_path:
            return
        if cs.SEPARATOR_DOUBLE_COLON in module_path:
            name = module_path.rsplit(cs.SEPARATOR_DOUBLE_COLON, 1)[-1]
        else:
            name = module_path.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_MODULE,
            {
                cs.KEY_NAME: name,
                cs.KEY_QUALIFIED_NAME: module_path,
                cs.KEY_PATH: full_name,
            },
        )

    def _resolve_rust_import_path(self, import_path: str, module_qn: str) -> str:
        # (H) crate:: is always relative to the crate root, not the current module.
        # (H) We find the src directory in the qualified name to identify the crate root.
        if self._is_local_rust_import(import_path):
            path_without_crate = import_path[len(cs.RUST_CRATE_PREFIX) :]
            module_parts = module_qn.split(cs.SEPARATOR_DOT)
            try:
                src_index = module_parts.index(cs.LANG_SRC_DIR)
                crate_root_qn = cs.SEPARATOR_DOT.join(module_parts[: src_index + 1])
            except ValueError:
                crate_root_qn = self.project_name
            module_part = path_without_crate.split(cs.SEPARATOR_DOUBLE_COLON)[0]
            return f"{crate_root_qn}{cs.SEPARATOR_DOT}{module_part}"

        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = (
            cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]
        )

        self._ensure_external_module_node(module_path, import_path)
        return module_path

    def _resolve_module_path(
        self,
        full_name: str,
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> str:
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        match language:
            # (H) Java MODULE semantics: Internal imports point to file-level MODULE
            # (H) nodes (e.g., project.utils.StringUtils) because Java files are named
            # (H) after their primary class. External imports point to package-level
            # (H) (e.g., java.util) because we lack source code to create file-level
            # (H) nodes. This asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return full_name
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                if self._is_local_js_import(full_name):
                    return self._resolve_js_internal_module(full_name)
            case cs.SupportedLanguage.RUST:
                return self._resolve_rust_import_path(full_name, module_qn)

        module_path = self.stdlib_extractor.extract_module_path(full_name, language)
        if not module_path.startswith(project_prefix):
            self._ensure_external_module_node(module_path, full_name)
        return module_path

    def _handle_python_import_from_statement(
        self, import_node: Node, module_qn: str
    ) -> None:
        module_name = self._extract_python_from_module_name(import_node, module_qn)
        if not module_name:
            return

        imported_items = self._extract_python_imported_items(import_node)
        is_wildcard = any(
            child.type == cs.TS_WILDCARD_IMPORT for child in import_node.children
        )

        if not imported_items and not is_wildcard:
            return

        base_module = self._resolve_python_base_module(module_name)
        self._register_python_from_imports(
            module_qn, base_module, imported_items, is_wildcard
        )

    def _extract_python_from_module_name(
        self, import_node: Node, module_qn: str
    ) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            return safe_decode_text(module_name_node)
        if module_name_node.type == cs.TS_RELATIVE_IMPORT:
            return self._resolve_relative_import(module_name_node, module_qn)
        return None

    def _extract_python_imported_items(
        self, import_node: Node
    ) -> list[tuple[str, str]]:
        imported_items: list[tuple[str, str]] = []

        for name_node in import_node.children_by_field_name(cs.FIELD_NAME):
            if item := self._extract_single_python_import(name_node):
                imported_items.append(item)

        return imported_items

    def _extract_single_python_import(self, name_node: Node) -> tuple[str, str] | None:
        if name_node.type == cs.TS_DOTTED_NAME:
            if name := safe_decode_text(name_node):
                return (name, name)
        elif name_node.type == cs.TS_ALIASED_IMPORT:
            original_node = name_node.child_by_field_name(cs.FIELD_NAME)
            alias_node = name_node.child_by_field_name(cs.FIELD_ALIAS)
            if original_node and alias_node:
                original = safe_decode_text(original_node)
                alias = safe_decode_text(alias_node)
                if original and alias:
                    return (alias, original)
        return None

    def _resolve_python_base_module(self, module_name: str) -> str:
        if module_name.startswith(self.project_name):
            return module_name
        top_level = module_name.split(cs.SEPARATOR_DOT)[0]
        return self._resolve_import_full_name(module_name, top_level)

    def _register_python_from_imports(
        self,
        module_qn: str,
        base_module: str,
        imported_items: list[tuple[str, str]],
        is_wildcard: bool,
    ) -> None:
        if is_wildcard:
            wildcard_key = f"*{base_module}"
            self.import_mapping[module_qn][wildcard_key] = base_module
            logger.debug(ls.IMP_WILDCARD_IMPORT, module=base_module)
            return

        for local_name, original_name in imported_items:
            full_name = f"{base_module}{cs.SEPARATOR_DOT}{original_name}"
            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(ls.IMP_FROM_IMPORT, local=local_name, full=full_name)

    def _is_package_qn(self, module_qn: str) -> bool:
        prefix = self.project_name + cs.SEPARATOR_DOT
        if not module_qn.startswith(prefix):
            return False
        rel = module_qn[len(prefix) :].replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH)
        return (self.repo_path / rel / cs.INIT_PY).is_file()

    def _resolve_relative_import(self, relative_node: Node, module_qn: str) -> str:
        # (H) Relative imports are always internal; resolve to the full project-
        # (H) prefixed qualified name so resolution does not depend on bare-name
        # (H) locality checks (which treat package children as external).
        module_parts = module_qn.split(cs.SEPARATOR_DOT)

        dots = 0
        module_name = ""

        for child in relative_node.children:
            if child.type == cs.TS_IMPORT_PREFIX:
                if decoded_text := safe_decode_text(child):
                    dots = len(decoded_text)
            elif child.type == cs.TS_DOTTED_NAME:
                if decoded_name := safe_decode_text(child):
                    module_name = decoded_name

        # (H) A package's qualified name already IS the package, so `from .` inside
        # (H) an __init__.py drops one fewer level than inside a regular module.
        drop = dots - 1 if self._is_package_qn(module_qn) else dots
        keep = max(len(module_parts) - drop, 0)
        target_parts = module_parts[:keep]

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        # (H) A relative climb that lands at the project root (e.g. `from . import x`
        # (H) in a top-level module) leaves no parts; resolve it to the project root
        # (H) so the import is not silently dropped.
        if not target_parts:
            return self.project_name

        return cs.SEPARATOR_DOT.join(target_parts)

    def _parse_js_ts_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_STATEMENT:
                source_module = None
                is_aliased_scheme = False
                for child in import_node.children:
                    if child.type == cs.TS_STRING:
                        source_text = safe_decode_with_fallback(child).strip("'\"")
                        is_aliased_scheme = _has_aliased_scheme(source_text)
                        source_module = self._resolve_js_module_path(
                            source_text, module_qn
                        )
                        break

                if not source_module:
                    continue

                for child in import_node.children:
                    if child.type == cs.TS_IMPORT_CLAUSE:
                        self._parse_js_import_clause(
                            child, source_module, module_qn, is_aliased_scheme
                        )

            elif import_node.type == cs.TS_LEXICAL_DECLARATION:
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == cs.TS_EXPORT_STATEMENT:
                self._parse_js_reexport(import_node, module_qn)

    def _ts_alias_module_qn(self, import_path: str) -> str | None:
        # (H) Resolve a tsconfig `paths` alias (`@/util` -> `src/util`) to the
        # (H) first-party module qn, so the call binds to the real file instead of
        # (H) being dropped as external. Precise (maps to the actual path), so no
        # (H) trie-fallback collision risk. Longest matching prefix wins.
        # (H) Collect every matching alias (a monorepo may define `@/` in several
        # (H) tsconfigs pointing at different package dirs), then accept the first,
        # (H) longest-prefix one whose target is a real first-party file on disk. The
        # (H) disk check both disambiguates siblings and blocks a catch-all alias
        # (H) (`"*": ["src/*"]`) from capturing bare package imports (`lodash` ->
        # (H) `proj.src.lodash`) and rebinding them to same-named locals (#580).
        candidates: list[tuple[int, str]] = []
        for prefix, target_prefix, is_wildcard in self.js_path_aliases:
            if is_wildcard:
                if import_path.startswith(prefix):
                    candidates.append(
                        (len(prefix), target_prefix + import_path[len(prefix) :])
                    )
            elif import_path == prefix:
                candidates.append((len(prefix), target_prefix))
        candidates.sort(key=lambda c: c[0], reverse=True)
        for _prefix_len, raw_path in candidates:
            path = raw_path
            for ext in cs.JS_TS_MODULE_EXTENSIONS:
                if path.endswith(ext):
                    path = path[: -len(ext)]
                    break
            # (H) normpath collapses `.`/`..` so the qn is clean and an escaping alias
            # (H) (`../x`) is rejected below.
            normalized = posixpath.normpath(path)
            if normalized in (cs.PATH_CURRENT_DIR, "") or normalized.startswith(
                cs.PATH_PARENT_DIR
            ):
                continue
            module_rel: str | None = None
            if any(
                (self.repo_path / f"{normalized}{ext}").is_file()
                for ext in cs.JS_TS_MODULE_EXTENSIONS
            ):
                module_rel = normalized
            elif (self.repo_path / normalized).is_dir() and any(
                (self.repo_path / normalized / f"{cs.JS_INDEX_STEM}{ext}").is_file()
                for ext in cs.JS_TS_MODULE_EXTENSIONS
            ):
                module_rel = f"{normalized}{cs.SEPARATOR_SLASH}{cs.JS_INDEX_STEM}"
            if module_rel is None:
                continue
            dotted = module_rel.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
            return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        return None

    @staticmethod
    def _strip_js_extension(import_path: str) -> str:
        # (H) ESM specifiers may carry an explicit extension (`./b.js`); the
        # (H) module qn never does, so keeping it poisons the import map AND
        # (H) the IMPORTS edge with a phantom `.js` segment (issue #652).
        for ext in cs.JS_TS_ALL_EXTENSIONS:
            if import_path.endswith(ext) and len(import_path) > len(ext):
                return import_path[: -len(ext)]
        return import_path

    def _resolve_js_module_path(self, import_path: str, current_module: str) -> str:
        import_path = self._strip_js_extension(import_path)
        if not import_path.startswith(cs.PATH_CURRENT_DIR):
            if aliased := self._ts_alias_module_qn(import_path):
                return aliased
            return import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        current_parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
        import_parts = import_path.split(cs.SEPARATOR_SLASH)

        for part in import_parts:
            if part == cs.PATH_CURRENT_DIR:
                continue
            if part == cs.PATH_PARENT_DIR:
                if current_parts:
                    current_parts.pop()
            elif part:
                current_parts.append(part)

        return cs.SEPARATOR_DOT.join(current_parts)

    def _parse_js_import_clause(
        self,
        clause_node: Node,
        source_module: str,
        current_module: str,
        is_aliased_scheme: bool = False,
    ) -> None:
        def _note_bare(local_name: str) -> None:
            if is_aliased_scheme:
                self.js_ts_bare_imports.setdefault(current_module, set()).add(
                    local_name
                )

        for child in clause_node.children:
            if child.type == cs.TS_IDENTIFIER:
                imported_name = safe_decode_with_fallback(child)
                self.import_mapping[current_module][imported_name] = (
                    f"{source_module}{cs.IMPORT_DEFAULT_SUFFIX}"
                )
                _note_bare(imported_name)
                logger.debug(
                    ls.IMP_JS_DEFAULT, name=imported_name, module=source_module
                )

            elif child.type == cs.TS_NAMED_IMPORTS:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            imported_name = safe_decode_with_fallback(name_node)
                            local_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else imported_name
                            )
                            self.import_mapping[current_module][local_name] = (
                                f"{source_module}{cs.SEPARATOR_DOT}{imported_name}"
                            )
                            _note_bare(local_name)
                            logger.debug(
                                ls.IMP_JS_NAMED,
                                local=local_name,
                                module=source_module,
                                name=imported_name,
                            )

            elif child.type == cs.TS_NAMESPACE_IMPORT:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IDENTIFIER:
                        namespace_name = safe_decode_with_fallback(grandchild)
                        self.import_mapping[current_module][namespace_name] = (
                            source_module
                        )
                        logger.debug(
                            ls.IMP_JS_NAMESPACE,
                            name=namespace_name,
                            module=source_module,
                        )
                        break

    def _parse_js_require(self, decl_node: Node, current_module: str) -> None:
        for declarator in decl_node.children:
            if declarator.type == cs.TS_VARIABLE_DECLARATOR:
                name_node = declarator.child_by_field_name(cs.FIELD_NAME)
                value_node = declarator.child_by_field_name(cs.FIELD_VALUE)

                if (
                    name_node
                    and value_node
                    and name_node.type == cs.TS_IDENTIFIER
                    and value_node.type == cs.TS_CALL_EXPRESSION
                ):
                    func_node = value_node.child_by_field_name(cs.FIELD_FUNCTION)
                    args_node = value_node.child_by_field_name(cs.FIELD_ARGUMENTS)

                    if (
                        func_node
                        and args_node
                        and func_node.type == cs.TS_IDENTIFIER
                        and safe_decode_text(func_node) == cs.IMPORT_REQUIRE
                    ):
                        for arg in args_node.children:
                            if arg.type == cs.TS_STRING:
                                var_name = safe_decode_with_fallback(name_node)
                                required_module = safe_decode_with_fallback(arg).strip(
                                    "'\""
                                )

                                resolved_module = self._resolve_js_module_path(
                                    required_module, current_module
                                )
                                self.import_mapping[current_module][var_name] = (
                                    resolved_module
                                )
                                logger.debug(
                                    ls.IMP_JS_REQUIRE,
                                    var=var_name,
                                    module=resolved_module,
                                )
                                break

    def _parse_js_reexport(self, export_node: Node, current_module: str) -> None:
        source_module = None
        for child in export_node.children:
            if child.type == cs.TS_STRING:
                source_text = safe_decode_with_fallback(child).strip("'\"")
                source_module = self._resolve_js_module_path(
                    source_text, current_module
                )
                break

        if not source_module:
            return

        for child in export_node.children:
            if child.type == cs.TS_ASTERISK:
                wildcard_key = f"*{source_module}"
                self.import_mapping[current_module][wildcard_key] = source_module
                logger.debug(ls.IMP_JS_NAMESPACE_REEXPORT, module=source_module)
            elif child.type == cs.TS_EXPORT_CLAUSE:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_EXPORT_SPECIFIER:
                        name_node = grandchild.child_by_field_name(cs.FIELD_NAME)
                        alias_node = grandchild.child_by_field_name(cs.FIELD_ALIAS)
                        if name_node:
                            original_name = safe_decode_with_fallback(name_node)
                            exported_name = (
                                safe_decode_with_fallback(alias_node)
                                if alias_node
                                else original_name
                            )
                            self.import_mapping[current_module][exported_name] = (
                                f"{source_module}{cs.SEPARATOR_DOT}{original_name}"
                            )
                            logger.debug(
                                ls.IMP_JS_REEXPORT,
                                exported=exported_name,
                                module=source_module,
                                original=original_name,
                            )

    def _parse_java_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_IMPORT_DECLARATION:
                is_static = False
                imported_path = None
                is_wildcard = False

                for child in import_node.children:
                    if child.type == cs.TS_STATIC:
                        is_static = True
                    elif child.type == cs.TS_SCOPED_IDENTIFIER:
                        imported_path = safe_decode_with_fallback(child)
                    elif child.type == cs.TS_ASTERISK:
                        is_wildcard = True

                if not imported_path:
                    continue

                resolved_path = self._resolve_java_import_path(imported_path)

                if is_wildcard:
                    logger.debug(ls.IMP_JAVA_WILDCARD, path=resolved_path)
                    self.import_mapping[module_qn][f"*{resolved_path}"] = resolved_path
                elif parts := resolved_path.split(cs.SEPARATOR_DOT):
                    imported_name = parts[-1]
                    self.import_mapping[module_qn][imported_name] = resolved_path
                    if is_static:
                        logger.debug(
                            ls.IMP_JAVA_STATIC,
                            name=imported_name,
                            path=resolved_path,
                        )
                    else:
                        logger.debug(
                            ls.IMP_JAVA_IMPORT,
                            name=imported_name,
                            path=resolved_path,
                        )

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_USE_DECLARATION:
                self._parse_rust_use_declaration(import_node, module_qn)

    def _parse_rust_use_declaration(self, use_node: Node, module_qn: str) -> None:
        imports = rs_utils.extract_use_imports(use_node)

        for imported_name, full_path in imports.items():
            self.import_mapping[module_qn][imported_name] = full_path
            logger.debug(ls.IMP_RUST, name=imported_name, path=full_path)

    def _parse_go_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_GO_IMPORT_DECLARATION:
                self._parse_go_import_declaration(import_node, module_qn)

    def _parse_go_import_declaration(self, import_node: Node, module_qn: str) -> None:
        for child in import_node.children:
            if child.type == cs.TS_IMPORT_SPEC:
                self._parse_go_import_spec(child, module_qn)
            elif child.type == cs.TS_IMPORT_SPEC_LIST:
                for grandchild in child.children:
                    if grandchild.type == cs.TS_IMPORT_SPEC:
                        self._parse_go_import_spec(grandchild, module_qn)

    def _parse_go_import_spec(self, spec_node: Node, module_qn: str) -> None:
        alias_name = None
        import_path = None

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            # (H) A path under a local go.mod module rewrites to the package dir's
            # (H) project qn ('' remainder = the module root package itself), so
            # (H) both the IMPORTS edge and call resolution bind first-party.
            # (H) External paths stay raw.
            if (mapped := self._map_go_import_path(import_path)) is not None:
                import_path = (
                    f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
                    if mapped
                    else self.project_name
                )
            self.import_mapping[module_qn][package_name] = import_path
            logger.debug(ls.IMP_GO, package=package_name, path=import_path)

    def _parse_cpp_imports(self, captures: dict, module_qn: str) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type == cs.TS_PREPROC_INCLUDE:
                self._parse_cpp_include(import_node, module_qn)
            elif import_node.type == cs.TS_TEMPLATE_FUNCTION:
                self._parse_cpp_module_import(import_node, module_qn)
            elif import_node.type == cs.TS_DECLARATION:
                self._parse_cpp_module_declaration(import_node, module_qn)

    def _resolve_cpp_include_target(
        self, include_path: str, module_qn: str
    ) -> str | None:
        """Resolve a quoted #include to the module qn of a real repo file.

        Tries the includer's directory, then the repo root, then a unique
        path-suffix match (covers -I style includes written relative to a
        source root). Returns None for headers outside the repo.
        """
        if self._cpp_module_qn_map is None:
            self._cpp_module_qn_map = build_module_qn_map(
                self.repo_path, self.project_name
            )
            self._cpp_qn_to_rel = {
                qn: rel for rel, qn in self._cpp_module_qn_map.items()
            }
        normalized = os.path.normpath(include_path).replace(os.sep, cs.SEPARATOR_SLASH)

        includer_rel = self._cpp_qn_to_rel.get(module_qn)
        if includer_rel is not None:
            candidate = os.path.normpath(
                str(Path(includer_rel).parent / normalized)
            ).replace(os.sep, cs.SEPARATOR_SLASH)
            if qn := self._cpp_module_qn_map.get(candidate):
                return qn

        if qn := self._cpp_module_qn_map.get(normalized):
            return qn

        suffix = f"{cs.SEPARATOR_SLASH}{normalized}"
        matches = sorted(rel for rel in self._cpp_module_qn_map if rel.endswith(suffix))
        if not matches:
            return None
        if len(matches) > 1 and includer_rel is not None:
            # (H) Prefer the header sharing the longest path prefix with the
            # (H) includer (the same source tree), deterministically. commonpath
            # (H) (not commonprefix) so sibling dirs with a shared name prefix
            # (H) (src/ast vs src/ast_new) rank by whole components.
            matches.sort(
                key=lambda rel: (
                    -len(os.path.commonpath([rel, includer_rel])),
                    rel,
                )
            )
        return self._cpp_module_qn_map[matches[0]]

    def _parse_cpp_include(self, include_node: Node, module_qn: str) -> None:
        include_path = None
        is_system_include = False

        for child in include_node.children:
            if child.type == cs.TS_STRING_LITERAL:
                include_path = safe_decode_with_fallback(child).strip('"')
                is_system_include = False
            elif child.type == cs.TS_SYSTEM_LIB_STRING:
                include_path = safe_decode_with_fallback(child).strip("<>")
                is_system_include = True

        if include_path:
            header_name = include_path.split(cs.SEPARATOR_SLASH)[-1]
            if header_name.endswith(cs.EXT_H) or header_name.endswith(cs.EXT_HPP):
                local_name = header_name.split(cs.SEPARATOR_DOT)[0]
            else:
                local_name = header_name

            if is_system_include:
                full_name = (
                    include_path
                    if include_path.startswith(cs.CPP_STD_PREFIX)
                    else f"{cs.IMPORT_STD_PREFIX}{include_path}"
                )
            elif resolved := self._resolve_cpp_include_target(include_path, module_qn):
                # (H) The include resolves to a real repo file; use that file's
                # (H) actual (collision-disambiguated) module qn. The old
                # (H) project-rooted, extension-stripped guess produced phantom
                # (H) module qns (self-imports for same-stem header/source pairs,
                # (H) wrong roots for -I style includes), which poisoned both the
                # (H) IMPORTS edges and class resolution via the import map
                # (H) (issue #652).
                full_name = resolved
            else:
                # (H) A quoted include that matches no repo file is a third-party
                # (H) header; a project-rooted qn would be a phantom.
                full_name = f"{cs.IMPORT_STD_PREFIX}{include_path}"

            self.import_mapping[module_qn][local_name] = full_name
            logger.debug(
                ls.IMP_CPP_INCLUDE,
                local=local_name,
                full=full_name,
                system=is_system_include,
            )

    def _parse_cpp_module_import(self, import_node: Node, module_qn: str) -> None:
        identifier_child = None
        template_args_child = None

        for child in import_node.children:
            if child.type == cs.TS_IDENTIFIER:
                identifier_child = child
            elif child.type == cs.TS_TEMPLATE_ARGUMENT_LIST:
                template_args_child = child

        if (
            identifier_child
            and safe_decode_text(identifier_child) == cs.IMPORT_IMPORT
            and template_args_child
        ):
            module_name = None
            for child in template_args_child.children:
                if child.type == cs.TS_TYPE_DESCRIPTOR:
                    for desc_child in child.children:
                        if desc_child.type == cs.TS_TYPE_IDENTIFIER:
                            module_name = safe_decode_with_fallback(desc_child)
                            break
                elif child.type == cs.TS_TYPE_IDENTIFIER:
                    module_name = safe_decode_with_fallback(child)

            if module_name:
                local_name = module_name
                full_name = f"{cs.IMPORT_STD_PREFIX}{module_name}"

                self.import_mapping[module_qn][local_name] = full_name
                logger.debug(ls.IMP_CPP_MODULE, local=local_name, full=full_name)

    def _parse_cpp_module_declaration(self, decl_node: Node, module_qn: str) -> None:
        decoded_text = safe_decode_text(decl_node)
        if not decoded_text:
            return
        decl_text = decoded_text.strip()

        if decl_text.startswith(cs.CPP_MODULE_PREFIX) and not decl_text.startswith(
            cs.CPP_MODULE_PRIVATE_PREFIX
        ):
            parts = decl_text.split()
            if len(parts) >= 2:
                self._register_cpp_module_mapping(
                    parts, 1, module_qn, ls.IMP_CPP_MODULE_IMPL
                )
        elif decl_text.startswith(cs.CPP_EXPORT_MODULE_PREFIX):
            parts = decl_text.split()
            if len(parts) >= 3:
                self._register_cpp_module_mapping(
                    parts, 2, module_qn, ls.IMP_CPP_MODULE_IFACE
                )
        elif cs.CPP_IMPORT_PARTITION_PREFIX in decl_text:
            colon_pos = decl_text.find(cs.SEPARATOR_COLON)
            if colon_pos != -1:
                if partition_part := decl_text[colon_pos + 1 :].split(";")[0].strip():
                    partition_name = f"{cs.CPP_PARTITION_PREFIX}{partition_part}"
                    full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{partition_part}"
                    self.import_mapping[module_qn][partition_name] = full_name
                    # (H) A partition lives inside the same named module; no
                    # (H) graph node models it, so never emit an IMPORTS edge.
                    self._cpp_declaration_mappings.add((module_qn, full_name))
                    logger.debug(
                        ls.IMP_CPP_PARTITION,
                        partition=partition_name,
                        full=full_name,
                    )

    def _register_cpp_module_mapping(
        self, parts: list[str], name_index: int, module_qn: str, log_template: str
    ) -> None:
        module_name = parts[name_index].rstrip(";")
        full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
        self.import_mapping[module_qn][module_name] = full_name
        # (H) `module X;` / `export module X;` DECLARE this file's module; the
        # (H) mapping exists for name resolution only, never as an IMPORTS edge.
        self._cpp_declaration_mappings.add((module_qn, full_name))
        logger.debug(log_template, name=module_name)

    _PHP_INCLUDE_REQUIRE_TYPES = frozenset(
        {
            cs.TS_PHP_INCLUDE_EXPRESSION,
            cs.TS_PHP_INCLUDE_ONCE_EXPRESSION,
            cs.TS_PHP_REQUIRE_EXPRESSION,
            cs.TS_PHP_REQUIRE_ONCE_EXPRESSION,
        }
    )

    def _parse_php_imports(self, captures: dict, module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            if import_node.type == cs.TS_PHP_NAMESPACE_USE_DECLARATION:
                self._handle_php_use_declaration(import_node, module_qn)
            elif import_node.type in self._PHP_INCLUDE_REQUIRE_TYPES:
                self._handle_php_include_require(import_node, module_qn)

    def _handle_php_use_declaration(self, use_node: Node, module_qn: str) -> None:
        # (H) `use function A\B\c` / `use const A\B\C` carry the modifier either on the
        # (H) declaration (older grammar) or inside each clause (current grammar).
        decl_is_function = any(c.type == cs.TS_PHP_FUNCTION for c in use_node.children)
        for child in use_node.named_children:
            if child.type != cs.TS_PHP_NAMESPACE_USE_CLAUSE:
                continue
            qn_node = next(
                (c for c in child.named_children if c.type == cs.TS_PHP_QUALIFIED_NAME),
                None,
            )
            if not qn_node:
                continue
            imported_path = safe_decode_with_fallback(qn_node)
            if not imported_path:
                continue
            imported_path = imported_path.replace("\\", cs.SEPARATOR_DOT)
            alias_node = child.child_by_field_name("alias")
            if alias_node and alias_node.text:
                local_name = safe_decode_with_fallback(alias_node)
            else:
                parts = imported_path.split(cs.SEPARATOR_DOT)
                local_name = parts[-1] if parts else imported_path
            self.import_mapping[module_qn][local_name] = imported_path
            if decl_is_function or any(
                c.type == cs.TS_PHP_FUNCTION for c in child.children
            ):
                self.php_function_imports.setdefault(module_qn, set()).add(local_name)

    def _handle_php_include_require(self, node: Node, module_qn: str) -> None:
        for child in node.children:
            if child.type in {"string", "encapsed_string"}:
                raw = safe_decode_with_fallback(child)
                if not raw:
                    continue
                path_str = raw.strip("'\"")
                path_str = path_str.replace("/", cs.SEPARATOR_DOT).replace(
                    "\\", cs.SEPARATOR_DOT
                )
                if path_str.endswith(".php"):
                    path_str = path_str[:-4]
                parts = path_str.split(cs.SEPARATOR_DOT)
                local_name = parts[-1] if parts else path_str
                self.import_mapping[module_qn][local_name] = path_str
                return

    def _parse_generic_imports(
        self, captures: dict, module_qn: str, lang_config: LanguageSpec
    ) -> None:
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            logger.debug(
                ls.IMP_GENERIC,
                language=lang_config.language,
                node_type=import_node.type,
            )

    def _parse_lua_imports(self, captures: dict, module_qn: str) -> None:
        for call_node in captures.get(cs.CAPTURE_IMPORT, []):
            if self._lua_is_require_call(call_node):
                if module_path := self._lua_extract_require_arg(call_node):
                    local_name = (
                        self._lua_extract_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved
            elif self._lua_is_pcall_require(call_node):
                if module_path := self._lua_extract_pcall_require_arg(call_node):
                    local_name = (
                        self._lua_extract_pcall_assignment_lhs(call_node)
                        or module_path.split(cs.SEPARATOR_DOT)[-1]
                    )
                    resolved = self._resolve_lua_module_path(module_path, module_qn)
                    self.import_mapping[module_qn][local_name] = resolved

            elif self._lua_is_stdlib_call(call_node):
                if stdlib_module := self._lua_extract_stdlib_module(call_node):
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == cs.TS_IDENTIFIER:
            return safe_decode_text(first_child) == cs.IMPORT_REQUIRE
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_child) == cs.IMPORT_PCALL
        ):
            return False

        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return False

        first_arg_node = next(
            (
                child
                for child in args.children
                if child.type not in cs.PUNCTUATION_TYPES
            ),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_arg_node) == cs.IMPORT_REQUIRE
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(node):
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return None
        found_require = False
        for child in args.children:
            if found_require and child.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(child):
                    return decoded.strip("'\"")
            if (
                child.type == cs.TS_IDENTIFIER
                and safe_decode_text(child) == cs.IMPORT_REQUIRE
            ):
                found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            call_node, accepted_var_types=(cs.TS_IDENTIFIER,)
        )

    def _lua_extract_pcall_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_pcall_second_identifier(call_node)

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith(cs.PATH_RELATIVE_PREFIX) or import_path.startswith(
            cs.PATH_PARENT_PREFIX
        ):
            parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
            rel_parts = list(
                import_path.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)
            )
            for p in rel_parts:
                if p == cs.PATH_CURRENT_DIR:
                    continue
                if p == cs.PATH_PARENT_DIR:
                    if parts:
                        parts.pop()
                elif p:
                    parts.append(p)
            return cs.SEPARATOR_DOT.join(parts)
        dotted = import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        try:
            relative_file = (
                dotted.replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH) + cs.EXT_LUA
            )
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            if (self.repo_path / f"{dotted}{cs.EXT_LUA}").is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            module_name = safe_decode_text(first_child.children[0])
            return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            return safe_decode_text(first_child.children[0])

        return None
