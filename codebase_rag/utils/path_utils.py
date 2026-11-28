import fnmatch
import hashlib
import os
import posixpath
import re
import stat
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path, PurePath
from typing import NamedTuple

from pathspec import GitIgnoreSpec, PathSpec, RegexPattern

from .. import constants as cs

_PROJECT_NAME_INVALID_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_PROJECT_NAME_FALLBACK_BASE = "repo"
_PROJECT_NAME_SEPARATORS = re.compile(r"\.+")


def derive_project_name(repo_path: Path) -> str:
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[
        : cs.PROJECT_NAME_DIGEST_LEN
    ]
    # Strip `-` as well as `_`: a name like `мой-repo` would otherwise start
    # with `-`, and so would every qualified name under it, which Click then
    # reads as an option wherever a command takes one (issue #2638).
    base = _PROJECT_NAME_INVALID_CHARS.sub("_", resolved.name).strip("_-")
    if not base:
        base = _PROJECT_NAME_FALLBACK_BASE
    return f"{base}{cs.PROJECT_NAME_DIGEST_MARKER}{digest}"


def nested_project_names(project_name: str, projects: Iterable[str]) -> list[str]:
    """The projects whose names extend `project_name` (`svc.v2` for `svc`).

    `svc.` prefixes every qualified name of `svc.v2` too, but the longest
    registered name a symbol sits under owns it (issue #1970).
    """
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    return [name for name in projects if name.startswith(prefix)]


def default_project_name(repo_path: Path) -> str:
    """The name a run that names no project writes under.

    The directory name, as before, unless it holds the qualified-name
    separator: `acme.web/` would write the nodes of project `acme`'s package
    `web` (#2412). Such a checkout gets the digest-suffixed derived name, not
    just the `.` dropped, which would give `acme.web/` and `acme_web/` the
    same project (review of PR 2497).
    """
    directory = repo_path.resolve().name
    if cs.SEPARATOR_DOT not in directory:
        return directory
    return derive_project_name(repo_path)


def project_name_error(name: str) -> str | None:
    """Why `name` cannot be stored as a project name, or None if it can.

    Qualified names join the project name and the package path with `.`, and
    nodes merge on qualified name, so project `acme.web` would write the
    nodes of project `acme`'s package `web` (issue #2412).
    """
    if cs.SEPARATOR_DOT not in name:
        return None
    return cs.ERR_PROJECT_NAME_HAS_SEPARATOR.format(
        name=name, suggestion=separator_free_project_name(name)
    )


def separator_free_project_name(name: str) -> str:
    if cs.SEPARATOR_DOT not in name:
        return name
    return (
        _PROJECT_NAME_SEPARATORS.sub("_", name.strip(cs.SEPARATOR_DOT))
        or _PROJECT_NAME_FALLBACK_BASE
    )


def normalize_project_name(project_name: str | None, repo_path: Path) -> str:
    # The name a sync writes its project under. The CLI's ownership check
    # asks about the same name, so a padded one cannot pass the check under
    # one spelling and be written under another (review of PR 2499).
    return (project_name and project_name.strip()) or default_project_name(repo_path)


def resolve_repo_path(repo_path: str | None, target_default: str) -> Path:
    if repo_path:
        return Path(repo_path).resolve()
    if target_default and target_default != ".":
        return Path(target_default).resolve()
    return Path.cwd().resolve()


def unwritable_output_reason(path: Path) -> str | None:
    """Why no file can be written at `path`, or None when one can.

    Run before the graph is read or indexed, so a mistyped `-o` fails at once
    instead of after the work it was meant to save (issue #2410). Missing
    parent directories are fine: the writer creates them.
    """
    if path.is_dir():
        return cs.CLI_ERR_OUTPUT_IS_DIR.format(path=path)
    if path.exists():
        if os.access(path, os.W_OK):
            return None
        return cs.CLI_ERR_OUTPUT_NOT_WRITABLE.format(target=path)
    ancestor = next((parent for parent in path.parents if parent.exists()), None)
    if ancestor is None:
        return None
    if not ancestor.is_dir():
        return cs.CLI_ERR_OUTPUT_PARENT_NOT_DIR.format(parent=ancestor)
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return cs.CLI_ERR_OUTPUT_NOT_WRITABLE.format(target=ancestor)
    return None


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


def portable_posix(path: str) -> str:
    """A path in normalised POSIX form, whichever OS recorded it.

    Nodes store `as_posix()` of the path relative to the root, so `\\`
    separators, `./` and doubled slashes are spellings of the same file.
    ``Path.as_posix`` converts only the running host's separator, so a
    Windows path read on Linux kept its backslashes and never matched a graph
    path. No real source path contains a backslash on POSIX, so treating one
    as a separator loses nothing.
    """
    return posixpath.normpath(path.replace(cs.SEPARATOR_BACKSLASH, cs.SEPARATOR_SLASH))


def ordered_ignore_block(lines: Iterable[str]) -> str:
    """One exclude-set entry that evaluates `lines` in order, as git does.

    A `.gitignore` exclude followed by `!` lines is not a set of independent
    patterns: the last matching line decides (issue #2835). Patterns cannot
    hold a line break, so the joined lines are one entry, compiled apart from
    the rest of the set; a set without such an entry behaves as before.
    """
    return cs.IGNORE_BLOCK_SEPARATOR.join(lines)


