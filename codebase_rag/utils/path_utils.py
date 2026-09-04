import hashlib
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from functools import lru_cache
from pathlib import Path

from pathspec import PathSpec

from .. import constants as cs

_PROJECT_NAME_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_PROJECT_NAME_FALLBACK_BASE = "repo"


def derive_project_name(repo_path: Path) -> str:
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[
        : cs.PROJECT_NAME_DIGEST_LEN
    ]
    base = _PROJECT_NAME_INVALID_CHARS.sub("_", resolved.name).strip("_")
    if not base:
        base = _PROJECT_NAME_FALLBACK_BASE
    return f"{base}{cs.PROJECT_NAME_DIGEST_MARKER}{digest}"


def resolve_repo_path(repo_path: str | None, target_default: str) -> Path:
    if repo_path:
        return Path(repo_path).resolve()
    if target_default and target_default != ".":
        return Path(target_default).resolve()
    return Path.cwd().resolve()


@lru_cache(maxsize=4096)
def cached_relative_path(file_path: Path, repo_path: Path) -> Path:
    return file_path.relative_to(repo_path)


@lru_cache(maxsize=4096)
def cached_resolve_posix(file_path: Path) -> str:
    return file_path.resolve().as_posix()


@lru_cache(maxsize=4096)
def cached_file_identity_posix(file_path: Path) -> str:
    """Absolute POSIX identity that survives the file's own deletion.

    ``resolve()`` dereferences a leaf symlink to its target, but once the link
    is deleted that target can no longer be recovered, so a node keyed on it can
    never be matched for deletion and leaks (GHSA-85gg aside, issue #1154).
    Resolving only the parent (which outlives the leaf) and keeping the leaf
    name yields a key that ingestion and deletion agree on, and one that stays
    inside the repository for a link whose target points outside it.
    """
    return (file_path.parent.resolve() / file_path.name).as_posix()


# #495: .cgrignore lines and --exclude values are interpreted with
# .gitignore (gitwildmatch) semantics: bare names match at any depth (as
# before), and globs / anchoring / dir-only trailing slash now work. The
# spec is compiled once per pattern set (frozensets are hashable).
@lru_cache(maxsize=64)
def compiled_ignore_spec(patterns: frozenset[str]) -> PathSpec:
    return PathSpec.from_lines(cs.GITWILDMATCH_STYLE, sorted(patterns))


def matches_ignore_patterns(rel_path_str: str, patterns: frozenset[str]) -> bool:
    return compiled_ignore_spec(patterns).match_file(rel_path_str)


_GLOB_MAGIC = re.compile(r"[*?\[]")


def unignore_names_this_file(rel_path_str: str, patterns: frozenset[str]) -> bool:
    """Whether some unignore pattern names THIS FILE rather than a container.

    `matches_ignore_patterns` cannot answer this: gitwildmatch matches
    `build/js/jquery.min.js` against the directory pattern `build` just as
    readily as against the file's own path, so a directory-level `!` would
    otherwise rescue a bundle inside it.

    This is the "what counts as exact" decision issue #1637 left open. A
    pattern qualifies when, with any trailing slash removed, it equals the
    file's path or its bare filename -- so `!docs/js/jquery.min.js` and
    `!jquery.min.js` both rescue, while `!docs`, `!docs/js` and `!docs/**` do
    not. Globs are deliberately excluded: `!docs/**` reads as "rescue this
    subtree", which is the directory-level intent the split preserves.
    """
    filename = rel_path_str.rsplit(cs.SEPARATOR_SLASH, 1)[-1]
    for pattern in patterns:
        candidate = pattern.strip().rstrip(cs.SEPARATOR_SLASH)
        if not candidate or _GLOB_MAGIC.search(candidate):
            continue
        if candidate.lstrip(cs.SEPARATOR_SLASH) in (rel_path_str, filename):
            return True
    return False


def unignore_could_match_within(pattern: str, rel_dir: str) -> bool:
    # Dir-pruning guard: keep a pruned-by-default directory when an
    # unignore pattern could match it or anything beneath it.
    if "/" not in pattern.rstrip("/"):
        # slash-free patterns are unanchored: they can match at any depth.
        return True
    head, *glob_rest = _GLOB_MAGIC.split(pattern, 1)
    if glob_rest:
        # the glob may complete the trailing segment; keep whole segments.
        head = head.rsplit("/", 1)[0]
    head = head.strip("/")
    return (
        not head
        or head == rel_dir
        or head.startswith(f"{rel_dir}/")
        or rel_dir.startswith(f"{head}/")
    )


