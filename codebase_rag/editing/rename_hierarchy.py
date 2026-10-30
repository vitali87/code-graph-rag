"""What a rename takes along beyond the symbol asked for (issue #1532):
the other parts of a C# partial type, the methods it overrides or is
overridden by, and each other build's declaration of a Go name, plus the Go
interface whose implementers cgr cannot tell, which refuses the rename.
Split from `rename.py`.
"""

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..language_spec import csharp_partial_key_from_graph, get_language_for_extension
from ..parsers.go import utils as go_utils
from ..parsers.go.builds import GoBuilds
from ..parsers.go.module_paths import read_package_clause
from ..types_defs import QueryFn, ResultRow
from ..utils import qn_markers
from .sites import Hierarchy, hierarchy


def rename_hierarchy(
    fetch_all: QueryFn, project: str, qn: str, repo_root: Path | None = None
) -> Hierarchy:
    """`qn`, every other part of the C# partial type `qn` is a part of, and
    every method `qn` overrides or is overridden by, transitively, with the
    members only a guessed OVERRIDES edge reaches."""
    parts = _csharp_partial_parts(fetch_all, project, qn)
    found = hierarchy(
        fetch_all,
        project,
        qn,
        lambda member: (
            _go_build_variants(fetch_all, project, member, repo_root)
            if repo_root is not None
            else []
        ),
    )
    members = list(dict.fromkeys([qn, *parts, *found.members]))
    return Hierarchy(members, found.guessed - set(parts))


def _go_build_variants(
    fetch_all: QueryFn, project: str, qn: str, repo_root: Path
) -> list[str]:
    """The other builds' declarations of Go method or function `qn`.

    A package declares a name once per build (`func (T) M()` in `t_linux.go`
    and in `t_windows.go`, `M()` in `i_linux.go`'s and `i_windows.go`'s
    `I`), and a caller every build compiles calls each build's. Renaming
    one left the others, and the builds holding them no longer compiled
    (#3360). A variant is a Go declaration of the same kind and name
    (a method of the same-named receiver, or a function) in the same
    package: its directory and `package` clause (`p_test` beside `p` is
    another), seen from the same files (a `_test.go` file's from test files
    alone), in a file no build compiles with `qn`'s. A Python `helper`
    beside a Go one, another package's, or a twin a build could compile
    together with it (which would not compile) is none."""
    definition = graph_query.definition(fetch_all, project, qn, None)
    path, name, label = definition["path"], definition["name"], definition["label"]
    if (
        not path
        or not name
        or label not in (cs.NodeLabel.METHOD, cs.NodeLabel.FUNCTION)
        or not _is_go_path(path)
    ):
        return []
    place = _go_declaration_place(repo_root, path, qn, label)
    if place is None:
        return []
    builds = GoBuilds(repo_root)
    return sorted(
        other
        for symbol in graph_query.resolve(fetch_all, project, name)
        if (other := symbol["qualified_name"]) != qn
        and symbol["label"] == label
        and (other_path := symbol["path"])
        and other_path != path
        and _is_go_path(other_path)
        and _go_declaration_place(repo_root, other_path, other, label) == place
        and builds.apart((path, other_path))
    )


def go_implementers_unknown(
    fetch_all: QueryFn, project: str, members: list[str]
) -> tuple[str, str, str] | None:
    """A Go interface whose implementers cgr cannot tell that declares the
    name the Go methods among `members` share, where they could satisfy it,
    as (interface, why, name); None when there is none.

    Such an interface gets no IMPLEMENTS or OVERRIDES edge by name (#3360),
    so the hierarchy cannot show what renaming the name must take along:
    renaming one of its implementations leaves it and any other behind, and
    renaming its own method leaves every implementation. Any type, in any
    package, may satisfy it through an exported name; an unexported one only
    in the interface's own package, which shares its directory."""
    definitions = [
        graph_query.definition(fetch_all, project, member, None) for member in members
    ]
    methods = [
        found
        for found in definitions
        if found["label"] == cs.NodeLabel.METHOD
        and found["path"]
        and _is_go_path(found["path"])
    ]
    name = methods[0]["name"] if methods else None
    if not name:
        return None
    directories = {PurePosixPath(str(found["path"])).parent for found in methods}
    owns = graph_query.owner_check(fetch_all, project)
    rows = fetch_all(
        cq.CYPHER_GRAPH_GO_IMPLEMENTERS_UNKNOWN,
        {cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}", cs.KEY_NAME: name},
    )
    for row in sorted(rows, key=lambda row: str(row.get(cs.KEY_QUALIFIED_NAME))):
        interface = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
        directory = PurePosixPath(str(row.get(cs.KEY_PATH) or "")).parent
        if owns(interface) and (name[:1].isupper() or directory in directories):
            return interface, str(row.get(cs.KEY_IMPLEMENTERS_UNKNOWN) or ""), name
    return None