def ordered_block_lines(pattern: str) -> list[str]:
    return pattern.split(cs.IGNORE_BLOCK_SEPARATOR)


class _BlockLine(NamedTuple):
    line: str
    pattern: RegexPattern


class _OrderedBlock(NamedTuple):
    lines: tuple[_BlockLine, ...]
    # The block's excludes alone: a path none of them reaches is kept, so
    # most paths cost one match, not one per line and directory level.
    excludes: GitIgnoreSpec


class _IgnoreMatcher(NamedTuple):
    patterns: PathSpec
    ordered_blocks: tuple[_OrderedBlock, ...]

    def match_file(self, rel_path_str: str) -> bool:
        return self.patterns.match_file(rel_path_str) or any(
            _git_ignores(block, rel_path_str) for block in self.ordered_blocks
        )


def _ordered_block(lines: list[str]) -> _OrderedBlock:
    spec = GitIgnoreSpec.from_lines(lines)
    return _OrderedBlock(
        lines=tuple(
            _BlockLine(line, pattern)
            for line, pattern in zip(lines, spec.patterns, strict=True)
        ),
        excludes=GitIgnoreSpec.from_lines(
            line for line in lines if not line.startswith(cs.GITIGNORE_NEGATION)
        ),
    )


def _git_ignores(block: _OrderedBlock, rel_path_str: str) -> bool:
    """Whether git ignores the path: at the path itself or at one of its
    parent directories, the last line matching that level is an exclude.

    So an excluded directory keeps everything beneath it excluded (`gen/`
    then `!gen/keep.py` keeps nothing), and a `!` line re-includes only what
    it matches itself: `!pkgs/` leaves `pkgs/a/gen/` excluded by `gen/`,
    while `src/**` then `!src/gen/` and `!src/gen/keep.py` keeps that file,
    since `src/**` excludes what is inside `src`, not `src`.
    """
    if not block.excludes.match_file(rel_path_str):
        return False
    is_dir = rel_path_str.endswith(cs.SEPARATOR_SLASH)
    parts = rel_path_str.rstrip(cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH)
    for depth in range(1, len(parts) + 1):
        level = cs.SEPARATOR_SLASH.join(parts[:depth])
        level_is_dir = is_dir or depth < len(parts)
        ignored = False
        for entry in block.lines:
            if entry.pattern.include is not None and _matches_level(
                entry, level, level_is_dir
            ):
                ignored = entry.pattern.include
        if ignored:
            return True
    return False


def _matches_level(entry: _BlockLine, level: str, is_dir: bool) -> bool:
    # Whether the line matches `level` itself. pathspec's regexes also match
    # every path beneath a matched directory, marking where that directory
    # ends, and git judges a path beneath it by its own lines.
    probe = f"{level}{cs.SEPARATOR_SLASH}" if is_dir else level
    found = entry.pattern.match_file(probe)
    if found is None:
        return False
    match = found.match
    if match.re.pattern == cs.PATHSPEC_ANY_DIR_REGEX:
        return is_dir
    mark = cs.PATHSPEC_DIR_MARK_GROUP
    start = match.start(mark) if mark in match.re.groupindex else -1
    if entry.line.endswith(cs.GITIGNORE_DIRS_INSIDE_SUFFIX):
        return is_dir and 0 <= start < len(level)
    if start >= 0:
        return start == len(level)
    # `x/**` matches what is inside x, which the probe `x/` is not.
    return not (is_dir and match.end() == len(probe))


# #495: .cgrignore lines and --exclude values are interpreted with
# .gitignore (gitwildmatch) semantics: bare names match at any depth (as
# before), and globs / anchoring / dir-only trailing slash now work. The
# spec is compiled once per pattern set (frozensets are hashable).
@lru_cache(maxsize=64)
def compiled_ignore_spec(patterns: frozenset[str]) -> _IgnoreMatcher:
    blocks = {p for p in patterns if cs.IGNORE_BLOCK_SEPARATOR in p}
    return _IgnoreMatcher(
        patterns=PathSpec.from_lines(cs.GITWILDMATCH_STYLE, sorted(patterns - blocks)),
        ordered_blocks=tuple(
            _ordered_block(ordered_block_lines(b)) for b in sorted(blocks)
        ),
    )


def ignore_line_error(line: str) -> ValueError | re.error | None:
    """Why pathspec refuses `line` as an ignore pattern, or None if it takes it.

    `compiled_ignore_spec` compiles a pattern set at once, so a single line it
    refuses, such as one ending in a backslash with nothing left to escape,
    failed the whole run (#3723). The ignore files are checked line by line as
    they are read, so such a line is left out on its own. A line can end up
    among the plain patterns or in an ordered `.gitignore` block, so it is
    compiled the way each of them compiles it. pathspec refuses some lines
    itself (ValueError) and hands others, such as the backwards range
    `[z-a]`, to `re.compile`, which raises `re.error`.
    """
    try:
        PathSpec.from_lines(cs.GITWILDMATCH_STYLE, [line])
        GitIgnoreSpec.from_lines([line])
    except (ValueError, re.error) as error:
        return error
    return None


def matches_ignore_patterns(rel_path_str: str, patterns: frozenset[str]) -> bool:
    return compiled_ignore_spec(patterns).match_file(rel_path_str)


