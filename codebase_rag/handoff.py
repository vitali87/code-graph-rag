"""The data the Python engine hands from one stage of an index to the next.

Phase 1 of the Rust rewrite (docs/reports/PHASE1_SEAMS.md, section 3). Most of
what `GraphUpdater.run`'s stages hand on are shared objects the stages keep
changing, so two points are written out, the two the Rust engine replaces
first: the file plan (`files.txt`), once discovery and change detection are
done, and the project index (`index.txt`), once the deferred definitions are
resolved. Each engine writes them, and the `both` engine mode compares them,
so a difference names the first stage at which the two engines part.

Each line is `kind<TAB>key<TAB>value`, the value JSON written the reference
graphs' way (sorted keys, no ASCII escaping, no spaces), in UTF-8 with `\\n`
line ends on every system. The first line names the file and the format
version. Nothing in them depends on the checkout's path, the hash seed or the
operating system: paths are relative to the repository root with `/`, and
every set is sorted. A list keeps its order only where the order means
something: the walk order of the files, which decides which of two same-stem
files claims a module's bare name (#1569), and a class's parents.

An empty set, map or list states no fact (Python's registries create some on
a mere lookup), so a simple name with no definitions, a module with no
imports and a class with no parents get no line.

From format version 2 the project index also holds Go's own tables
(docs/reports/PHASE3_GO.md, section 6), the facts Go's analysis keeps beside
the shared registries: each module's package clause, each method's
signature, each interface's embeds as written, each implementation pair with
who paired it and the builds it holds in, each free function's first result,
and each file's build constraint, written in the canonical text
`parsers/go/builds.py` gives it. A file that every build compiles states no
constraint and gets no line. The Rust engine writes the same from its index,
so a difference names the stage at which the two part.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from pathlib import Path

from . import constants as cs
from .canonical_graph import canonical_json
from .language_spec import get_language_for_extension
from .parsers.go.builds import ALWAYS, canonical_text
from .parsers.notebook import is_notebook_path
from .types_defs import GoProjectIndex, JsonValue, PlannedFile, ProjectIndex
from .utils.path_utils import cached_relative_path


def _line(kind: cs.HandoffKind, key: str, value: JsonValue) -> str:
    return cs.REFERENCE_FIELD_SEPARATOR.join((kind, key, canonical_json(value)))


def _write(directory: Path, file: cs.HandoffFile, lines: Iterable[str]) -> None:
    header = cs.HANDOFF_HEADER.format(
        file=file, version=cs.HANDOFF_FORMAT_VERSIONS[file]
    )
    text = "".join(f"{line}{cs.LINE_FEED}" for line in (header, *lines))
    directory.mkdir(parents=True, exist_ok=True)
    # Bytes, not text: text mode would end each line with `\r\n` on Windows.
    (directory / file).write_bytes(text.encode(cs.ENCODING_UTF8))


def clear_handoffs(directory: Path) -> None:
    """Remove the hand-offs an earlier run left in `directory`.

    A run writes each file only when it reaches that file's point, so one
    that returns early (already in sync) or stops between the two would
    otherwise leave files describing another run as if they were its own.
    """
    for file in cs.HandoffFile:
        (directory / file).unlink(missing_ok=True)


def write_file_plan(
    directory: Path,
    files: Iterable[PlannedFile],
    reparse: Mapping[str, cs.ReparseReason] | None = None,
    deleted: Iterable[str] | None = None,
) -> None:
    """Write `files.txt`: every eligible file in walk order, with the tier and
    language `_route` gives it and the hash the run records for it, then, on
    an incremental run, the files it re-parses and those it deletes, each
    sorted. A full build passes neither: it re-parses every file."""
    lines = [
        _line(
            cs.HandoffKind.FILE,
            file.path,
            {
                cs.HANDOFF_KEY_LANGUAGE: file.route.language,
                cs.HANDOFF_KEY_ROUTE: file.route.tier,
                cs.HANDOFF_KEY_MD5: file.md5,
            },
        )
        for file in files
    ]
    lines.extend(
        _line(cs.HandoffKind.REPARSE, path, {cs.HANDOFF_KEY_REASON: reason})
        for path, reason in sorted((reparse or {}).items())
    )
    lines.extend(
        _line(cs.HandoffKind.DELETED, path, {}) for path in sorted(deleted or ())
    )
    _write(directory, cs.HandoffFile.FILES, lines)


def module_language(path: Path) -> cs.SupportedLanguage | None:
    """The language a module's `index.txt` line names for its file."""
    # A notebook's cells are parsed as Python (issue #2480), as the call
    # resolver reads a module's language.
    if is_notebook_path(path):
        return cs.SupportedLanguage.PYTHON
    return get_language_for_extension(path.suffix)