def _is_go_path(path: str) -> bool:
    return get_language_for_extension(Path(path).suffix) == cs.SupportedLanguage.GO


def _go_declaration_place(
    repo_root: Path, path: str, qn: str, label: str
) -> tuple[str, str] | None:
    # The files that see it (its package, `dir:clause`, or that package's
    # test files) and the name of its receiver type (a method's) or "" (a
    # function's). None when its `package` clause cannot be read.
    clause = read_package_clause(repo_root / path)
    if clause is None:
        return None
    owner = (
        qn_markers.natural_qn(qn)
        .rpartition(cs.SEPARATOR_DOT)[0]
        .rpartition(cs.SEPARATOR_DOT)[2]
        if label == cs.NodeLabel.METHOD
        else ""
    )
    package = go_utils.package_key(path, clause)
    return go_utils.declaration_scope(path, package), owner


def _csharp_partial_parts(fetch_all: QueryFn, project: str, qn: str) -> list[str]:
    """The other parts of the C# `partial` type `qn`; [] for anything else.

    Each file declaring a partial type holds its own node, joined into one
    type only while parsing, so renaming the part asked for alone left
    `partial class Order` in the other files as a second type holding the
    rest of the members, and the build broke (issue #2469). The parts are
    the ones parsing groups: declared `partial`, of one kind and name, in one
    directory under one namespace-qualified name.
    """
    definition = graph_query.definition(fetch_all, project, qn, None)
    path, name, label = definition["path"], definition["name"], definition["label"]
    if (
        not path
        or not name
        or label not in cs.CSHARP_PARTIAL_TYPE_LABELS
        or get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.CSHARP
    ):
        return []
    owns = graph_query.owner_check(fetch_all, project)
    rows = fetch_all(
        cs.CYPHER_SAME_NAMED_CSHARP_TYPES,
        {cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}", cs.KEY_NAME: name},
    )
    # `Box` and `Box<T>` are two types (issue #2579): the key spells the
    # arity, so a partial twin of another arity is not one of the parts.
    arity = next(
        (_generic_arity(row) for row in rows if row.get(cs.KEY_QUALIFIED_NAME) == qn),
        0,
    )
    key = csharp_partial_key_from_graph(qn, path, project, arity)
    parts = {
        part
        for row in rows
        if (part := _partial_part(row, label, project, key, owns)) is not None
    }
    # `qn` is among them only when it is declared `partial` itself; a type
    # that is not has no other parts, whatever shares its name.
    if key is None or qn not in parts:
        return []
    return sorted(parts - {qn})


def _partial_part(
    row: ResultRow,
    label: str,
    project: str,
    key: str | None,
    owns: Callable[[str], bool],
) -> str | None:
    """A same-named type row's qualified name, when it is this project's own
    `partial` declaration of the kind `label` in the partial group `key`."""
    part, part_path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
    modifiers = row.get(cs.KEY_MODIFIERS)
    if (
        not isinstance(part, str)
        or not isinstance(part_path, str)
        or not isinstance(modifiers, list)
        or cs.TS_CSHARP_MODIFIER_PARTIAL not in modifiers
        or row.get(cs.KEY_LABEL) != label
        or not owns(part)
    ):
        return None
    part_key = csharp_partial_key_from_graph(
        part, part_path, project, _generic_arity(row)
    )
    return part if part_key == key else None


def _generic_arity(row: ResultRow) -> int:
    """A C# type row's stored generic arity; 0 for a non-generic type."""
    arity = row.get(cs.KEY_GENERIC_ARITY)
    return arity if isinstance(arity, int) else 0