def exclusions_matching(
    match_path: str, exclude_paths: frozenset[str]
) -> frozenset[str]:
    """The exclude lines that match `match_path` (a directory ends in `/`).

    The walk's decision made pattern by pattern, so a caller can name the
    line that keeps a path out. An ordered `.gitignore` block is named by its
    exclude lines that match, not as one multi-line entry.
    """
    return frozenset(
        line
        for pattern in exclude_paths
        if matches_ignore_patterns(match_path, frozenset({pattern}))
        for line in ordered_block_lines(pattern)
        if not line.startswith(cs.GITIGNORE_NEGATION)
        and matches_ignore_patterns(match_path, frozenset({line}))
    )


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
    if dirname in cs.VCS_METADATA_DIRS:
        return unignore_names_vcs_dir(rel_dir, unignore_paths)
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


def is_symlink_entry(path: str | os.PathLike[str]) -> bool:
    """Whether a repository walk leaves this entry out as a symbolic link.

    The one symlink rule every walk applies (issue #2451): a link is never
    followed, whether it names a file or a directory and wherever it points.
    A target outside the repository would put content from beyond
    `--repo-path` into a graph every project shares. A target inside it is
    indexed once, under its own path, when the walk reaches it; following
    the link as well made a second module of the same code, while the walk
    already declined to enter linked directories. `os.path.islink` judges the
    entry itself and answers False on any error, and a dangling link or a
    cycle is a link like any other.

    A Windows directory junction is no link to `os.path.islink`, so the walk
    enters one; `is_junction_left_out` leaves out, as a link, one that names
    a folder outside the repository or one the walk entered on its way to it.
    """
    return os.path.islink(path)


def is_junction_left_out(path: str | os.PathLike[str], repo_path: Path) -> bool:
    """Whether a repository walk leaves this entry out as a junction, reporting
    it as it reports a link: one naming a folder outside the repository, or
    one naming a folder the walk entered on its way to the junction.

    `os.walk` enters a Windows directory junction, so the walk indexed what
    one named wherever it pointed, while the structure pass leaves out a
    folder that resolves outside the root: those files became modules with
    no parent (issue #3740). Content beyond `--repo-path` is never indexed
    through a link (issue #2451), nor through a junction. And one naming its
    own folder or an ancestor was entered again at every level until the
    path grew too long, two of them doubling the walk each time. The way
    down is the root and each folder below it to the junction's own, each
    by its real path, so a cycle through other junctions is a loop too.

    A junction is judged by its target as `os.readlink` reads it, so a
    dangling one by where it points, and one whose target cannot be read is
    left out. Any other junction is entered, as before. Always False off
    Windows, where `os.path.isjunction` is.
    """
    if not os.path.isjunction(path):
        return False
    try:
        target = os.readlink(path)
        folder = Path(os.path.dirname(path))
        resolved = Path(folder, without_verbatim_prefix(target)).resolve()
        way_down = [entered.resolve() for entered in _way_down(folder, repo_path)]
        return junction_left_out(resolved, way_down, repo_path.resolve())
    except (OSError, RuntimeError, ValueError):
        return True


def junction_left_out(
    target: PurePath, way_down: Iterable[PurePath], root: PurePath
) -> bool:
    """Whether a junction whose resolved target is `target` is left out: it
    is outside the resolved `root`, or the same folder as one of `way_down`,
    the real paths of the folders entered to reach it.

    Each path is compared without the `\\\\?\\` prefix where a drive or share
    follows it and as its flavour compares (without case on Windows):
    `Path.resolve()` keeps a prefix it is given, so a root spelled with one
    held no target spelled without it.
    """
    spelled = _comparable(target)
    return not spelled.is_relative_to(_comparable(root)) or any(
        spelled == _comparable(entered) for entered in way_down
    )


def _comparable(path: PurePath) -> PurePath:
    return type(path)(without_verbatim_prefix(str(path)))


def _way_down(folder: Path, repo_path: Path) -> list[Path]:
    """The folders a walk of `repo_path` entered to reach `folder`: the root,
    each folder below it, `folder` last."""
    try:
        parts = folder.relative_to(repo_path).parts
    except ValueError:
        return [folder]
    way_down = [repo_path]
    for part in parts:
        way_down.append(way_down[-1] / part)
    return way_down


def without_verbatim_prefix(path: str) -> str:
    """`path` without the `\\\\?\\` prefix `os.readlink` gives a junction's target,
    where a drive or a share follows it.

    `Path.resolve()` keeps a prefix it is given, so a target spelled with
    one would never compare as inside the root. Any other path keeps it: a
    volume's name after it is no path on a drive.
    """
    if path.startswith(cs.WINDOWS_VERBATIM_UNC_PREFIX):
        return cs.WINDOWS_UNC_PREFIX + path[len(cs.WINDOWS_VERBATIM_UNC_PREFIX) :]
    rest = path[len(cs.WINDOWS_VERBATIM_PREFIX) :]
    if (
        path.startswith(cs.WINDOWS_VERBATIM_PREFIX)
        and rest[1:2] == cs.WINDOWS_DRIVE_SEPARATOR
    ):
        return rest
    return path


