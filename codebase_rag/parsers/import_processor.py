import json
import os
import posixpath
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LANGUAGE_SPECS, LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import (
    DeferredImportEdge,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    FunctionSpanKey,
    LanguageQueries,
)
from ..utils.path_utils import should_keep_dir, should_skip_rel_file
from .cpp_frontend.qn import build_module_qn_map
from .dart import dart_extract_uri, dart_local_name, dart_resolve_import
from .go import discover_go_module_paths, resolve_go_import_path
from .js_ts.module_paths import (
    discover_js_workspace_packages,
    resolve_js_workspace_import,
)
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
# Bodyless `mod NAME;` declarations in a Rust crate entry file: the mod graph
# is what assigns a file to the lib or the bin crate (issue #1007). Attribute
# prefixes on the same line (`#[cfg(unix)] mod unix;`) are idiomatic; the
# source is lexed through _rs_strip_comments_and_strings first so neither a
# commented-out declaration nor one hidden behind a string literal containing
# a comment marker can flip the crate attribution.
_RS_MOD_DECL_PATTERN = re.compile(
    r"^[^\S\n]*(?:#\[[^\]\n]*\][^\S\n]*)*(?:pub\s*(?:\([^)]*\))?\s+)?"
    r"mod\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
# Top-level item declarations in an entry file: `crate::Item` attaches under
# the entry module's qn when the entry declares Item, and the declaring entry
# disambiguates the path when BOTH src/lib.rs and src/main.rs declare the
# importing module. `mod` here catches INLINE `mod x { ... }` blocks, which
# declare a name in the entry module but pull no file into the crate (the
# bodyless pattern above is what assigns files).
# A `#[path = "..."]` attribute among a bodyless declaration's attributes
# names the file backing it, which is where the qn scheme keys the module.
# Only the plain string form is read: a cfg_attr wrapper is conditional and
# no single target speaks for it (issue #1035).
_RS_MOD_REDIRECT_PATTERN = re.compile(
    r"^[^\S\n]*((?:#\[[^\]\n]*\][^\S\n]*)*)"
    r"(?:pub\s*(?:\([^)]*\))?\s+)?"
    r"mod\s+(?:r#)?([A-Za-z_][A-Za-z0-9_]*)\s*;",
    re.MULTILINE,
)
# One attribute-only line, for the linear back-walk that gathers the block
# ABOVE a declaration. Keeping every attribute group line-local is what makes
# a long unbroken attribute run scan linearly: with `\s*` crossing newlines,
# every line of the run restarted a match attempt that consumed the rest of
# the run before failing, which is quadratic in the run length (issue #1089).
_RS_ATTRIBUTE_LINE_PATTERN = re.compile(r"^[^\S\n]*((?:#\[[^\]\n]*\][^\S\n]*)+)$")
_RS_PATH_ATTRIBUTE_PATTERN = re.compile(r'#\[\s*path\s*=\s*"([^"\n]+)"\s*\]')
# The opening of a path attribute, matched against the code the lexer has
# already emitted, so the literal that follows can be kept verbatim.
_RS_PATH_ATTRIBUTE_OPEN = re.compile(r"#\[\s*path\s*=\s*$")
_RS_ITEM_DECL_PATTERN = re.compile(
    r"^[^\S\n]*(?:#\[[^\]\n]*\][^\S\n]*)*(?:pub\s*(?:\([^)]*\))?\s+)?"
    r'(?:(?:unsafe|async|const|extern\s+"[^"]*")\s+)*'
    r"(?:trait|struct|enum|fn|type|const|static|union|mod)"
    r"\s+(?:mut\s+)?(?:r#)?([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
# A depth-0 `{` opens a macro body rather than an item body when the text
# before it ends in a macro NAME followed by `!` (an invocation: `cfg_if! {`)
# or names a macro_rules definition. Macro bodies stay visible to the
# declaration scans: libc, backtrace and getrandom declare their platform
# `mod` files inside cfg_if!. A diverging `fn abort() -> ! {` also ends in
# `!`, but with no name touching it: that brace opens an item body.
_RS_MACRO_OPEN_RE = re.compile(r"(?:\w!|\bmacro_rules!\s*(?:r#)?[A-Za-z_]\w*)\s*$")


def _rs_strip_comments_and_strings(source: str) -> str:
    """Blank Rust comments and string/char contents, keeping line structure.

    A regex cannot do this: block comments NEST, and string literals may
    contain comment markers (`const P: &str = "/*";`) or newline-separated
    text that would fake a `mod x;` line.

    The one literal kept is a `#[path = "..."]` value, which names the file
    backing a mod declaration and so has to survive for the declaration
    scan to read it (issue #1035). Keeping it is safe precisely because
    this lexer decides it: the opening `#[path =` is matched in code the
    lexer has already walked, so no comment or outer string can present
    one.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = source.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and nxt == "*":
            depth = 1
            i += 2
            while i < n and depth:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    if source[i] == "\n":
                        out.append("\n")
                    i += 1
            continue
        if c == "r" and nxt in ('"', "#"):
            j = i + 1
            hashes = 0
            while j < n and source[j] == "#":
                hashes += 1
                j += 1
            if j < n and source[j] == '"':
                end_marker = '"' + "#" * hashes
                k = source.find(end_marker, j + 1)
                i = n if k == -1 else k + len(end_marker)
                out.append('""')
                continue
        if c == '"':
            start = i
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == '"':
                    i += 1
                    break
                i += 1
            literal = source[start:i]
            keep = _RS_PATH_ATTRIBUTE_OPEN.search("".join(out[-80:])) is not None
            out.append(literal if keep and "\n" not in literal else '""')
            continue
        if c == "'":
            # A char literal ('x', '\n', '\u{7f}'); a lifetime ('a) has no
            # closing quote and passes through untouched. The escape must be
            # honoured: pairing '\'' at its FIRST following quote leaves an
            # orphan quote that swallows the rest of the file.
            j = i + 1
            if j < n and source[j] == "\\":
                k = j + 1
                if source.startswith("u{", k):
                    brace = source.find("}", k + 2)
                    k = k + 2 if brace == -1 else brace + 1
                elif k < n and source[k] == "x":
                    k += 3
                else:
                    k += 1
                if k < n and source[k] == "'":
                    out.append("''")
                    i = k + 1
                    continue
            elif j + 1 < n and source[j] not in ("'", "\n") and source[j + 1] == "'":
                out.append("''")
                i = j + 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


class RustEntryDecls(NamedTuple):
    """What an entry file's top level declares, for crate attribution."""

    mods: set[str]
    items: set[str]
    # Declared name -> the file path its `#[path]` attribute names, kept
    # separate because the qn scheme keys the module by that FILE while
    # every crate path names it by the declared name (issue #1035).
    redirects: dict[str, str]


def _rs_preceding_attribute_lines(top_level: str, decl_start: int) -> str:
    collected: list[str] = []
    end = decl_start
    while end > 0:
        line_start = top_level.rfind("\n", 0, end - 1) + 1
        line = top_level[line_start : end - 1]
        if not line.strip():
            # Whitespace between an attribute and its item is legal; keep
            # walking, exactly as the old newline-crossing `\s*` matched.
            end = line_start
            continue
        line_match = _RS_ATTRIBUTE_LINE_PATTERN.fullmatch(line)
        if line_match is None:
            break
        collected.append(line_match.group(1))
        end = line_start
    return "".join(reversed(collected))


def _rs_entry_decls_of(top_level: str) -> RustEntryDecls:
    # One name may be declared once per cfg (`#[cfg(unix)] mod platform;`
    # beside its windows twin), each redirected somewhere else. Exactly one
    # of them compiles and nothing here knows which, so disagreeing targets
    # are ambiguous and the name keeps no redirect at all.
    redirects: dict[str, str] = {}
    ambiguous: set[str] = set()
    for decl in _RS_MOD_REDIRECT_PATTERN.finditer(top_level):
        attributes, name = decl.group(1), decl.group(2)
        # The pattern only sees SAME-LINE attributes now (line-local group,
        # issue #1089); the block above the declaration is gathered by a
        # linear walk over the preceding attribute-only lines.
        attributes = _rs_preceding_attribute_lines(top_level, decl.start()) + attributes
        match = _RS_PATH_ATTRIBUTE_PATTERN.search(attributes)
        target = match.group(1) if match else None
        if name in redirects and redirects[name] != target:
            ambiguous.add(name)
        elif target is not None:
            redirects[name] = target
    for name in ambiguous:
        redirects.pop(name, None)
    return RustEntryDecls(
        set(_RS_MOD_DECL_PATTERN.findall(top_level)),
        set(_RS_ITEM_DECL_PATTERN.findall(top_level)),
        redirects,
    )


def _rs_top_level_only(stripped: str) -> str:
    """Keep only brace-depth-zero text of a comment-stripped source.

    A `mod unix;` nested in an inline `mod sys { ... }` block declares a
    file in a DIFFERENT directory, and a method inside an `impl` block is
    not a crate-root item; both would otherwise match the line-anchored
    declaration patterns. Depth-0 MACRO bodies (`cfg_if! { ... }`,
    `macro_rules! m { ... }`) are kept instead: the declarations they emit
    are top-level, so their braces and semicolons become newlines to keep
    each one line-anchored. A depth-0 `}` becomes a newline so a
    declaration following it on the same line stays anchored.
    """
    out: list[str] = []
    depth = 0
    in_macro = False
    for c in stripped:
        if c == "{":
            if depth == 0:
                in_macro = bool(_RS_MACRO_OPEN_RE.search("".join(out[-80:])))
                out.append(c)
                if in_macro:
                    out.append("\n")
            elif in_macro:
                out.append("\n")
            depth += 1
        elif c == "}":
            depth = max(depth - 1, 0)
            if depth == 0:
                out.append("\n")
                in_macro = False
            elif in_macro:
                out.append("\n")
        elif depth == 0 or c == "\n":
            out.append(c)
        elif in_macro:
            out.append(c)
            if c == ";":
                out.append("\n")
    return "".join(out)


_JSONC_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JSONC_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_JSONC_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _js_destructured_names(pattern: Node) -> list[tuple[str, str]]:
    # (local, imported) name pairs bound by an object destructuring pattern:
    # `{ writeFileSync }` -> (writeFileSync, writeFileSync); `{ x: y }` -> (y, x).
    out: list[tuple[str, str]] = []
    for child in pattern.named_children:
        if child.type == cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN:
            if name := safe_decode_text(child):
                out.append((name, name))
        elif child.type == cs.TS_PAIR_PATTERN:
            key = child.child_by_field_name(cs.FIELD_KEY)
            value = child.child_by_field_name(cs.FIELD_VALUE)
            if key is not None and value is not None and value.type == cs.TS_IDENTIFIER:
                imported = safe_decode_text(key)
                local = safe_decode_text(value)
                if imported and local:
                    out.append((local, imported))
    return out


def _load_jsonc(path: Path) -> dict | None:
    # tsconfig.json is JSONC (comments, trailing commas). Try strict JSON first,
    # then fall back to stripping comments/trailing commas. The naive strip can
    # mangle `//` inside string values, so it is only a fallback; on any failure
    # return None (aliases simply stay unresolved).
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
    # Immediate subdirectories worth searching, pruning dependency/build/VCS trees
    # at traversal time so we never stat into node_modules (thousands of package
    # tsconfigs) or hidden dirs.
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
    # tsconfig can live at the repo root OR in a subdirectory (a monorepo's
    # `frontend/`, `packages/*`), so search the root and up to two levels down; root
    # first so its aliases win prefix-length ties.
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
    # Parse one tsconfig's `compilerOptions.paths` into (match_prefix,
    # target_prefix, is_wildcard) tuples, folding baseUrl and the tsconfig's own
    # directory into the target so targets are repo-root-relative. A `@/*`->`src/*`
    # entry in `frontend/tsconfig.json` yields ("@/", "frontend/src/", True); an
    # exact `~lib`->`src/lib/index.ts` yields ("~lib", ".../src/lib/index.ts",
    # False). `extends` chains are not followed.
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
    # Aggregate `paths` aliases from every tsconfig at or below the repo root, each
    # target prefixed by the tsconfig's own directory so `@/util` resolves against
    # the config that defines it (a subdir `frontend/tsconfig.json` maps `@/` to
    # `frontend/src/`). _ts_alias_module_qn keeps only aliases whose target exists
    # on disk, so same-prefix aliases from sibling packages do not collide.
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
    # True for a JS/TS specifier with a non-standard scheme (`ext:deno_node/x`),
    # which names first-party code under a non-file-path alias. Standard external
    # schemes (node:/npm:/jsr:/http(s):) and bare/scoped package names (`lodash`,
    # `@scope/pkg`) are NOT aliased, so they stay externally suppressed. A tsconfig
    # `paths` alias (`@/util`) has no scheme and is not exempted here (it would be
    # indistinguishable from a scoped package `@scope/pkg`); it is instead resolved
    # PRECISELY to its real module upstream by _resolve_js_module_path via
    # _load_ts_path_aliases, so no trie fallback is needed for it.
    match = _JS_SCHEME_RE.match(specifier)
    return bool(match) and match.group(1).lower() not in cs.JS_EXTERNAL_IMPORT_SCHEMES


def _is_conditional_import_node(import_node: Node) -> bool:
    # An import nested under an if/try (click's platform-conditional
    # `if WIN: from ._winconsole import X ... else: def X(...)`) binds its
    # name only on some runtime paths; a same-named local def is then a
    # mutually-exclusive fallback variant, not shadowed dead code.
    current = import_node.parent
    while current is not None:
        if current.type in (cs.TS_PY_IF_STATEMENT, cs.TS_PY_TRY_STATEMENT):
            return True
        current = current.parent
    return False


def _rust_norm_manifest_path(path: str) -> str:
    # Cargo normalises manifest paths (a ./ prefix, backslashes); the
    # matcher compares against repo-relative posix form, so mirror it.
    return posixpath.normpath(path.replace("\\", cs.SEPARATOR_SLASH))


class ImportProcessor:
    __slots__ = (
        "repo_path",
        "project_name",
        "ingestor",
        "function_registry",
        "exclude_paths",
        "unignore_paths",
        "import_mapping",
        "commonjs_direct_exports",
        "conditional_imports",
        "php_function_imports",
        "js_ts_bare_imports",
        "js_path_aliases",
        "stdlib_extractor",
        "_is_local_module_cached",
        "_is_local_java_import_cached",
        "_java_source_root_prefix_cached",
        "_project_named_package",
        "_map_py_source_root",
        "_map_go_import_path",
        "_map_js_workspace_import",
        "_cpp_module_qn_map",
        "_cpp_qn_to_rel",
        "_deferred_import_edges",
        "_cpp_declaration_mappings",
        "_rust_dir_listing",
        "_rust_entry_mod_decls",
        "_rust_module_mod_decls",
        "_rust_redirect_parents",
        "_rust_explicit_targets",
        "_rust_auto_build_flags",
        "_rust_auto_discovery_flags",
        "_rust_workspace_crates",
        "_rust_pkg_deps",
        "_rust_inline_scope_keys",
        "_rust_pending_fn_scope_uses",
        "rust_fn_scope_imports",
        "rust_fn_scope_mod_imports",
        "rust_block_items",
        "rust_block_item_qns",
        "rust_block_scope_imports",
        "rust_self_module_imports",
        "_rust_fn_scope_keys",
        "_rust_pending_mod_scope_uses",
        "_rust_mod_scope_registry",
        "_rust_mod_scope_shadows",
    )

    def __init__(
        self,
        repo_path: Path,
        project_name: str,
        ingestor: IngestorProtocol | None = None,
        function_registry: FunctionRegistryTrieProtocol | None = None,
        exclude_paths: frozenset[str] | None = None,
        unignore_paths: frozenset[str] | None = None,
    ) -> None:
        self.repo_path = repo_path
        self.project_name = project_name
        self.ingestor = ingestor
        self.function_registry = function_registry
        # The same sets the indexer walks with, so the redirect sweep sees
        # exactly the files the graph holds (issue #1088).
        self.exclude_paths = exclude_paths
        self.unignore_paths = unignore_paths
        self.import_mapping: dict[str, dict[str, str]] = {}
        # CommonJS modules whose ENTIRE export is one function
        # (`module.exports = function (...) {...}`): module qn -> the
        # exported function's qn, so a whole-module require alias called
        # directly (`const f = require('./m'); f(x)`) resolves to it.
        self.commonjs_direct_exports: dict[str, str] = {}
        # Names bound by a CONDITIONAL Python import (nested under if/try --
        # click's `if WIN: from ._winconsole import X`): the dead-code fan-out
        # treats a same-named local def as the mutually-exclusive fallback
        # variant ONLY for these; an unconditional import is plain shadowing.
        self.conditional_imports: dict[str, set[str]] = {}
        # Lazy: replayed walk of every eligible repo file, built on the first C++
        # include so non-C++ projects never pay for it.
        self._cpp_module_qn_map: dict[str, str] | None = None
        self._cpp_qn_to_rel: dict[str, str] = {}
        # IMPORTS edges held back until every file is parsed, so internal
        # targets verify against the full module registry (issue #652).
        self._deferred_import_edges: list[DeferredImportEdge] = []
        # Exact-case directory listings and entry-file `mod` declarations for
        # Rust path rewriting (issue #1007); cleared per run by
        # reset_rust_path_caches so watch re-runs re-observe the filesystem.
        self._rust_dir_listing: dict[str, frozenset[str]] = {}
        self._rust_entry_mod_decls: dict[
            tuple[str, ...], dict[str, RustEntryDecls]
        ] = {}
        # Same, for an ORDINARY module file: its own declarations, plus the
        # directory its `#[path]` targets count from. Kept apart from the
        # entry map, whose every stem is a crate root candidate (issue #1065).
        self._rust_module_mod_decls: dict[
            tuple[tuple[str, ...], bool, bool],
            tuple[RustEntryDecls, list[str]] | None,
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
        # Inline-mod import scopes minted per file (file qn -> effective qns),
        # so a watch-mode re-parse of the file drops its stale sub-scopes.
        self._rust_inline_scope_keys: dict[str, set[str]] = {}
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
        # Import-map entries registered by C++20 module DECLARATIONS (`module X;`,
        # `export module X;`, `import :partition;`). They exist for name resolution
        # only; a declaration is not an import, so no IMPORTS edge is emitted.
        self._cpp_declaration_mappings: set[tuple[str, str]] = set()
        # Local names brought in by a PHP `use function A\B\c` import, keyed by
        # module. A PHP namespace path never matches cgr's file-path qn (a global
        # helper declares `namespace Illuminate\Support` from
        # Collections/functions.php), so these must resolve by simple name via the
        # trie rather than being judged external-import and suppressed.
        self.php_function_imports: dict[str, set[str]] = {}
        # Local names brought in by a JS/TS import with a NON-STANDARD scheme
        # (`ext:deno_node/y`; see _has_aliased_scheme), keyed by module. Such a
        # specifier aliases first-party code but does not resolve to a file-path
        # module qn, so the target is unregistered and would be judged external,
        # dropping the call. These names defer to the simple-name trie instead of
        # being suppressed. Ordinary package specifiers (bare, scoped, node:/npm:)
        # are excluded, so genuine external calls stay suppressed.
        self.js_ts_bare_imports: dict[str, set[str]] = {}
        # tsconfig `paths` aliases (match_prefix, target_prefix, is_wildcard), parsed
        # once from the repo-root tsconfig so `@/util` imports resolve to the real
        # first-party module instead of being dropped as external.
        self.js_path_aliases: list[tuple[str, str, bool]] = _load_ts_path_aliases(
            repo_path
        )
        self.stdlib_extractor = StdlibExtractor(
            function_registry, repo_path, project_name
        )

        repo_is_package = (repo_path / cs.INIT_PY).is_file()

        # A repo whose top-level package shares the repo's name (a django
        # clone is django/django, celery is celery/celery) makes written
        # absolute imports collide with the project prefix: `django.http`
        # names the package dir and needs the project prefix on top
        # (django.django.http), while in a flat layout (repo root doubles
        # as the installed package) the written path already IS the qn.
        self._project_named_package = (
            not repo_is_package and (repo_path / project_name).is_dir()
        )

        # Python packages under nested source roots (src-layout, monorepo packages,
        # pyproject package-dir remaps) are importable by a name that differs from
        # their repo-relative path, so absolute imports of them cannot resolve by the
        # import-name == path assumption. Discover the name -> dotted-path map once
        # so those imports resolve first-party.
        py_source_roots = discover_python_source_roots(repo_path)

        @lru_cache(maxsize=4096)
        def _map_py_source_root_cached(module_name: str) -> str | None:
            return resolve_via_source_roots(repo_path, py_source_roots, module_name)

        self._map_py_source_root = _map_py_source_root_cached

        # Go import paths are module-path-prefixed (github.com/acme/tool/pkg), never
        # repo-relative, so no local Go import resolves by the name == path
        # assumption. Map each go.mod module directive to its directory once so local
        # imports rewrite to project-prefixed qns and unmapped (external) paths stay
        # recognisably slash-separated.
        go_module_paths = discover_go_module_paths(repo_path)

        @lru_cache(maxsize=4096)
        def _map_go_import_path_cached(import_path: str) -> str | None:
            return resolve_go_import_path(go_module_paths, import_path)

        self._map_go_import_path = _map_go_import_path_cached

        # A JS/TS monorepo imports its own packages by manifest NAME
        # (`@acme/sdk/admin`), which no relative-path arithmetic resolves, so
        # map every first-party package.json name to its directory once, the
        # same way go.mod module directives are mapped above (issue #945).
        js_workspace_packages = discover_js_workspace_packages(repo_path)

        @lru_cache(maxsize=4096)
        def _map_js_workspace_import_cached(
            import_path: str, require: bool = False
        ) -> str | None:
            return resolve_js_workspace_import(
                js_workspace_packages, import_path, repo_path, require
            )

        self._map_js_workspace_import = _map_js_workspace_import_cached

        @lru_cache(maxsize=4096)
        def _is_local_module_cached(module_name: str) -> bool:
            # When the repo root is itself a package, its children are importable
            # only under the package name (project_name.child), never as bare
            # top-level names, so a bare top-level import resolves externally.
            if repo_is_package:
                return module_name == project_name
            return (
                (repo_path / module_name).is_dir()
                or (repo_path / f"{module_name}{cs.EXT_PY}").is_file()
                or (repo_path / module_name / cs.INIT_PY).is_file()
            )

        @lru_cache(maxsize=4096)
        def _java_source_root_prefix_cached(import_path: str) -> str | None:
            # The registered Module qns carry the build-tool source root
            # (src.main.java.), so a local import's qn must too; a flat
            # layout keeps the empty prefix and is unchanged (issue #1121).
            # Resolution probes the COMPLETE import target under each root:
            # an external import sharing a local top-level segment
            # (com.fasterxml under a repo with src/main/java/com) stays
            # external, and a test-only class binds to src/test/java even
            # when src/main/java also contains the top-level package. The
            # target may be a package dir or any class-file ancestor: a
            # static member (Utility.run) sits one segment past its file and
            # a nested-class member (Outer.Inner.CONSTANT) two or more, so
            # every ancestor is probed.
            parts = import_path.split(cs.SEPARATOR_DOT)
            for root in ((), *cs.JAVA_MAVEN_SOURCE_ROOTS):
                base = repo_path.joinpath(*root)
                if base.joinpath(*parts).is_dir() or any(
                    base.joinpath(
                        *parts[: end - 1], f"{parts[end - 1]}{cs.EXT_JAVA}"
                    ).is_file()
                    for end in range(len(parts), 0, -1)
                ):
                    return (
                        cs.SEPARATOR_DOT.join(root) + cs.SEPARATOR_DOT if root else ""
                    )
            return None

        def _is_local_java_import_cached(import_path: str) -> bool:
            return _java_source_root_prefix_cached(import_path) is not None

        self._is_local_module_cached = _is_local_module_cached
        self._is_local_java_import_cached = _is_local_java_import_cached
        self._java_source_root_prefix_cached = _java_source_root_prefix_cached

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
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        pre_captures: dict | None = None,
    ) -> None:
        if language not in queries:
            return
        imports_query = queries[language]["imports"]
        if not imports_query:
            return

        lang_config = queries[language]["config"]

        self.import_mapping[module_qn] = {}
        # A re-parsed module that no longer directly exports one function
        # must not leave the stale whole-module alias mapping behind.
        self.commonjs_direct_exports.pop(module_qn, None)
        # Reset per-module PHP use-function state too, so a re-index that drops a
        # `use function` import does not leave a stale exemption behind.
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
                case cs.SupportedLanguage.CSHARP:
                    self._parse_csharp_imports(captures, module_qn)
                case cs.SupportedLanguage.DART:
                    self._parse_dart_imports(captures, module_qn)
                case _:
                    self._parse_generic_imports(captures, module_qn, lang_config)

            logger.debug(
                ls.IMP_PARSED_COUNT,
                count=len(self.import_mapping[module_qn]),
                module=module_qn,
            )

            if self.ingestor:
                # Hold the edges back: an internal target is only real if some file
                # yields that module qn, known only after every file is parsed
                # (flush_deferred_import_edges).
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

    def defer_import_edge(
        self, module_qn: str, full_name: str, language: cs.SupportedLanguage
    ) -> None:
        # Entry point for import shapes discovered outside parse_imports (the
        # CommonJS destructuring fallback); every IMPORTS edge goes through the same
        # deferred verification.
        self._deferred_import_edges.append(
            DeferredImportEdge(
                module_qn=module_qn, full_name=full_name, language=language
            )
        )

    def flush_deferred_import_edges(self, known_module_paths: dict[str, str]) -> int:
        """Emit IMPORTS edges now that every file is parsed.

        An external target gets its ExternalModule node as before. An internal
        target must verify against the real module qns (with their file paths
        for language tie-breaking); a guess that resolves nowhere (a broken
        import, a directory with no index module, a crate path resolved from
        the wrong root) emits no edge, because the phantom endpoint is
        silently dropped by the database anyway.
        """
        # Rust sub-scope maps normally commit earlier (run() finalises
        # before the deferred inheritance pass reads them); flushing
        # drains any pendings a direct caller queued without that pass.
        self.finalise_rust_mod_scope_uses(known_module_paths)
        deferred = self._deferred_import_edges
        if not deferred or self.ingestor is None:
            return 0
        self._deferred_import_edges = []
        known_module_qns = set(known_module_paths)
        module_aliases = self._module_alias_map(known_module_qns)
        emitted = 0
        for entry in deferred:
            # `from pkg.transport import TTransport` is ambiguous in
            # Python: TTransport may be an item OR a submodule. The stdlib
            # extractor strips it as an item, anchoring the edge at the
            # package; when the FULL dotted name verifies as a real module,
            # the submodule is the true target.
            if entry.language == cs.SupportedLanguage.PYTHON and (
                full_target := self._verify_internal_import_target(
                    entry.full_name, known_module_paths, module_aliases, entry.language
                )
            ):
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn),
                    cs.RelationshipType.IMPORTS,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, full_target),
                )
                emitted += 1
                continue
            if entry.language == cs.SupportedLanguage.RUST and (
                entry.full_name == self.project_name
                or entry.full_name.startswith(f"{self.project_name}{cs.SEPARATOR_DOT}")
            ):
                # A crate::/super::/self:: use path was rewritten to a project
                # qn at parse time. The module target is the longest prefix
                # that verifies: the full path for a module import, minus the
                # item for item imports, further for enum variants and
                # associated items (use crate::color::Color::Red). Never
                # externalise a local path (the phantom ExternalModule would
                # orphan; issue #1007).
                candidate = entry.full_name
                target = None
                while True:
                    target = self._verify_internal_import_target(
                        candidate, known_module_paths, module_aliases, entry.language
                    )
                    if target is not None:
                        break
                    trimmed = candidate.rsplit(cs.SEPARATOR_DOT, 1)[0]
                    if trimmed in (candidate, self.project_name):
                        break
                    candidate = trimmed
                if target == entry.module_qn:
                    # `use super::*` in an inline mod resolves to the file's
                    # own module; a self-import edge is meaningless.
                    continue
                if target is None:
                    logger.debug(
                        ls.IMP_DROPPED_PHANTOM_TARGET,
                        from_module=entry.module_qn,
                        to_module=entry.full_name,
                    )
                    continue
                self.ingestor.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, entry.module_qn),
                    cs.RelationshipType.IMPORTS,
                    (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, target),
                )
                emitted += 1
                continue
            module_path = self._resolve_module_path(entry.full_name, entry.language)
            target_label = self._module_label(module_path)
            if target_label == cs.NodeLabel.EXTERNAL_MODULE:
                # An external import target has no file pass to create its
                # node; without one here the IMPORTS edge MERGEs against
                # nothing and is silently dropped (issue #652).
                self._ensure_external_module_node(module_path, entry.full_name)
            else:
                verified = self._verify_internal_import_target(
                    module_path, known_module_paths, module_aliases, entry.language
                )
                if verified is None and entry.language == cs.SupportedLanguage.PYTHON:
                    # A package-anchored guess that names no sibling module is an
                    # ABSOLUTE import in Python semantics (`import sys` inside a
                    # package); re-resolve it as one.
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
        # A module reached through its container's name: pkg/__init__.py,
        # shared/index.js, utils/mod.rs. Importers write the container qn; the real
        # Module node lives at the index-file leaf.
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
        known_module_paths: dict[str, str],
        module_aliases: dict[str, str],
        language: cs.SupportedLanguage,
    ) -> str | None:
        if module_path in known_module_paths:
            return module_path
        if alias := module_aliases.get(module_path):
            return alias
        # A path resolved from the wrong root (`use crate::utils` written outside
        # src/) still names a unique real module; a whole-segment suffix match
        # recovers it. Ambiguity means no edge, not a guess.
        prefix = f"{self.project_name}{cs.SEPARATOR_DOT}"
        if not module_path.startswith(prefix):
            return None
        tail = module_path[len(prefix) :]
        if not tail:
            return None
        suffix = f"{cs.SEPARATOR_DOT}{tail}"
        matches = {qn for qn in known_module_paths if qn.endswith(suffix)}
        matches.update(
            real for base, real in module_aliases.items() if base.endswith(suffix)
        )
        if len(matches) > 1:
            # A polyglot repo can hold same-named modules in several
            # languages (thrift: src/protocol/__init__.py vs
            # src/ext/protocol.h); the importing language can only target
            # its own modules, so tie-break by file extension. Inline
            # modules (no file) stay eligible.
            matches = {
                qn
                for qn in matches
                if self._path_matches_language(known_module_paths.get(qn, ""), language)
            }
        if len(matches) == 1:
            return matches.pop()
        return None

    @staticmethod
    def _path_matches_language(path: str, language: cs.SupportedLanguage) -> bool:
        if not path:
            return True
        spec = LANGUAGE_SPECS.get(language)
        if spec is None:
            return True
        return path.endswith(tuple(spec.file_extensions))

    def _parse_python_imports(self, captures: dict, module_qn: str) -> None:
        all_imports = captures.get(cs.CAPTURE_IMPORT, []) + captures.get(
            cs.CAPTURE_IMPORT_FROM, []
        )
        for import_node in all_imports:
            before = set(self.import_mapping[module_qn])
            if import_node.type == cs.TS_PY_IMPORT_STATEMENT:
                self._handle_python_import_statement(import_node, module_qn)
            elif import_node.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                self._handle_python_import_from_statement(import_node, module_qn)
            if _is_conditional_import_node(import_node):
                new_names = set(self.import_mapping[module_qn]) - before
                if new_names:
                    self.conditional_imports.setdefault(module_qn, set()).update(
                        new_names
                    )

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
            if self._project_named_package:
                return f"{self.project_name}{cs.SEPARATOR_DOT}{module_name}"
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
        prefix = self._java_source_root_prefix_cached(import_path)
        if prefix is not None:
            return f"{self.project_name}{cs.SEPARATOR_DOT}{prefix}{import_path}"
        return import_path

    def _java_owning_module_qn(self, full_name: str) -> str:
        # A static or nested-class import carries symbol segments past the
        # class file (Utility.run, Outer.Inner.CONSTANT). The import map keeps
        # the full path for call resolution, but the IMPORTS edge must land on
        # the owning file-level Module, so truncate to the deepest ancestor
        # whose .java file exists. Package (wildcard) and file-level targets
        # come back unchanged.
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        segments = full_name[len(project_prefix) :].split(cs.SEPARATOR_DOT)
        for end in range(len(segments), 0, -1):
            candidate = segments[:end]
            if self.repo_path.joinpath(*candidate).is_dir():
                break
            if self.repo_path.joinpath(
                *candidate[:-1], f"{candidate[-1]}{cs.EXT_JAVA}"
            ).is_file():
                return project_prefix + cs.SEPARATOR_DOT.join(candidate)
        return full_name

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
        filename = rel_parts[-1]
        dot = filename.rfind(cs.SEPARATOR_DOT)
        suffix = filename[dot:] if dot != -1 else ""
        return not should_skip_rel_file(
            cs.SEPARATOR_SLASH.join(rel_parts),
            tuple(rel_parts[:-1]),
            suffix,
            exclude_paths=self.exclude_paths,
            unignore_paths=self.unignore_paths,
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
        if not dir_parts:
            return True, True
        if dir_parts[-1] == cs.LANG_SRC_DIR:
            pkg_parts = tuple(dir_parts[:-1])
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                return (
                    self._rust_auto_kind_enabled(pkg_parts, cs.RS_MANIFEST_AUTOLIB_KEY),
                    self._rust_auto_kind_enabled(
                        pkg_parts, cs.RS_MANIFEST_AUTOBINS_KEY
                    ),
                )
            return True, True
        if (
            len(dir_parts) >= 2
            and dir_parts[-1] == cs.RS_BIN_DIR
            and dir_parts[-2] == cs.LANG_SRC_DIR
        ):
            # Every .rs directly in src/bin — main.rs and lib.rs alike — is
            # a bin auto target, so both entry stems follow autobins.
            pkg_parts = tuple(dir_parts[:-2])
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                enabled = self._rust_auto_kind_enabled(
                    pkg_parts, cs.RS_MANIFEST_AUTOBINS_KEY
                )
                return enabled, enabled
            return True, True
        if dir_parts[-1] in cs.RS_AUTO_TARGET_DIRS:
            # Every .rs directly in a kind dir is that kind's auto target,
            # so both entry stems follow the kind's flag.
            pkg_parts = tuple(dir_parts[:-1])
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                enabled = self._rust_auto_kind_enabled(
                    pkg_parts, cs.RS_AUTO_DIR_KEYS[dir_parts[-1]]
                )
                return enabled, enabled
            return True, True
        if (
            len(dir_parts) >= 3
            and dir_parts[-2] == cs.RS_BIN_DIR
            and dir_parts[-3] == cs.LANG_SRC_DIR
        ):
            # A multi-file target compiles <name>/main.rs; a lib.rs there is
            # never a cargo target, so the lib flag is off in nested dirs.
            pkg_parts = tuple(dir_parts[:-3])
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                return False, self._rust_auto_kind_enabled(
                    pkg_parts, cs.RS_MANIFEST_AUTOBINS_KEY
                )
            return True, True
        if len(dir_parts) >= 2 and dir_parts[-2] in cs.RS_AUTO_TARGET_DIRS:
            pkg_parts = tuple(dir_parts[:-2])
            if cs.PKG_CARGO_TOML in self._rust_dir_entries(
                self.repo_path.joinpath(*pkg_parts)
            ):
                return False, self._rust_auto_kind_enabled(
                    pkg_parts, cs.RS_AUTO_DIR_KEYS[dir_parts[-2]]
                )
        return True, True

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
            entries = manifest.get(section)
            if isinstance(entries, dict):
                entries = [entries]
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if isinstance(path := entry.get(cs.RS_MANIFEST_PATH_KEY), str):
                    paths.add(_rust_norm_manifest_path(path))
                elif default := self._rust_default_target_path(
                    pkg_parts, section, entry, manifest
                ):
                    # A PATHLESS table is still an explicit target at its
                    # conventional location ([lib] means src/lib.rs), and an
                    # explicit target must survive its kind's auto-discovery
                    # opt-out (issue #1030 review).
                    paths.add(default)
        # `[package] build = "..."` overrides the build script location;
        # the named file is a crate root like any explicit target.
        package = manifest.get(cs.RS_MANIFEST_PACKAGE_KEY)
        if isinstance(package, dict) and isinstance(
            build := package.get(cs.RS_MANIFEST_BUILD_KEY), str
        ):
            paths.add(_rust_norm_manifest_path(build))
        build_value = (
            package.get(cs.RS_MANIFEST_BUILD_KEY) if isinstance(package, dict) else None
        )
        # Unset AND `build = true` both mean auto-detection
        # (cargo-verified: `build = true` compiles build.rs exactly like
        # unset); a string names the script explicitly, false disables.
        self._rust_auto_build_flags[pkg_parts] = (
            build_value is None or build_value is True
        )
        self._rust_auto_discovery_flags[pkg_parts] = {
            key: False
            for key in cs.RS_MANIFEST_AUTO_KEYS
            if isinstance(package, dict) and package.get(key) is False
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
            try:
                matches = sorted(self.repo_path.glob(pattern))
            except (ValueError, NotImplementedError):
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
            parts = _rust_norm_manifest_path(path).split(cs.SEPARATOR_SLASH)
            if parts[-1].endswith(cs.EXT_RS):
                return (*rel, *parts[:-1]), parts[-1][: -len(cs.EXT_RS)]
        # A pathless [lib] table is still an explicit target at the default
        # src/lib.rs; only a genuinely undeclared library respects the
        # autolib opt-out (issue #1030 review).
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
        tables = [manifest]
        target = manifest.get(cs.RS_MANIFEST_TARGET_TABLE_KEY)
        if isinstance(target, dict):
            tables.extend(t for t in target.values() if isinstance(t, dict))
        deps: dict[str, tuple[str, ...] | None] = {}
        for table in tables:
            self._rust_collect_dep_table(pkg_parts, table, deps)
        self._rust_pkg_deps[pkg_parts] = deps
        return deps

    def _rust_collect_dep_table(
        self,
        pkg_parts: tuple[str, ...],
        table: dict,
        deps: dict[str, tuple[str, ...] | None],
    ) -> None:
        for section in cs.RS_MANIFEST_DEP_SECTIONS:
            entries = table.get(section)
            if not isinstance(entries, dict):
                continue
            for key, value in entries.items():
                name = key.replace(cs.CHAR_HYPHEN, cs.CHAR_UNDERSCORE)
                resolved = self._rust_dep_target_dir(pkg_parts, key, value)
                # A path target from any section wins over a registry
                # spelling elsewhere (version + path in one entry is the
                # common published-workspace shape).
                if resolved is not None or name not in deps:
                    deps[name] = resolved

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
            workspace = self._rust_read_manifest(self.repo_path).get(
                cs.RS_MANIFEST_WORKSPACE_KEY
            )
            entry = (
                workspace.get(cs.RS_MANIFEST_DEP_SECTIONS[0], {}).get(key)
                if isinstance(workspace, dict)
                else None
            )
            if not isinstance(entry, dict):
                return None
            value = entry
            base = self.repo_path
        path = value.get(cs.RS_MANIFEST_PATH_KEY)
        if not isinstance(path, str):
            return None
        try:
            return (
                (base / _rust_norm_manifest_path(path))
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
        if (
            stem not in cs.RS_ENTRY_STEMS
            and f"{stem}{cs.EXT_RS}"
            in self._rust_dir_entries(self.repo_path.joinpath(*dir_parts))
        ):
            if self._rust_is_auto_target_dir(dir_parts, stem):
                return "file", qn_parts
            if self._rust_is_explicit_target(dir_parts, stem):
                # An explicit manifest target is a crate root like any
                # entry file: its `mod` declarations resolve in the
                # CONTAINING directory (the tests/common.rs idiom,
                # cargo-verified), so it attaches classically with
                # itself as the definitive entry stem.
                return "entry", qn_parts
        if stem in cs.RS_ENTRY_STEMS and f"{stem}{cs.EXT_RS}" in self._rust_dir_entries(
            self.repo_path.joinpath(*dir_parts)
        ):
            # An entry-stem FILE that is a target in its own right — by
            # auto location (src/bin/main.rs beside src/bin/mod.rs) or by
            # explicit manifest path — keeps its own crate: its qn carries
            # the stem only when a sibling claimed the dir qn, and the
            # ancestor mod.rs check below must not swallow it
            # (issue #1031 review).
            if self._rust_is_auto_target_dir(dir_parts, stem):
                return "file", qn_parts
            if self._rust_is_explicit_target(dir_parts, stem):
                return "entry", qn_parts
        if self._rust_is_mod_rs_target(qn_parts):
            # Cargo compiles src/bin/mod.rs (or an explicit target whose
            # path ends in mod.rs) as a target named `mod` whose crate root
            # is the file itself. The mod.rs spelling maps the module to its
            # DIRECTORY qn, so that qn is the file root and `mod x;` inside
            # it resolves beside it — file-root nesting whose declarations
            # come from mod.rs, not a sibling `<dir>.rs` shadow
            # (issue #1031).
            return "dir_file", qn_parts
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
                return stem, True
        return "lib", False

    def _rust_entry_decls(self, dir_parts: list[str]) -> dict[str, RustEntryDecls]:
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
            top_level = _rs_top_level_only(_rs_strip_comments_and_strings(source))
            decls[stem] = _rs_entry_decls_of(top_level)
        return decls

    def _rust_module_decls(
        self,
        module_parts: list[str],
        dir_backed: bool = False,
        want_mods: bool = False,
    ) -> tuple[RustEntryDecls, list[str]] | None:
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
        if not want_mods and _RS_PATH_ATTRIBUTE_PATTERN.search(source) is None:
            # Only redirects are read here, and both the lexer and the
            # declaration scan behind them cost far more than this prescan
            # (the scan is superlinear in file length, so a 30k-line
            # generated module spends tens of seconds proving it declares no
            # redirect). Searching the RAW source overshoots by the
            # commented-out and quoted spellings, which merely fall through
            # to the real scan; nothing it misses can be a redirect.
            found = (RustEntryDecls(set(), set(), {}), list(base))
        else:
            found = (
                _rs_entry_decls_of(
                    _rs_top_level_only(_rs_strip_comments_and_strings(source))
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
                    name, dir_prefix, self.exclude_paths, self.unignore_paths
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
                cs.EXT_RS,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
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
        if _RS_PATH_ATTRIBUTE_PATTERN.search(source) is None:
            return []
        # A mod.rs IS its directory's module, so it contributes no segment of
        # its own; every other file contributes its stem.
        declarer = cs.SEPARATOR_DOT.join(
            [self.project_name, *dir_parts]
            if path.stem == cs.INDEX_MOD
            else [self.project_name, *dir_parts, path.stem]
        )
        decls = _rs_entry_decls_of(
            _rs_top_level_only(_rs_strip_comments_and_strings(source))
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

    def _rust_super_base(self, module_qn: str, depth: int) -> str:
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
    ) -> list[str]:
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
            target = (
                rs_utils.path_attribute_qn_parts(base, redirect) if redirect else None
            )
            if redirect is None or target is None:
                out, dir_backed = [*out, segment], False
                continue
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
        return cs.SEPARATOR_DOT.join(
            [self.project_name, *self._rust_walk_mods(parts, rest, dir_backed)]
        )

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
        if rest and chosen is not None:
            file_mods, items = chosen.mods, chosen.items
            if redirect := chosen.redirects.get(rest[0]):
                # The declaration names its backing file outright, and the
                # module keys under that file, so the declared name never
                # appears in the qn at all (issue #1035).
                if target := rs_utils.path_attribute_qn_parts(dir_parts, redirect):
                    return self._rust_join_mods(
                        target,
                        rest[1:],
                        redirect.rsplit(cs.SEPARATOR_SLASH, 1)[-1] == cs.MOD_RS,
                    )
            if rest[0] in file_mods:
                return self._rust_join_mods([*dir_parts, rest[0]], rest[1:])
            if rest[0] in items:
                return cs.SEPARATOR_DOT.join(
                    [self.project_name, *dir_parts, stem, *rest]
                )
        if rest and (
            f"{rest[0]}{cs.EXT_RS}" in entries
            or (rest[0] in entries and (directory / rest[0]).is_dir())
        ):
            return self._rust_join_mods([*dir_parts, rest[0]], rest[1:])
        if rest and not definitive and chosen is not None:
            # When a file compiles into BOTH crates (lib.rs and main.rs each
            # declare its module), the path can only mean the entry that
            # DECLARES the item; the chosen entry declaring it returned above.
            for other, other_decls in self._rust_entry_decls(dir_parts).items():
                if other != stem and rest[0] in other_decls.items:
                    stem = other
                    break
        return cs.SEPARATOR_DOT.join([self.project_name, *dir_parts, stem, *rest])

    def _rust_resolve_relative(
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
        if walked == [*parts, *rest]:
            # No redirect fired, so keep base_qn verbatim rather than
            # rebuilding it from project_name, which re-splits differently
            # when the repository directory name itself contains a dot.
            return cs.SEPARATOR_DOT.join([base_qn, *rest])
        return cs.SEPARATOR_DOT.join([self.project_name, *walked])

    def _rewrite_rust_local_use_path(
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
        rustc binds the local module first (issue #1033). External paths
        (std::fmt) pass through unchanged.
        """
        parts = full_path.split(cs.SEPARATOR_DOUBLE_COLON)
        head = parts[0]
        if head == cs.RUST_CRATE_KEYWORD:
            return self._rust_rewrite_crate_path(parts[1:], module_qn)
        if head == cs.KEYWORD_SELF:
            return self._rust_resolve_relative(module_qn, parts[1:], module_qn)
        if head == cs.KEYWORD_SUPER:
            depth = 0
            while depth < len(parts) and parts[depth] == cs.KEYWORD_SUPER:
                depth += 1
            base = self._rust_super_base(module_qn, depth)
            return self._rust_resolve_relative(base, parts[depth:], module_qn)
        if (
            head not in local_mods
            and (root := self._rust_workspace_crate_roots().get(head)) is not None
            and self._rust_member_rewrite_allowed(root[0], module_qn)
        ):
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

    def _module_label(self, module_path: str) -> cs.NodeLabel:
        # #498: import targets outside the project prefix live under the
        # dedicated ExternalModule label (mirroring Package/ExternalPackage).
        if module_path == self.project_name or module_path.startswith(
            self.project_name + cs.SEPARATOR_DOT
        ):
            return cs.NodeLabel.MODULE
        return cs.NodeLabel.EXTERNAL_MODULE

    def ensure_external_module_node(self, module_path: str) -> None:
        # Public entry for non-import callers (deferred inheritance): an external
        # base keeps its edge by targeting the same ExternalModule node an import of
        # it would mint.
        self._ensure_external_module_node(module_path, module_path)

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

    def _resolve_rust_import_path(self, import_path: str) -> str:
        # Local (crate::/super::/self::) paths never reach this point: they
        # are rewritten to project qns at parse time and short-circuited in
        # flush_deferred_import_edges. Only external paths (std::fmt) remain.
        parts = import_path.split(cs.SEPARATOR_DOUBLE_COLON)
        module_path = (
            cs.SEPARATOR_DOUBLE_COLON.join(parts[:-1]) if len(parts) > 1 else parts[0]
        )

        self._ensure_external_module_node(module_path, import_path)
        return module_path

    def _resolve_module_path(
        self,
        full_name: str,
        language: cs.SupportedLanguage,
    ) -> str:
        project_prefix = self.project_name + cs.SEPARATOR_DOT
        match language:
            # Java MODULE semantics: internal imports point to file-level MODULE
            # nodes (project.utils.StringUtils) because Java files are named after
            # their primary class. External imports point to package-level
            # (java.util) because we lack source code for file-level nodes. This
            # asymmetry is intentional.
            case cs.SupportedLanguage.JAVA:
                if full_name.startswith(project_prefix):
                    return self._java_owning_module_qn(full_name)
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                if self._is_local_js_import(full_name):
                    return self._resolve_js_internal_module(full_name)
            case cs.SupportedLanguage.RUST:
                return self._resolve_rust_import_path(full_name)

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

        self._register_python_from_imports(
            module_qn, module_name, imported_items, is_wildcard
        )

    def _extract_python_from_module_name(
        self, import_node: Node, module_qn: str
    ) -> str | None:
        module_name_node = import_node.child_by_field_name(cs.FIELD_MODULE_NAME)
        if not module_name_node:
            return None

        if module_name_node.type == cs.TS_DOTTED_NAME:
            # A written absolute path resolves through the same collision-aware
            # mapping as plain imports; a relative import is already project-prefixed
            # by construction and must NOT re-enter it (a second prefixing would
            # double the project segment).
            if written := safe_decode_text(module_name_node):
                return self._resolve_python_base_module(written)
            return None
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
        # The old `startswith(project_name)` as-is shortcut is subsumed by
        # _resolve_import_full_name's first branch, which also handles the
        # project-named-package collision.
        top_level = module_name.split(cs.SEPARATOR_DOT, maxsplit=1)[0]
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
        # Relative imports are always internal; resolve to the full project-prefixed
        # qn so resolution does not depend on bare-name locality checks (which treat
        # package children as external).
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

        # A package's qn already IS the package, so `from .` inside an __init__.py
        # drops one fewer level than inside a regular module.
        drop = dots - 1 if self._is_package_qn(module_qn) else dots
        keep = max(len(module_parts) - drop, 0)
        target_parts = module_parts[:keep]

        if module_name:
            target_parts.extend(module_name.split(cs.SEPARATOR_DOT))

        # A relative climb that lands at the project root (`from . import x` in a
        # top-level module) leaves no parts; resolve it to the project root so the
        # import is not silently dropped.
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

            elif import_node.type in (
                cs.TS_LEXICAL_DECLARATION,
                cs.TS_VARIABLE_DECLARATION,
            ):
                self._parse_js_require(import_node, module_qn)

            elif import_node.type == cs.TS_EXPORT_STATEMENT:
                self._parse_js_reexport(import_node, module_qn)

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
            # normpath collapses `.`/`..` so the qn is clean and an escaping alias
            # (`../x`) is rejected below.
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
        # An ESM RELATIVE specifier may carry an explicit extension
        # (`./b.js`); the module qn never does, so keeping it poisons the
        # import map AND the IMPORTS edge with a phantom `.js` segment
        # (issue #652). Bare package specifiers are exempt: a package can
        # legitimately be NAMED with the extension (p5.js, highlight.js).
        for ext in cs.JS_TS_ALL_EXTENSIONS:
            if import_path.endswith(ext) and len(import_path) > len(ext):
                return import_path[: -len(ext)]
        return import_path

    def _resolve_js_module_path(
        self, import_path: str, current_module: str, require: bool = False
    ) -> str:
        if not import_path.startswith(cs.PATH_CURRENT_DIR):
            if aliased := self._ts_alias_module_qn(import_path):
                return aliased
            if workspace := self._map_js_workspace_import(import_path, require):
                dotted = workspace.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            return import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)
        import_path = self._strip_js_extension(import_path)

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
            if arg is None:
                continue
            # A CommonJS `require()` reads a dual-package exports map from
            # the require side.
            resolved_module = self._resolve_js_module_path(
                safe_decode_with_fallback(arg).strip("'\""), current_module, True
            )
            if name_node.type == cs.TS_IDENTIFIER:
                # `const fs = require('fs')`: bind the whole module.
                var_name = safe_decode_with_fallback(name_node)
                self.import_mapping[current_module][var_name] = resolved_module
                logger.debug(ls.IMP_JS_REQUIRE, var=var_name, module=resolved_module)
            elif name_node.type == cs.TS_OBJECT_PATTERN:
                # `const { writeFileSync } = require('fs')` / `{ x: y }`: bind each
                # local to module.imported, mirroring how ESM named imports resolve.
                for local, imported in _js_destructured_names(name_node):
                    full = f"{resolved_module}{cs.SEPARATOR_DOT}{imported}"
                    self.import_mapping[current_module][local] = full
                    logger.debug(ls.IMP_JS_REQUIRE, var=local, module=full)

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

    def _parse_csharp_imports(self, captures: dict, module_qn: str) -> None:
        name_types = (cs.TS_CSHARP_QUALIFIED_NAME, cs.TS_CSHARP_IDENTIFIER)
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            if import_node.type != cs.TS_CSHARP_USING_DIRECTIVE:
                continue
            # `using Alias = Target;` marks the alias with a `name` field; the
            # imported path is then the OTHER name node. Plain/static/global
            # forms have no `name` field, so the sole name node is the path.
            alias_node = import_node.child_by_field_name(cs.TS_CSHARP_FIELD_NAME)
            target = None
            for child in import_node.children:
                if child.type in name_types and child != alias_node:
                    target = child
            if target is None:
                continue
            imported_path = safe_decode_with_fallback(target)
            if not imported_path:
                continue
            if alias_node is not None and alias_node.text:
                local_name = safe_decode_with_fallback(alias_node)
            else:
                local_name = imported_path.split(cs.SEPARATOR_DOT)[-1]
            self.import_mapping[module_qn][local_name] = imported_path
            logger.debug(ls.IMP_CSHARP, name=local_name, path=imported_path)

    def reset_java_path_caches(self) -> None:
        # Same contract as reset_rust_path_caches: the filesystem may have
        # gained or lost files since the layout decisions were cached.
        self._java_source_root_prefix_cached.cache_clear()

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
        try:
            dir_parts = directory.relative_to(self.repo_path).parts
        except ValueError:
            return
        if file_path.suffix == cs.EXT_RS:
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
                    self._rust_module_mod_decls.pop(
                        (module, dir_backed, want_mods), None
                    )
            # Whole-map, because the edit may have added or removed a
            # redirect naming any file at all, and a stale declarer keeps
            # `super::` in a file it no longer moves pointing at it.
            self._rust_redirect_parents = None
        if (
            file_path.name in (cs.LIB_RS, cs.MAIN_RS)
            or file_path.name in self._rust_explicit_entry_files(tuple(dir_parts))
            or (
                file_path.name == f"{cs.RS_BUILD_STEM}{cs.EXT_RS}"
                and cs.PKG_CARGO_TOML in self._rust_dir_entries(directory)
                and self._rust_has_auto_build(tuple(dir_parts))
            )
        ):
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
                    pass
                else:
                    top_level = _rs_top_level_only(
                        _rs_strip_comments_and_strings(source)
                    )
                    stems[file_path.stem] = _rs_entry_decls_of(top_level)
        if file_path.name == cs.PKG_CARGO_TOML:
            # A manifest edit can add, remove, or repoint explicit targets
            # anywhere in its package, and a CREATED manifest moves the
            # package boundary for every file below it, so the whole
            # target cache rebuilds. The entry-declaration map is DERIVED
            # from the target set: evict exactly the stems the fresh
            # manifests no longer back. lib/main keep their declarations
            # (and their storm protection) only while their kind's
            # discovery opt-out permits them — an edit flipping autolib or
            # autobins to false must leave the map exactly as a clean
            # index would (issue #1030 review).
            self._rust_explicit_targets.clear()
            self._rust_auto_build_flags.clear()
            self._rust_auto_discovery_flags.clear()
            self._rust_workspace_crates = None
            self._rust_pkg_deps.clear()
            for key, stems in self._rust_entry_mod_decls.items():
                allowed = {
                    name[: -len(cs.EXT_RS)]
                    for name in self._rust_explicit_entry_files(key)
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
        for key in self._rust_fn_scope_keys.pop(module_qn, ()):
            self.rust_fn_scope_imports.pop(key, None)
            self.rust_fn_scope_mod_imports.pop(key, None)
        for key in self._rust_inline_scope_keys.pop(module_qn, ()):
            self.import_mapping.pop(key, None)

    def _parse_rust_imports(self, captures: dict, module_qn: str) -> None:
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
            resolved = self._rewrite_rust_local_use_path(
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
            resolved_imports[imported_name] = resolved
            if sub_scope:
                # The generic deferral loop only reads the file-level map;
                # sub-scope imports still owe the file its IMPORTS edge.
                self.defer_import_edge(module_qn, resolved, cs.SupportedLanguage.RUST)
            logger.debug(ls.IMP_RUST, name=imported_name, path=resolved)
        if scope_node is not None:
            if scope_node.type == cs.TS_RS_FUNCTION_ITEM:
                self._rust_pending_fn_scope_uses.setdefault(module_qn, []).append(
                    (
                        scope_node.start_point[0] + 1,
                        scope_node.start_point[1],
                        resolved_imports,
                        False,
                    )
                )
            else:
                # A const/static initializer block: no qn scope, so the
                # entry is span-gated and answers only calls written
                # inside the block, with nested mod/fn/item-scope spans
                # recorded so inner scopes' own bindings keep precedence.
                mod_holes, fn_holes, item_scopes = rs_utils.rust_block_scope_holes(
                    scope_node
                )
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
            return
        if scope_parts is None:
            # No enclosing block could be attributed (defensive): no qn
            # scope corresponds to the use, and any key would serve a
            # real scope's readers. Keep only the IMPORTS edges.
            return
        if not scope_parts:
            self.import_mapping.setdefault(effective_qn, {}).update(resolved_imports)
            return
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
        if not pure_chain:
            # A fn-local mod can lose the shared-key arbitration to a pure
            # twin (issue #1017: one qn, two modules), yet its own functions
            # still see this use: fan it out to their spans so the weak
            # fn-scope map answers for them instead of whatever the key
            # ends up holding.
            for line, col in rs_utils.enclosing_mod_fn_spans(use_node):
                self._rust_pending_fn_scope_uses.setdefault(module_qn, []).append(
                    (line, col, resolved_imports, True)
                )

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
        for keys in self._rust_inline_scope_keys.values():
            for key in keys:
                if known_module_paths.get(key):
                    # The key has since become an indexed file's own module
                    # qn (a watch CREATE of the cfg twin's file form): the
                    # map is that file's parse output now, not this
                    # arbitration's to retract. The owner branch below
                    # keeps no inline writers for it, so the claim lapses.
                    continue
                self.import_mapping.pop(key, None)
        self._rust_inline_scope_keys = {}
        by_key: dict[str, list[tuple[str, bool, dict[str, str]]]] = {}
        for writer_qn, entries in self._rust_mod_scope_registry.items():
            for key, pure, imports in entries:
                by_key.setdefault(key, []).append((writer_qn, pure, imports))
        for key, writers in by_key.items():
            owner_path = known_module_paths.get(key)
            kept = writers
            if owner_path:
                kept = [
                    w for w in writers if known_module_paths.get(w[0]) == owner_path
                ]
            else:
                if any(w[1] for w in writers) and not all(w[1] for w in writers):
                    kept = [w for w in writers if w[1]]
                if len({w[0] for w in kept}) > 1:
                    kept = []
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
        is_dot_import = False

        for child in spec_node.children:
            if child.type == cs.TS_PACKAGE_IDENTIFIER:
                alias_name = safe_decode_with_fallback(child)
            elif child.type == cs.TS_GO_DOT:
                is_dot_import = True
            elif child.type == cs.TS_INTERPRETED_STRING_LITERAL:
                import_path = safe_decode_with_fallback(child).strip('"')

        if import_path:
            package_name = alias_name or import_path.split(cs.SEPARATOR_SLASH)[-1]
            # A path under a local go.mod module rewrites to the package dir's
            # project qn ('' remainder = the module root package itself), so both the
            # IMPORTS edge and call resolution bind first-party. External paths stay
            # raw.
            if (mapped := self._map_go_import_path(import_path)) is not None:
                import_path = (
                    f"{self.project_name}{cs.SEPARATOR_DOT}{mapped}"
                    if mapped
                    else self.project_name
                )
            if is_dot_import:
                # `import . "fmt"` binds the package's exported names, not the
                # package identifier; a `.`-prefixed sentinel key (no identifier
                # can contain a dot) lets bare-callee lookups re-qualify.
                self.import_mapping[module_qn][f"{cs.SEPARATOR_DOT}{package_name}"] = (
                    import_path
                )
            else:
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
                self.repo_path,
                self.project_name,
                self.exclude_paths,
                self.unignore_paths,
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
            # Prefer, deterministically, the header sharing the longest path prefix
            # with the includer (the same source tree). commonpath (not
            # commonprefix) so sibling dirs with a shared name prefix (src/ast vs
            # src/ast_new) rank by whole components.
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
                # The include resolves to a real repo file; use that file's
                # actual (collision-disambiguated) module qn. The old
                # project-rooted, extension-stripped guess produced phantom
                # module qns (self-imports for same-stem header/source pairs,
                # wrong roots for -I style includes), which poisoned both the
                # IMPORTS edges and class resolution via the import map
                # (issue #652).
                full_name = resolved
            else:
                # A quoted include matching no repo file is a third-party header; a
                # project-rooted qn would be a phantom.
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
            colon_pos = decl_text.find(cs.CHAR_COLON)
            if colon_pos != -1:
                if partition_part := decl_text[colon_pos + 1 :].split(";")[0].strip():
                    partition_name = f"{cs.CPP_PARTITION_PREFIX}{partition_part}"
                    full_name = f"{self.project_name}{cs.SEPARATOR_DOT}{partition_part}"
                    self.import_mapping[module_qn][partition_name] = full_name
                    # A partition lives inside the same named module; no graph node
                    # models it, so never emit an IMPORTS edge.
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
        # `module X;` / `export module X;` DECLARE this file's module; the mapping
        # exists for name resolution only, never as an IMPORTS edge.
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
        # `use function A\B\c` / `use const A\B\C` carry the modifier on the
        # declaration (older grammar) or inside each clause (current grammar).
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

    def _parse_dart_imports(self, captures: dict, module_qn: str) -> None:
        # Dart import/export/part directives carry a URI string. `dart:` and
        # `package:` targets are external (kept verbatim); relative paths and part
        # files resolve to a project-internal module qn. A `part of my.library;`
        # directive names a dotted library, not a file, so it has no URI and is
        # skipped.
        for import_node in captures.get(cs.CAPTURE_IMPORT, []):
            uri = dart_extract_uri(import_node)
            if not uri:
                continue
            if full_name := dart_resolve_import(uri, module_qn, self.project_name):
                self.import_mapping[module_qn][dart_local_name(uri)] = full_name

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
