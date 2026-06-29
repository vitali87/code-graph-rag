from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import (
    GraphData,
    OracleEdge,
    OracleNameEdge,
    OracleNodeRef,
    OraclePayload,
    OracleRecord,
)
from ._common import is_ignored, payload_to_graph

if TYPE_CHECKING:
    from clang.cindex import Cursor

# (H) The libclang oracle is authoritative C/C++ ground truth: driven by a
# (H) compile_commands.json it resolves #includes and expands macros to the true
# (H) translation-unit AST, which tree-sitter (cgr's parser) cannot do. cgr's
# (H) C/C++ nodes are graded against it on (kind, file, start_line).

_CLASS = cs.NodeLabel.CLASS.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_MODULE = cs.NodeLabel.MODULE.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value
_BASE_SPECIFIER = "CXX_BASE_SPECIFIER"

_NodeId = tuple[str, str, int]
_EdgeId = tuple[str, str, int, str, int]
_NameEdgeId = tuple[str, str, int, str]

# (H) libclang CursorKind members are registered dynamically (not static class
# (H) attributes), so map by the kind's stable NAME string — exactly what
# (H) `cursor.kind.name` yields at runtime — instead of `ci.CursorKind.CLASS_DECL`.
_KIND_BY_NAME: dict[str, str] = {
    "CLASS_DECL": _CLASS,
    "STRUCT_DECL": _CLASS,
    "CLASS_TEMPLATE": _CLASS,
    "FUNCTION_DECL": _FUNCTION,
    "FUNCTION_TEMPLATE": _FUNCTION,
    "CXX_METHOD": _METHOD,
    "CONSTRUCTOR": _METHOD,
    "DESTRUCTOR": _METHOD,
    "CONVERSION_FUNCTION": _METHOD,
}


def cpp_available() -> bool:
    try:
        import clang.cindex as ci

        ci.Index.create()
    except Exception:
        return False
    return True


def _rel(path: str, root: Path) -> str | None:
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def run_cpp_oracle(target: Path) -> GraphData:
    import clang.cindex as ci

    root = target.resolve()
    db = ci.CompilationDatabase.fromDirectory(str(root))
    index = ci.Index.create()
    nodes: dict[_NodeId, OracleRecord] = {}
    edges: dict[_EdgeId, OracleEdge] = {}
    name_edges: dict[_NameEdgeId, OracleNameEdge] = {}

    for command in db.getAllCompileCommands():
        args = list(command.arguments)[1:]
        try:
            tu = index.parse(None, args=args)
        except ci.TranslationUnitLoadError:
            continue
        _walk(tu.cursor, root, nodes, edges, name_edges)

    payload = OraclePayload(
        nodes=list(nodes.values()),
        edges=list(edges.values()),
        name_edges=list(name_edges.values()),
    )
    return payload_to_graph(payload)


def _walk(
    cursor: Cursor,
    root: Path,
    nodes: dict[_NodeId, OracleRecord],
    edges: dict[_EdgeId, OracleEdge],
    name_edges: dict[_NameEdgeId, OracleNameEdge],
) -> None:
    for child in cursor.get_children():
        _emit(child, root, nodes, edges, name_edges)
        _walk(child, root, nodes, edges, name_edges)


def _base_simple_name(spelling: str) -> str:
    # (H) Mirror cgr's base-name normalization (extract_cgr_lang_graph): collapse
    # (H) `::` to `.` and take the last component, so the oracle and cgr agree on
    # (H) the inheritance target spelling.
    flat = spelling.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
    return flat.rsplit(cs.SEPARATOR_DOT, 1)[-1]


def _emit(
    cursor: Cursor,
    root: Path,
    nodes: dict[_NodeId, OracleRecord],
    edges: dict[_EdgeId, OracleEdge],
    name_edges: dict[_NameEdgeId, OracleNameEdge],
) -> None:
    if not cursor.is_definition():
        return
    kind = _KIND_BY_NAME.get(cursor.kind.name)
    if kind is None or cursor.location.file is None:
        return
    rel = _rel(cursor.location.file.name, root)
    if rel is None:
        return
    line = cursor.location.line
    key: _NodeId = (kind, rel, line)
    if key not in nodes:
        nodes[key] = OracleRecord(
            kind=kind,
            file=rel,
            line=line,
            name=cursor.spelling,
            end_line=cursor.extent.end.line,
        )

    if kind == _METHOD:
        parent = cursor.semantic_parent
        if parent is None or parent.location.file is None:
            return
        prel = _rel(parent.location.file.name, root)
        if prel is not None:
            _add_edge(edges, _DEFINES_METHOD, _CLASS, prel, parent.location.line, key)
        return

    _add_edge(edges, _DEFINES, _MODULE, rel, ec.MODULE_START_LINE, key)
    if kind == _CLASS:
        for child in cursor.get_children():
            if child.kind.name != _BASE_SPECIFIER:
                continue
            base = _base_simple_name(child.type.spelling)
            nk: _NameEdgeId = (_INHERITS, rel, line, base)
            if nk not in name_edges:
                name_edges[nk] = OracleNameEdge(
                    rel=_INHERITS,
                    source=OracleNodeRef(kind=_CLASS, file=rel, line=line),
                    target_name=base,
                )