def walk_tree(
    top: str | os.PathLike[str],
) -> Iterator[tuple[str, list[str], list[str]]]:
    """`os.walk(top)`, top-down, entering no Windows junction the repository
    walk leaves out (`is_junction_left_out`, judged against `top`).

    `os.walk` and `rglob` enter every junction, as one is no link to
    `os.path.islink`, so each walk of the repository besides the indexer's
    (the import maps', the build files', the contracts') went on down one
    naming a folder on its own way down until the path grew too long, two
    of them doubling the walk at every level: the run never ended (issue
    #3740). The caller prunes the folders it is given as it would
    `os.walk`'s. Off Windows it is `os.walk`.
    """
    root = Path(top)
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [
            name
            for name in dirnames
            if not is_junction_left_out(os.path.join(dirpath, name), root)
        ]
        yield dirpath, dirnames, filenames


def rglob_files(top: Path, pattern: str) -> Iterator[Path]:
    """What `top.rglob(pattern)` finds that is no folder, walked by
    `walk_tree`, as `rglob` enters every junction too.

    A folder whose name matches is left out: each caller keeps only the
    regular files it finds.
    """
    for dirpath, _dirnames, filenames in walk_tree(top):
        for name in fnmatch.filter(filenames, pattern):
            yield Path(dirpath, name)


def is_special_file_entry(path: str | os.PathLike[str]) -> bool:
    """Whether a repository walk leaves this entry out as a pipe, socket or device.

    Opening a named pipe blocks until something writes to it, so hashing one
    hung the run for good (issue #3721), and a device can be read forever.
    Judged by `os.lstat`, which never opens the entry. A regular file and a
    directory are not special, nor is a link, which `is_symlink_entry` judges
    on its own. An entry `lstat` cannot read is not special either: the
    unreadable-file handling deals with it, as it did before.
    """
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return False
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode))


def is_regular_file(path: str | os.PathLike[str]) -> bool:
    """Whether a file a reader found by listing a directory itself may be read.

    The walk leaves pipes, sockets and devices out (`is_special_file_entry`),
    but a resolver that lists folders on its own sees them, and opening a
    named pipe blocks until something writes to it: one manifest or source
    of that name hung the whole run (issue #3721). `os.path.isfile` follows
    a link, so a link to a pipe counts as the pipe, and holds for a regular
    file only; a path it cannot stat reads as absent, as a failed read did.
    """
    return os.path.isfile(path)


def is_enterable_dir(path: str | os.PathLike[str]) -> bool:
    """Whether the walk would list this directory and reach what is in it.

    `os.walk` indexes a directory's contents only when it may both read
    (list) and search (reach the entries of) it, and passes over one it may
    not without a word, so nothing inside such a directory reaches the
    graph. A reader that maps names to directories on its own, such as
    source-root discovery, asks this to agree with the walk: an import
    resolved into a folder cgr may not enter names nothing the graph holds
    (issues #3748, #3750). Never raises. Root, and Windows, where the mode
    bits do not bind, read every directory as enterable.
    """
    return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)


def is_utf8_name(name: str) -> bool:
    """Whether a name from a repository walk can be stored as text.

    `os.walk` decodes bytes that are not valid UTF-8 as lone surrogates
    (PEP 383), and no surrogate encodes: one such file or directory name
    reached the graph's keys and the hand-off files, and failing to write it
    ended the whole run (issue #3722). The walk leaves such an entry out.
    """
    try:
        name.encode(cs.ENCODING_UTF8)
    except UnicodeEncodeError:
        return False
    return True


def readable_name(name: str) -> str:
    """A path `is_utf8_name` rejects, spelled for a log: each bad byte as `\\xff`."""
    return os.fsencode(name).decode(cs.ENCODING_UTF8, cs.DECODE_ERRORS_BACKSLASHREPLACE)


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
    dir_parts = rel_path.parent.parts if _is_file else rel_path.parts
    if (
        verdict := _vcs_metadata_verdict(
            dir_parts, rel_path_str, _is_file, unignore_paths
        )
    ) is not None:
        return verdict
    # The rescuable half, decided BEFORE the general unignore below: that check
    # returns False for a directory-level `!` too, since gitwildmatch matches
    # `build/js/jquery.min.js` against the pattern `build`. Requiring a pattern
    # that names the FILE is what keeps `!build/` from resurrecting a bundle
    # inside it while `!build/js/jquery.min.js` rescues it (issue #1637).
    if _is_file and is_ignored_filename(path.name):
        return not (
            unignore_paths and unignore_names_this_file(rel_path_str, unignore_paths)
        )
    if _unignore_rescues(match_path, rel_path_str, _is_file, unignore_paths):
        return False
    return has_ignored_dir_part(dir_parts)


def _vcs_metadata_verdict(
    dir_parts: tuple[str, ...],
    rel_path_str: str,
    is_file: bool,
    unignore_paths: frozenset[str] | None,
) -> bool | None:
    # Whether to skip a path for the version-control metadata dirs on it:
    # True or False when they decide, None when they do not.
    if has_unrescued_vcs_part(dir_parts, unignore_paths):
        return True
    # A metadata dir a `!` line names, and a directory in it that the line
    # spells a path through, are kept as the walk keeps them: the built-in
    # check after this counts `.git` itself as ignored, so a file rescued
    # there had no Folder ancestry (CodeRabbit review of #3644).
    if (
        not is_file
        and not cs.VCS_METADATA_DIRS.isdisjoint(dir_parts)
        and unignore_names_vcs_dir(rel_path_str, unignore_paths)
    ):
        return False
    return None