def should_keep_dir(
    dirname: str,
    dir_prefix: str,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Whether a repository walk descends into this directory.

    The one predicate every whole-repo walk prunes with, so a sweep that
    reads files the indexer skipped (or skips files the indexer holds)
    cannot happen by construction (issue #1088).
    """
    rel_dir = f"{dir_prefix}{dirname}"
    # an explicit exclude can never be rescued by unignore (excludes win
    # at the file level too), so prune the subtree outright.
    if exclude_paths and matches_ignore_patterns(f"{rel_dir}/", exclude_paths):
        return False
    if dirname not in cs.IGNORE_PATTERNS:
        return True
    # Cargo's src/bin/ holds first-party binaries, not build output;
    # mirrors has_ignored_dir_part.
    if (
        dirname == cs.DIR_BIN
        and dir_prefix.rstrip(cs.SEPARATOR_SLASH).rsplit(cs.SEPARATOR_SLASH, 1)[-1]
        == cs.DIR_SRC
    ):
        return True
    return bool(
        unignore_paths
        and any(unignore_could_match_within(u, rel_dir) for u in unignore_paths)
    )


def is_ignored_filename(name: str) -> bool:
    """Whether a filename is a machine-generated artefact, by its ending.

    The single definition of the `IGNORE_SUFFIXES` rule, shared by the
    repository walk and the real-time watcher. It tests the whole filename
    rather than `Path.suffix` because the list holds endings that are not
    pathlib suffixes: `Path("jquery.min.js").suffix` is ".js" and
    `Path("notes.py~").suffix` is ".py~", so a `Path.suffix` membership test
    matched neither, and the two consumers disagreed about which files belong
    in the graph -- `~` was live for the watcher and dead for the indexer
    (issue #1636).
    """
    return name.endswith(cs.IGNORE_FILENAME_ENDINGS)


def is_unconditionally_ignored_filename(name: str) -> bool:
    """Whether a filename is ignored even against an explicit unignore.

    The stricter half of `is_ignored_filename`: compiled output and editor
    droppings, which no configuration should resurrect. The rest of the list
    is text a parser can read, so an explicit `!` line rescues it instead
    (issue #1637). Both walk predicates must ask this question the same way,
    or the indexer and the watcher disagree about which files are in the graph.
    """
    return name.endswith(cs.UNCONDITIONAL_IGNORE_FILENAME_ENDINGS)


def should_skip_path(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    is_file: bool | None = None,
) -> bool:
    _is_file = path.is_file() if is_file is None else is_file
    # Ahead of every exclude/unignore check for the UNCONDITIONAL half only:
    # compiled output is not source in any configuration, so rescuing the
    # DIRECTORY it sits in must not drag it back in. The rescuable half is
    # tested after the unignore below, so an explicit `!` line can win.
    if _is_file and is_unconditionally_ignored_filename(path.name):
        return True
    # Containment below is lexical, so a symlink whose target escapes the root
    # would pass it and let a repo-scoped sweep read or overwrite outside files
    # (GHSA-85gg-2gfq-q95m). Resolve first, mirroring validate_project_path
    # (decorators.py) and absolute_path_within_project_root (this module).
    try:
        path.resolve().relative_to(repo_path.resolve())
    except ValueError:
        return True
    rel_path = cached_relative_path(path, repo_path)
    rel_path_str = rel_path.as_posix()
    # a trailing slash marks the path as a directory for dir-only patterns.
    match_path = rel_path_str if _is_file else f"{rel_path_str}/"
    if exclude_paths and matches_ignore_patterns(match_path, exclude_paths):
        return True
    # The rescuable half, decided BEFORE the general unignore below: that check
    # returns False for a directory-level `!` too, since gitwildmatch matches
    # `build/js/jquery.min.js` against the pattern `build`. Requiring a pattern
    # that names the FILE is what keeps `!build/` from resurrecting a bundle
    # inside it while `!build/js/jquery.min.js` rescues it (issue #1637).
    if _is_file and is_ignored_filename(path.name):
        return not (
            unignore_paths and unignore_names_this_file(rel_path_str, unignore_paths)
        )
    # unignore rescues only built-in ignores, never explicit user excludes.
    if unignore_paths and matches_ignore_patterns(match_path, unignore_paths):
        return False
    if (
        not _is_file
        and unignore_paths
        and any(unignore_could_match_within(u, rel_path_str) for u in unignore_paths)
    ):
        # structure traversal must descend into a built-in-ignored dir when
        # an unignore pattern can match beneath it (mirrors _should_keep_dir),
        # or rescued files get no Folder/Package ancestry in the graph.
        return False
    dir_parts = rel_path.parent.parts if _is_file else rel_path.parts
    return has_ignored_dir_part(dir_parts)


def has_ignored_dir_part(dir_parts: tuple[str, ...]) -> bool:
    # `bin` is a build-output ignore (dotnet's <proj>/bin, repo-root bin/)
    # EXCEPT directly under src/: Cargo's multi-binary layout puts
    # first-party binaries in src/bin/, where build systems never emit.
    for index, part in enumerate(dir_parts):
        if part not in cs.IGNORE_PATTERNS:
            continue
        if part == cs.DIR_BIN and index > 0 and dir_parts[index - 1] == cs.DIR_SRC:
            continue
        return True
    return False


def should_skip_rel_file(
    rel_path_str: str,
    dir_parts: tuple[str, ...],
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    # The filename comes from `rel_path_str` rather than a caller-supplied
    # suffix: every caller derived that suffix with a last-dot split, which
    # cannot see a compound ending like ".min.js" no matter what this function
    # then does with it (issue #1636). First, matching `should_skip_path`; the
    # two must agree on precedence as well as on the rule.
    filename = rel_path_str.rsplit(cs.SEPARATOR_SLASH, 1)[-1]
    if is_unconditionally_ignored_filename(filename):
        return True
    if exclude_paths and matches_ignore_patterns(rel_path_str, exclude_paths):
        return True
    # Same position and rule as `should_skip_path`: the rescuable half needs a
    # pattern naming the FILE, decided before the general unignore below, or a
    # directory-level `!` would rescue it there (#1637). The two predicates
    # must agree on precedence, not merely on the ending rule.
    if is_ignored_filename(filename):
        return not (
            unignore_paths and unignore_names_this_file(rel_path_str, unignore_paths)
        )
    # unignore rescues only built-in ignores, never explicit user excludes.
    if unignore_paths and matches_ignore_patterns(rel_path_str, unignore_paths):
        return False
    return has_ignored_dir_part(dir_parts)


# Longest first, so `.d.ts` is tried before the `.ts` it ends with. Derived
# from the language set rather than restated, so an extension added there
# cannot silently fall through to the single-suffix rule below (issue #1720).
_MODULE_EXTS_LONGEST_FIRST: tuple[str, ...] = tuple(
    sorted(cs.JS_TS_MODULE_EXTENSIONS, key=str.__len__, reverse=True)
)


def module_stem(filename: str) -> str:
    """The filename with its MODULE extension removed, compound ones included.

    ``Path.with_suffix("")`` strips one dot-segment, which is right for `.py`
    and wrong for `.d.ts`: TypeScript declaration files carry a two-segment
    extension that the language treats as a unit, and every other part of cgr
    already does too -- ``JS_TS_MODULE_EXTENSIONS`` lists `.d.ts`, and the
    JS/TS resolver looks up `pkg/index.d.ts` as the `pkg` entry point. Only the
    qn derivation disagreed, storing it as `proj.pkg.index.d`, a name no
    importer ever asks for, so its definitions were unreachable (issue #1720).

    Extensions outside the language set keep the single-suffix behaviour:
    `archive.tar.gz` is not a module named `archive`, and a `.d` in some other
    language is not a declaration marker.
    """
    for ext in _MODULE_EXTS_LONGEST_FIRST:
        if filename.endswith(ext) and len(filename) > len(ext):
            return filename[: -len(ext)]
    return Path(filename).stem


_DECLARATION_EXTS: tuple[str, ...] = tuple(
    ext
    for ext in _MODULE_EXTS_LONGEST_FIRST
    if ext.startswith(cs.DECLARATION_EXT_PREFIX)
)
_IMPLEMENTATION_EXTS: tuple[str, ...] = tuple(
    ext for ext in _MODULE_EXTS_LONGEST_FIRST if ext not in _DECLARATION_EXTS
)


def declaration_extension(filename: str) -> str | None:
    """The TYPE-ONLY module extension this file carries, if any.

    Partitioned from the one language set rather than listed again, so a
    declaration form added to ``JS_TS_MODULE_EXTENSIONS`` is classified here
    without a second edit.
    """
    for ext in _DECLARATION_EXTS:
        if filename.endswith(ext) and len(filename) > len(ext):
            return ext
    return None


def has_implementation_sibling(path: Path) -> bool:
    """Does a same-stem file with a NON-declaration module extension exist?

    Asked of the filesystem rather than of the parse registry deliberately.
    The registry answers "has one been seen yet", which depends on walk order;
    the disk answers "does one exist", which does not. The distinction is the
    whole point -- a `.d.ts` sorts before its `.ts` in an ascending walk, so a
    registry-based tie-break hands the shared name to the stub every time
    (issue #1720, and the regression that closed PR #1721).
    """
    stem = module_stem(path.name)
    return any((path.parent / f"{stem}{ext}").is_file() for ext in _IMPLEMENTATION_EXTS)


def base_module_qn(rel_path: Path, project_name: str) -> str:
    """The module qualified name for a file, BEFORE collision disambiguation.

    Shared by the tree-sitter indexer (``DefinitionProcessor.process_file``)
    and the C++ module-qn map (``cpp_frontend.qn``), which must agree
    byte-for-byte: the whole graph keys on these names, and a disagreement
    silently splits one module into two nodes rather than raising (#1025).

    ``__init__.py`` and ``mod.rs`` name their PACKAGE rather than themselves,
    so they drop their own filename segment.
    """
    if rel_path.name in (cs.INIT_PY, cs.MOD_RS):
        parts = rel_path.parent.parts
    else:
        parts = (*rel_path.parent.parts, module_stem(rel_path.name))
    return cs.SEPARATOR_DOT.join([project_name, *parts])


def _walk_dir_keys(
    dirpath: str, repo_prefix_len: int
) -> tuple[tuple[str, ...], str, str]:
    """Derive a walked directory's ``(parts, cache_key, prefix)``.

    Split out of ``walk_eligible_files`` so the walk body reads as filtering
    alone. The repository root is the special case: it has no relative path,
    and its cache key is ``ROOT_DIR_KEY`` rather than the empty string.

    The relative directory itself is deliberately not returned: every caller
    needs it only as the ``dir_prefix`` built here, and handing back both
    invites the two to be derived independently.
    """
    if len(dirpath) < repo_prefix_len:
        return (), cs.ROOT_DIR_KEY, ""
    rel_dir = dirpath[repo_prefix_len:].replace(os.sep, "/")
    dir_parts = tuple(rel_dir.split("/")) if rel_dir else ()
    return (
        dir_parts,
        rel_dir or cs.ROOT_DIR_KEY,
        f"{rel_dir}/" if rel_dir else "",
    )


def walk_eligible_files(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    on_dir: Callable[[str, str], None] | None = None,
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(dirpath, filename, rel_path)`` for every indexable file, in order.

    The single definition of the repository walk. Both the tree-sitter indexer
    (``GraphUpdater._collect_eligible_files``) and the C++ module-qn map
    (``cpp_frontend.qn.build_module_qn_map``) consume it, because the module-qn
    disambiguation rule hands the base qn to whichever file is seen FIRST and
    appends an extension to the loser -- so the two must agree on ORDER, not
    merely on which files are eligible (issues #1025, #1099).

    They previously kept separate copies of this loop that shared only the
    filter predicates. Nothing forced the two orderings to stay equal, and a
    set-based parity test cannot see the difference.

    ``on_dir`` receives ``(dir_key, dirpath)`` per visited directory, for the
    indexer's mtime bookkeeping; it must not mutate the walk.
    """
    repo_str = str(repo_path)
    # A repo path that already ends in a separator (the filesystem root, "/")
    # must not have another counted, or the slice below eats the first
    # character of every child -- "/tmp" became "mp" (CodeRabbit, PR #1511).
    repo_prefix_len = len(repo_str) + (0 if repo_str.endswith(os.sep) else 1)
    state_filenames = cs.CGR_STATE_FILENAMES
    for dirpath, dirnames, filenames in os.walk(repo_str):
        dir_parts, dir_key, dir_prefix = _walk_dir_keys(dirpath, repo_prefix_len)
        if on_dir is not None:
            on_dir(dir_key, dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if should_keep_dir(d, dir_prefix, exclude_paths, unignore_paths)
        )
        for fname in sorted(filenames):
            if fname in state_filenames:
                continue
            rel_path_str = f"{dir_prefix}{fname}"
            if not should_skip_rel_file(
                rel_path_str,
                dir_parts,
                exclude_paths=exclude_paths,
                unignore_paths=unignore_paths,
            ):
                yield dirpath, fname, rel_path_str


def project_roots_from_rows(
    rows: Iterable[Mapping[str, object]],
) -> dict[str, str | None]:
    """Build {project_name: root_path} from CYPHER_LIST_PROJECTS rows."""
    roots: dict[str, str | None] = {}
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        root = row.get("root_path")
        roots[name] = root if isinstance(root, str) else None
    return roots


def absolute_path_within_project_root(
    qualified_name: str, absolute_path: str, roots: dict[str, str | None]
) -> bool:
    """A stored absolute path may only be read from inside its own project's
    indexed root; projects with no recorded root (legacy graphs) stay
    readable (issue #425). Project names may contain dots, so the owning
    project is the longest known name prefixing the qualified name. The
    resolve() calls are load-bearing: containment is checked lexically, so
    an unresolved ``..`` segment or symlink would escape the root."""
    matches = [name for name in roots if qualified_name.startswith(name + ".")]
    if not matches:
        return True
    owner = matches[0]
    for name in matches[1:]:
        if len(name) > len(owner):
            owner = name
    root = roots[owner]
    if root is None:
        return True
    return Path(absolute_path).resolve().is_relative_to(Path(root).resolve())