def module_line(module_qn: str, path: str, language: str | None) -> str:
    """A module's line in `index.txt`: its qualified name, then the language
    and the path from the root of the file that claimed it."""
    return _line(
        cs.HandoffKind.MODULE,
        module_qn,
        {cs.HANDOFF_KEY_LANGUAGE: language, cs.HANDOFF_KEY_PATH: path},
    )


def _go_lines(
    repo_path: Path,
    modules: Mapping[str, Path],
    definitions: Set[str],
    go: GoProjectIndex,
) -> list[str]:
    """The lines of Go's own tables, one per entry, each pair once for each
    side that paired it, and a build constraint for each Go file that has
    one.

    Of the modules and definitions the index holds only. Python's tables
    keep what a deleted file declared on a reused updater (the watcher's, the
    MCP server's): its clause, its methods and interfaces, under the qns the
    file gave them, though those left the index with the file. A fresh index
    holds none of it, and the Rust engine re-reads every Go file each run.
    """
    lines = [
        _line(cs.HandoffKind.GO_PACKAGE, module_qn, {cs.HANDOFF_KEY_CLAUSE: clause})
        for module_qn, clause in go.packages.items()
        if module_qn in modules
    ]
    lines.extend(
        _line(
            cs.HandoffKind.GO_METHOD,
            method_qn,
            {
                cs.HANDOFF_KEY_FILE: spec.file,
                cs.HANDOFF_KEY_PACKAGE: spec.package,
                cs.HANDOFF_KEY_PARAMS: [*spec.signature.params],
                cs.HANDOFF_KEY_RESULTS: None
                if spec.signature.results is None
                else [*spec.signature.results],
            },
        )
        for method_qn, spec in go.methods.items()
        if method_qn in definitions
    )
    lines.extend(
        _line(
            cs.HandoffKind.GO_INTERFACE,
            interface_qn,
            {
                cs.HANDOFF_KEY_EMBEDS: [*decl.embeds],
                cs.HANDOFF_KEY_FILE: decl.file,
                cs.HANDOFF_KEY_PACKAGE: decl.package,
            },
        )
        for interface_qn, decl in go.interfaces.items()
        if interface_qn in definitions
    )
    sources = (
        (cs.GoPairSource.PROVEN, go.proven),
        (cs.GoPairSource.GUESSED, go.guessed),
    )
    lines.extend(
        _line(
            cs.HandoffKind.GO_IMPLEMENTS,
            implementer,
            {
                cs.HANDOFF_KEY_BUILDS: canonical_text(
                    go.pair_builds.get((interface_qn, implementer), ALWAYS)
                ),
                cs.HANDOFF_KEY_INTERFACE: interface_qn,
                cs.HANDOFF_KEY_SOURCE: source,
            },
        )
        for source, pairs in sources
        for interface_qn, implementer in pairs
    )
    lines.extend(
        _line(cs.HandoffKind.GO_RETURN_TYPE, qn, {cs.HANDOFF_KEY_TYPE: return_type})
        for qn, return_type in go.return_types.items()
        if qn in definitions
    )
    go_files = {
        cached_relative_path(path, repo_path).as_posix()
        for path in modules.values()
        if module_language(path) == cs.SupportedLanguage.GO
    }
    for rel_path in go_files:
        if (constraint := go.builds.constraint(rel_path)) != ALWAYS:
            lines.append(
                _line(
                    cs.HandoffKind.GO_BUILD,
                    rel_path,
                    {cs.HANDOFF_KEY_CONSTRAINT: canonical_text(constraint)},
                )
            )
    return lines


def write_project_index(directory: Path, repo_path: Path, index: ProjectIndex) -> None:
    """Write `index.txt`: the modules, definitions, simple names, imports and
    parents the run registered, and Go's own tables, every line sorted."""
    lines = [
        module_line(
            module_qn,
            cached_relative_path(path, repo_path).as_posix(),
            module_language(path),
        )
        for module_qn, path in index.modules.items()
    ]
    definitions = dict(index.definitions)
    lines.extend(
        _line(cs.HandoffKind.DEFINITION, qn, {cs.HANDOFF_KEY_LABEL: label})
        for qn, label in definitions.items()
    )
    for name, qns in index.simple_names.items():
        if found := sorted(qns):
            lines.append(_line(cs.HandoffKind.SIMPLE_NAME, name, [*found]))
    for module_qn, mapping in index.imports.items():
        if mapping:
            lines.append(_line(cs.HandoffKind.IMPORTS, module_qn, {**mapping}))
    for class_qn, parents in index.parents.items():
        if parents:
            lines.append(_line(cs.HandoffKind.INHERITS, class_qn, [*parents]))
    lines.extend(_go_lines(repo_path, index.modules, definitions.keys(), index.go))
    _write(directory, cs.HandoffFile.INDEX, sorted(lines))