def _unignore_rescues(
    match_path: str,
    rel_path_str: str,
    is_file: bool,
    unignore_paths: frozenset[str] | None,
) -> bool:
    if not unignore_paths:
        return False
    # unignore rescues only built-in ignores, never explicit user excludes.
    if matches_ignore_patterns(match_path, unignore_paths):
        return True
    # structure traversal must descend into a built-in-ignored dir when
    # an unignore pattern can match beneath it (mirrors _should_keep_dir),
    # or rescued files get no Folder/Package ancestry in the graph.
    return not is_file and any(
        unignore_could_match_within(u, rel_path_str) for u in unignore_paths
    )


def unignore_names_vcs_dir(rel_dir: str, unignore_paths: frozenset[str] | None) -> bool:
    """Whether an unignore spells out a path through this directory.

    Asked of a version-control metadata dir, and of a directory inside one.
    Only such a line (`!submodule1/.git`) rescues anything in it. A line that
    can match at any depth (`!*.[Cc]ache/`, `!**/packages/build/`, both in
    .NET's standard `.gitignore`) keeps every other built-in-ignored
    directory walkable, but walking `.git` for it recorded Git's own folders
    as Folder nodes (issue #3642).
    """
    if not unignore_paths:
        return False
    dir_parts = tuple(rel_dir.split(cs.SEPARATOR_SLASH))
    for pattern in unignore_paths:
        bare = pattern.strip(cs.SEPARATOR_SLASH)
        if cs.SEPARATOR_SLASH not in bare:
            # `!.git/` names a `.git` at any depth, `!/.git/` only the root's.
            anchored = pattern.startswith(cs.SEPARATOR_SLASH)
            if bare == dir_parts[-1] and (not anchored or len(dir_parts) == 1):
                return True
            continue
        if _pattern_reaches_dir(tuple(bare.split(cs.SEPARATOR_SLASH)), dir_parts):
            return True
    return False


def _pattern_reaches_dir(
    pattern_parts: tuple[str, ...], dir_parts: tuple[str, ...]
) -> bool:
    # Whether the pattern's leading segments match the whole directory path.
    # Any segment may be a glob or `**`, except a metadata dir that ends the
    # path: it must be spelled out, since a wildcard standing in for it
    # (`*/*/x.py`) is a broad line, not a rescue.
    #
    # One dir segment at a time, keeping the set of pattern positions that
    # can stand there, so each (pattern, dir) position pair is visited once:
    # recursing on `**` both ways was exponential in the number of `**`
    # segments (CodeRabbit review of #3644).
    last = len(dir_parts) - 1
    positions = _past_empty_double_stars({0}, pattern_parts)
    for index, part in enumerate(dir_parts):
        literal = index == last and part in cs.VCS_METADATA_DIRS
        positions = _past_empty_double_stars(
            _positions_after(positions, pattern_parts, part, literal), pattern_parts
        )
        if not positions:
            return False
    return True


def _positions_after(
    positions: set[int], pattern_parts: tuple[str, ...], part: str, literal: bool
) -> set[int]:
    # The pattern positions that can follow `positions` once a directory
    # segment `part` is consumed. A `literal` part (a metadata dir ending the
    # path) only an identical segment matches; a `**` never stands for it.
    advanced: set[int] = set()
    for position in positions:
        if position == len(pattern_parts):
            continue
        segment = pattern_parts[position]
        if segment == cs.GLOB_DOUBLE_STAR:
            if not literal:
                advanced.add(position)
        elif segment == part if literal else fnmatch.fnmatchcase(part, segment):
            advanced.add(position + 1)
    return advanced


def _past_empty_double_stars(
    positions: set[int], pattern_parts: tuple[str, ...]
) -> set[int]:
    # A `**` may match no segment at all, so the position after it is
    # reachable wherever it is.
    reachable = set(positions)
    pending = list(positions)
    while pending:
        position = pending.pop()
        if (
            position < len(pattern_parts)
            and pattern_parts[position] == cs.GLOB_DOUBLE_STAR
            and position + 1 not in reachable
        ):
            reachable.add(position + 1)
            pending.append(position + 1)
    return reachable


def has_unrescued_vcs_part(
    dir_parts: tuple[str, ...], unignore_paths: frozenset[str] | None
) -> bool:
    # Ahead of the general unignore: the same rule `should_keep_dir` applies
    # to the walk, so the file predicates and the walk agree (issue #3642).
    for index, part in enumerate(dir_parts):
        if part in cs.VCS_METADATA_DIRS and not unignore_names_vcs_dir(
            cs.SEPARATOR_SLASH.join(dir_parts[: index + 1]), unignore_paths
        ):
            return True
    return False


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
    if has_unrescued_vcs_part(dir_parts, unignore_paths):
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


def module_extension(filename: str) -> str:
    """The extension `module_stem` removes, dot included (`.py`, `.d.ts`)."""
    return filename[len(module_stem(filename)) :]


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