_FUNCTION_DECL = "FUNCTION_DECL"
_CALL_EXPR = "CALL_EXPR"


def _capture_path(command: tuple[str, ...]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        out = subprocess.run(
            command, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return out or None


def _clang_system_args() -> list[str]:
    # (H) Resolve the SDK system headers and clang's own builtin headers
    # (H) (stdarg.h, stddef.h) so a translation unit parses fully without a
    # (H) compile_commands.json. Best-effort and portable: each probe is skipped
    # (H) when its tool is absent (e.g. no SDK on Linux, headers found on PATH).
    args: list[str] = []
    if sdk := _capture_path(ec.XCRUN_SDK_PATH_CMD):
        args.extend((ec.CLANG_ISYSROOT_FLAG, sdk))
    if resource := _capture_path(ec.CLANG_RESOURCE_DIR_CMD):
        args.extend((ec.CLANG_ISYSTEM_FLAG, str(Path(resource) / ec.CLANG_INCLUDE_DIR)))
    return args


def _c_include_args(root: Path) -> list[str]:
    # (H) Every dir holding a header becomes an -I path so first-party #includes
    # (H) resolve without a compile database.
    dirs = {root}
    for header in root.rglob(ec.C_HEADER_GLOB):
        dirs.add(header.parent)
    args: list[str] = []
    for directory in sorted(dirs):
        args.extend((ec.CLANG_INCLUDE_FLAG, str(directory)))
    return args


def _collect_c_decls_and_calls(
    cursor: Cursor,
    root: Path,
    declared: set[str],
    raw_calls: list[tuple[str, str]] | None,
) -> None:
    # (H) raw_calls is None for an unclean translation unit: its AST may be
    # (H) truncated by a missing header, so its call sites are not authoritative
    # (H) and only its (reliable) definitions are harvested into `declared`.
    for child in cursor.get_children():
        file = child.location.file
        rel = _rel(file.name, root) if file else None
        # (H) Prune non-first-party subtrees (system/library headers): they are
        # (H) never graded and walking them is the dominant cost.
        if rel is None or is_ignored(rel):
            continue
        if child.kind.name == _FUNCTION_DECL and child.is_definition():
            declared.add(child.spelling)
        elif raw_calls is not None and child.kind.name == _CALL_EXPR and child.spelling:
            raw_calls.append((rel, child.spelling))
        _collect_c_decls_and_calls(child, root, declared, raw_calls)


def run_c_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str], frozenset[str]]:
    # (H) File-level C call sites restricted to first-party callees (a callee whose
    # (H) name is a first-party defined function), the declared name universe, and
    # (H) the set of cleanly-parsed source files. libclang resolves the true call
    # (H) graph (independent of cgr's tree-sitter C frontend). Each .c file is
    # (H) parsed directly (no compile_commands.json); C has no overloading, so a
    # (H) simple name is unambiguous. A file whose TU emits an error diagnostic
    # (H) (a missing build-generated header) is not authoritative, so it is left
    # (H) out of the covered set and the cgr side is held to the same files.
    import clang.cindex as ci

    root = target.resolve()
    index = ci.Index.create()
    base_args = [ec.CLANG_C_STD, *_clang_system_args(), *_c_include_args(root)]
    declared: set[str] = set()
    raw_calls: list[tuple[str, str]] = []
    covered: set[str] = set()
    for source in sorted(root.rglob(ec.C_SOURCE_GLOB)):
        rel = _rel(str(source), root)
        if rel is None or is_ignored(rel):
            continue
        try:
            tu = index.parse(str(source), args=base_args)
        except ci.TranslationUnitLoadError:
            continue
        clean = not any(
            diag.severity >= ec.CLANG_SEVERITY_ERROR for diag in tu.diagnostics
        )
        _collect_c_decls_and_calls(
            tu.cursor, root, declared, raw_calls if clean else None
        )
        if clean:
            covered.add(rel)
    declared_names = frozenset(declared)
    covered_files = frozenset(covered)
    edges = {
        (file, name)
        for file, name in raw_calls
        if name in declared_names and file in covered_files
    }
    return edges, declared_names, covered_files


def _add_edge(
    edges: dict[_EdgeId, OracleEdge],
    rel: str,
    pkind: str,
    pfile: str,
    pline: int,
    child: _NodeId,
) -> None:
    ckind, cfile, cline = child
    ek: _EdgeId = (rel, pfile, pline, cfile, cline)
    if ek in edges:
        return
    edges[ek] = OracleEdge(
        rel=rel,
        parent=OracleNodeRef(kind=pkind, file=pfile, line=pline),
        child=OracleNodeRef(kind=ckind, file=cfile, line=cline),
    )