def has_implementation_sibling(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Will a same-stem NON-declaration module actually be INDEXED?

    Asked of the filesystem rather than of the parse registry deliberately.
    The registry answers "has one been seen yet", which depends on walk order;
    the disk answers "does one exist", which does not. The distinction is the
    whole point -- a `.d.ts` sorts before its `.ts` in an ascending walk, so a
    registry-based tie-break hands the shared name to the stub every time
    (issue #1720, and the regression that closed PR #1721).

    But existence alone is too permissive, and the gap is not hypothetical: an
    excluded `foo.ts` is on disk and will never be a node, so yielding
    `proj.foo` to it leaves NOTHING owning the name imports of `./foo` resolve
    to -- #1720 reintroduced by its own fix, under a configuration the first
    version of this predicate could not see (Greptile P1 on PR #1728).

    So candidates are filtered through `should_skip_path`, the same predicate
    the repository walk prunes with. Sharing it is what stops this from
    disagreeing with the indexer about which files exist (issue #1088); asking
    the question a second way here is how the two would drift apart.
    """
    stem = module_stem(path.name)
    for ext in _IMPLEMENTATION_EXTS:
        candidate = path.parent / f"{stem}{ext}"
        # A linked sibling is no sibling: the walk leaves links out (#2451).
        if not candidate.is_file() or is_symlink_entry(candidate):
            continue
        if should_skip_path(
            candidate,
            repo_path,
            exclude_paths=exclude_paths,
            unignore_paths=unignore_paths,
            is_file=True,
        ):
            continue
        return True
    return False


def python_stub_has_implementation(
    path: Path,
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Is `path` a `.pyi` stub whose module an indexed `.py` already defines?

    Such a stub is skipped rather than parsed. It strips to the same module qn
    as its implementation, so parsing it would either overwrite the `.py`'s
    Module or, disambiguated, add a `proj.x.pyi` twin carrying a second copy
    of every signature (issue #2445). A stub with NO implementation is kept:
    for a compiled extension it is the only source the module has.

    The implementation is whatever `import` would load under the same name:
    `x.py`, or the package `x/__init__.py`, which wins over `x.py` at runtime
    and so owns `proj.x` in the graph. For `__init__.pyi` it is the
    `__init__.py` beside it. As with `has_implementation_sibling`, this asks
    the disk and the walk's own skip predicate, not the parse registry, so the
    answer does not depend on which file was walked first.

    And the walk's symlink rule (#2451): a candidate that is a link, or a
    package behind a linked `x/`, is never walked, so it is never indexed.
    Yielding to one left nothing defining the module, neither the stub nor
    the implementation it deferred to.
    """
    return any(
        candidate.is_file()
        # A linked implementation is no implementation: the walk leaves links
        # out (#2451).
        and not is_symlink_entry(candidate)
        # The walk does not enter a linked package directory `x/` (#2451).
        and not (candidate.parent != path.parent and is_symlink_entry(candidate.parent))
        and not should_skip_path(
            candidate,
            repo_path,
            exclude_paths=exclude_paths,
            unignore_paths=unignore_paths,
            is_file=True,
        )
        for candidate in python_stub_implementations(path)
    )


def declaration_implementations[P: PurePath](path: P) -> list[P]:
    """The files that may implement the module a declaration file declares,
    by path alone, as the indexer pairs them: a `.pyi` stub's (see
    `python_stub_implementations`), and for a TypeScript declaration
    (`x.d.ts`, `x.d.mts`, `x.d.cts`) every same-stem implementation module
    (`x.ts`, `x.js`, ...), as `has_implementation_sibling` asks. Nothing for
    any other file.
    """
    if declaration_extension(path.name) is None:
        return python_stub_implementations(path)
    stem = module_stem(path.name)
    return [path.with_name(f"{stem}{ext}") for ext in _IMPLEMENTATION_EXTS]


def python_stub_implementations[P: PurePath](path: P) -> list[P]:
    """The files that may implement the module a `.pyi` stub declares, by
    path alone: `x.py` or the package `x/__init__.py` for `x.pyi`, and the
    `__init__.py` beside it for `__init__.pyi`. Nothing for any other file.
    """
    if path.suffix != cs.EXT_PYI:
        return []
    if path.name == cs.INIT_PYI:
        return [path.with_name(cs.INIT_PY)]
    return [path.with_suffix(cs.EXT_PY), path.with_suffix("") / cs.INIT_PY]


def base_module_qn(rel_path: Path, project_name: str) -> str:
    """The module qualified name for a file, BEFORE collision disambiguation.

    Shared by the tree-sitter indexer (``DefinitionProcessor.process_file``)
    and the C++ module-qn map (``cpp_frontend.qn``), which must agree
    byte-for-byte: the whole graph keys on these names, and a disagreement
    silently splits one module into two nodes rather than raising (#1025).

    ``__init__.py`` (and its stub ``__init__.pyi``) and ``mod.rs`` name their
    PACKAGE rather than themselves, so they drop their own filename segment.
    """
    if rel_path.name in (*cs.PY_PACKAGE_INIT_FILES, cs.MOD_RS):
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


def _skipped_link(
    dirpath: str,
    name: str,
    rel_path_str: str,
    on_symlink: Callable[[str], None] | None,
) -> bool:
    """Whether the walk leaves this entry out as a link, reporting it if so."""
    if not is_symlink_entry(os.path.join(dirpath, name)):
        return False
    if on_symlink is not None:
        on_symlink(rel_path_str)
    return True


def _skipped_dir_link(
    dirpath: str,
    name: str,
    rel_path_str: str,
    on_symlink: Callable[[str], None] | None,
    repo_path: Path,
) -> bool:
    """Whether the walk leaves this folder out as a link or as a junction
    naming one outside the repository or on its way down, reporting it if
    so."""
    path = os.path.join(dirpath, name)
    if not (is_symlink_entry(path) or is_junction_left_out(path, repo_path)):
        return False
    if on_symlink is not None:
        on_symlink(rel_path_str)
    return True


def _skipped_name(
    name: str, rel_path_str: str, on_undecodable: Callable[[str], None] | None
) -> bool:
    """Whether the walk leaves this entry out by its name, reporting it if so."""
    if is_utf8_name(name):
        return False
    if on_undecodable is not None:
        on_undecodable(rel_path_str)
    return True


def _skipped_special(
    dirpath: str,
    name: str,
    rel_path_str: str,
    on_special: Callable[[str], None] | None,
) -> bool:
    """Whether the walk leaves this file out as a pipe, socket or device."""
    if not is_special_file_entry(os.path.join(dirpath, name)):
        return False
    if on_special is not None:
        on_special(rel_path_str)
    return True


def walk_eligible_files(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
    on_dir: Callable[[str, str], None] | None = None,
    on_symlink: Callable[[str], None] | None = None,
    on_special: Callable[[str], None] | None = None,
    on_undecodable: Callable[[str], None] | None = None,
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

    A symbolic link is never walked (`is_symlink_entry`), nor a Windows
    junction naming a folder outside the repository or one the walk entered
    on its way to it (`is_junction_left_out`),
    and ``on_symlink`` receives the repo-relative path of each one the ignore
    rules would otherwise have let through, file or directory. The same goes for a pipe,
    socket or device (`is_special_file_entry`, reported to ``on_special``)
    and for a file or directory whose name is not valid UTF-8
    (`is_utf8_name`, reported to ``on_undecodable``): only regular files the
    graph can name are yielded.
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
            and not _skipped_dir_link(
                dirpath, d, f"{dir_prefix}{d}", on_symlink, repo_path
            )
            and not _skipped_name(d, f"{dir_prefix}{d}", on_undecodable)
        )
        for fname in sorted(filenames):
            if fname in state_filenames:
                continue
            rel_path_str = f"{dir_prefix}{fname}"
            if (
                should_skip_rel_file(
                    rel_path_str,
                    dir_parts,
                    exclude_paths=exclude_paths,
                    unignore_paths=unignore_paths,
                )
                or _skipped_link(dirpath, fname, rel_path_str, on_symlink)
                or _skipped_name(fname, rel_path_str, on_undecodable)
                or _skipped_special(dirpath, fname, rel_path_str, on_special)
            ):
                continue
            yield dirpath, fname, rel_path_str


def rescued_files(
    repo_path: Path,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> frozenset[str]:
    """The files the walk indexes although a default-excluded name is on their path.

    The compiler frontends skip those names on their own, so they must be told
    which files under them the graph holds: a git-tracked `pkg/out/out.go` or
    a file a `!` line rescues otherwise reaches the graph without its compiler
    facts (review of PR 2490).
    """
    return frozenset(
        rel_path_str
        for _dirpath, _fname, rel_path_str in walk_eligible_files(
            repo_path, exclude_paths, unignore_paths
        )
        if any(
            part in cs.IGNORE_PATTERNS
            for part in rel_path_str.split(cs.SEPARATOR_SLASH)[:-1]
        )
    )


def frontend_ignored_dirs(rescued: frozenset[str]) -> frozenset[str]:
    """The default-excluded names a frontend that filters by name may skip whole.

    For gotypes and Roslyn, which can only drop a directory name everywhere:
    every name a rescued file sits under stays visible. They then also report
    on untracked files under that name, which is harmless, because every fact
    joins to a definition by the position the parse registered and a file the
    walk skipped registers none.
    """
    return cs.IGNORE_PATTERNS - {
        part for rel in rescued for part in rel.split(cs.SEPARATOR_SLASH)[:-1]
    }


def is_walked_dir(
    dir_parts: tuple[str, ...],
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Whether `walk_eligible_files` descends into this repo-relative directory.

    The walk prunes one level at a time, so a directory is reached only when
    every ancestor passes `should_keep_dir` too, under a name `is_utf8_name`
    accepts.
    """
    prefix = ""
    for part in dir_parts:
        if not is_utf8_name(part) or not should_keep_dir(
            part, prefix, exclude_paths, unignore_paths
        ):
            return False
        prefix = f"{prefix}{part}{cs.SEPARATOR_SLASH}"
    return True


def is_eligible_rel_file(
    rel_path_str: str,
    exclude_paths: frozenset[str] | None = None,
    unignore_paths: frozenset[str] | None = None,
) -> bool:
    """Whether `walk_eligible_files` would yield this repo-relative file.

    For consumers that see one path at a time, such as the real-time watcher.
    Restating the rule there let the two disagree: the watcher checked
    directory names against the built-in ignores only, so it dropped every
    change under a directory the run had unignored, and followed changes
    under one the user had excluded.
    """
    *dirs, filename = rel_path_str.split(cs.SEPARATOR_SLASH)
    if filename in cs.CGR_STATE_FILENAMES or not is_utf8_name(filename):
        return False
    dir_parts = tuple(dirs)
    if not is_walked_dir(dir_parts, exclude_paths, unignore_paths):
        return False
    return not should_skip_rel_file(
        rel_path_str,
        dir_parts,
        exclude_paths=exclude_paths,
        unignore_paths=unignore_paths,
    )


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


def root_proven_missing(root_path: str) -> bool:
    """Whether a project root is provably gone, rather than unreadable.

    Asked by `cgr prune` before it destroys a project (#2479): the cost of a
    wrong answer is not a stale node but a lost graph. ``FileNotFoundError``
    and ``NotADirectoryError`` establish absence -- a parent component that is
    now a file makes the old directory unreachable by construction -- and a
    stat that succeeds on a non-directory proves the checkout is gone too:
    the recorded root is a path that no longer holds one. Every other
    ``OSError`` (an EACCES on a protected parent, an EIO or ESTALE from a
    mounted filesystem) leaves the question open, and open means live: such
    a project is never a prune candidate. The stat is unbounded -- a
    hard-mounted stale server hangs rather than raises, so the listing waits
    on the mount instead of guessing.
    """
    try:
        st = os.stat(root_path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return not stat.S_ISDIR(st.st_mode)


def project_root_for_qualified_name(
    qualified_name: str, roots: dict[str, str | None]
) -> Path | None:
    matches = [
        name
        for name in roots
        if qualified_name == name or qualified_name.startswith(name + ".")
    ]
    if not matches:
        return None
    owner = matches[0]
    for name in matches[1:]:
        if len(name) > len(owner):
            owner = name
    root = roots[owner]
    return Path(root).resolve() if root is not None else None


def projects_sharing_root(project_name: str, roots: dict[str, str | None]) -> list[str]:
    """Other projects indexed from the same directory as `project_name`.

    Folder and File nodes are keyed by absolute path, so such projects hold
    the same ones (issue #2665).
    """
    root = roots.get(project_name)
    if root is None:
        return []
    resolved = Path(root).resolve()
    return [
        name
        for name, other in roots.items()
        if name != project_name
        and other is not None
        and Path(other).resolve() == resolved
    ]


def absolute_path_within_project_root(
    qualified_name: str, absolute_path: str, roots: dict[str, str | None]
) -> bool:
    """A stored absolute path may only be read from inside its own project's
    indexed root; projects with no recorded root (legacy graphs) stay
    readable (issue #425). Project names may contain dots, so the owning
    project is the longest known name prefixing the qualified name. The
    resolve() calls are load-bearing: containment is checked lexically, so
    an unresolved ``..`` segment or symlink would escape the root."""
    root = project_root_for_qualified_name(qualified_name, roots)
    if root is None:
        return True
    return Path(absolute_path).resolve().is_relative_to(root)


class SourceMiss(StrEnum):
    """Why a node's source file could not be located."""

    STALE_ROOT = "stale_root"
    OUTSIDE_ROOT = "outside_root"
    MISSING = "missing"


@dataclass(frozen=True)
class SourceLocation:
    """A node's source file, or why there is none. `project` and `root` name
    the owning project and its recorded root when one is known, so a stale
    root can be reported as such rather than as a missing file."""

    path: Path | None
    miss: SourceMiss | None = None
    project: str | None = None
    root: Path | None = None


def _owning_project(qualified_name: str, roots: dict[str, str | None]) -> str | None:
    owner = None
    for name in roots:
        if (qualified_name == name or qualified_name.startswith(name + ".")) and (
            owner is None or len(name) > len(owner)
        ):
            owner = name
    return owner


def locate_node_source(
    qualified_name: str,
    absolute_path: str | None,
    relative_path: str | Path,
    roots: dict[str, str | None],
    unowned_root: Path | None,
) -> SourceLocation:
    """The one fallback sequence both source readers use (issue #2123).

    The recorded absolute path is authoritative when it sits inside the
    owning project's root and is a file. Otherwise the relative path is
    joined to that root, or to `unowned_root` for a node no registered
    project owns; with no root at all the relative path is used as given,
    the legacy behaviour. A recorded root that is no longer a directory is
    reported as stale: the repository moved and must be re-indexed, and
    falling back to another root would read another project's file (#1881).
    """
    project = _owning_project(qualified_name, roots)
    recorded = roots.get(project) if project is not None else None
    owner_root = Path(recorded).resolve() if recorded is not None else None
    if (
        absolute_path
        and absolute_path_within_project_root(qualified_name, absolute_path, roots)
        and Path(absolute_path).is_file()
    ):
        return SourceLocation(Path(absolute_path), project=project, root=owner_root)
    if recorded is not None and owner_root is not None and not owner_root.is_dir():
        # Reported as recorded: resolving a vanished path rewrites it through
        # whatever the host maps it to, which is not what the user indexed.
        return SourceLocation(None, SourceMiss.STALE_ROOT, project, Path(recorded))
    base = owner_root or unowned_root
    # With no root the path is used exactly as the caller validated it.
    if base is None:
        candidate = (
            Path(relative_path) if isinstance(relative_path, str) else relative_path
        )
    else:
        candidate = (base / relative_path).resolve()
    if base is not None and not candidate.is_relative_to(base):
        return SourceLocation(None, SourceMiss.OUTSIDE_ROOT, project, owner_root)
    if not candidate.is_file():
        return SourceLocation(None, SourceMiss.MISSING, project, owner_root)
    return SourceLocation(candidate, project=project, root=owner_root)
