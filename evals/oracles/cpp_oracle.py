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

# The libclang oracle is authoritative C/C++ ground truth: driven by a
# compile_commands.json it resolves #includes and expands macros to the true
# translation-unit AST, which tree-sitter (cgr's parser) cannot do. cgr's
# C/C++ nodes are graded against it on (kind, file, start_line).

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

# libclang CursorKind members are registered dynamically (not static class
# attributes), so map by the kind's stable NAME string — exactly what
# `cursor.kind.name` yields at runtime — instead of `ci.CursorKind.CLASS_DECL`.
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


_libclang_pinned = False


def _ensure_libclang() -> None:
    # Pin the libclang shared library BEFORE the first Index.create (libclang is
    # a global one-shot). Prefer a system libclang whose clang version matches the
    # active SDK's libc++ — required to parse C++ standard headers, which the
    # bundled pip wheel's older clang cannot. C parsing is unaffected by the
    # choice, so both the C and C++ oracles share one consistent toolchain.
    global _libclang_pinned
    if _libclang_pinned:
        return
    _libclang_pinned = True
    # clang is an optional dependency: if the bindings are absent this import
    # raises ModuleNotFoundError, so swallow it here and let cpp_available's own
    # try/except report the oracle as unavailable (returning False), rather than
    # letting the exception escape and break test collection / the CLI path.
    try:
        from clang.cindex import Config
    except Exception:
        return

    for candidate in ec.LIBCLANG_CANDIDATES:
        if Path(candidate).exists():
            try:
                Config.set_library_file(candidate)
                return
            except Exception:
                # libclang loading raises a wide, unpredictable range of errors
                # (arch mismatch, format errors, an already-loaded library); on
                # any, fall through to the next candidate, else the bundled
                # default the bindings load on their own.
                continue


def cpp_available() -> bool:
    _ensure_libclang()
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
    _ensure_libclang()
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
    # Mirror cgr's base-name normalization (extract_cgr_lang_graph): collapse
    # `::` to `.` and take the last component, so the oracle and cgr agree on
    # the inheritance target spelling.
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
_FUNCTION_TEMPLATE = "FUNCTION_TEMPLATE"
_CXX_METHOD = "CXX_METHOD"
_CALL_EXPR = "CALL_EXPR"
# C: only free functions are first-party callees. C++: free functions (incl.
# templates) plus member functions; constructors/destructors are excluded
# because cgr models object creation as INSTANTIATES, not CALLS.
_C_DECL_KINDS = frozenset({_FUNCTION_DECL})
_CPP_DECL_KINDS = frozenset({_FUNCTION_DECL, _FUNCTION_TEMPLATE, _CXX_METHOD})


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
    # Resolve the SDK system headers and clang's own builtin headers
    # (stdarg.h, stddef.h) so a translation unit parses fully without a
    # compile_commands.json. Best-effort and portable: each probe is skipped
    # when its tool is absent (e.g. no SDK on Linux, headers found on PATH).
    args: list[str] = []
    if sdk := _capture_path(ec.XCRUN_SDK_PATH_CMD):
        args.extend((ec.CLANG_ISYSROOT_FLAG, sdk))
    if resource := _capture_path(ec.CLANG_RESOURCE_DIR_CMD):
        args.extend((ec.CLANG_ISYSTEM_FLAG, str(Path(resource) / ec.CLANG_INCLUDE_DIR)))
    return args


def _c_include_args(root: Path) -> list[str]:
    # Every dir holding a header becomes an -I path so first-party #includes
    # resolve without a compile database.
    dirs = {root}
    for header in root.rglob(ec.C_HEADER_GLOB):
        rel = _rel(str(header), root)
        if rel is not None and not is_ignored(rel):
            dirs.add(header.parent)
    args: list[str] = []
    for directory in sorted(dirs):
        args.extend((ec.CLANG_INCLUDE_FLAG, str(directory)))
    return args


def _callee_is_first_party(call: Cursor, root: Path) -> bool:
    # libclang resolves a call to its callee declaration; grade the call only
    # when that declaration is itself first-party. Without this, a call whose
    # simple name collides with a first-party symbol (e.g. `std::string::size`
    # vs a project `size()`) would be counted as a first-party edge, understating
    # cgr recall against calls it correctly resolves as external/builtin. C++'s
    # large STL surface (size/data/empty/clear/...) makes this collision common.
    ref = call.referenced
    if ref is None or ref.location.file is None:
        return False
    cref = _rel(ref.location.file.name, root)
    return cref is not None and not is_ignored(cref)


def _collect_decls_and_calls(
    cursor: Cursor,
    root: Path,
    declared: set[str],
    raw_calls: list[tuple[str, str]] | None,
    decl_kinds: frozenset[str],
    strict_callee: bool = False,
) -> None:
    # raw_calls is None for an unclean translation unit: its AST may be
    # truncated by a missing header, so its call sites are not authoritative
    # and only its (reliable) definitions are harvested into `declared`.
    for child in cursor.get_children():
        file = child.location.file
        rel = _rel(file.name, root) if file else None
        # Prune non-first-party subtrees (system/library headers): they are
        # never graded and walking them is the dominant cost.
        if rel is None or is_ignored(rel):
            continue
        if child.kind.name in decl_kinds and child.is_definition():
            declared.add(child.spelling)
        elif (
            raw_calls is not None
            and child.kind.name == _CALL_EXPR
            and child.spelling
            and (not strict_callee or _callee_is_first_party(child, root))
        ):
            raw_calls.append((rel, child.spelling))
        _collect_decls_and_calls(
            child, root, declared, raw_calls, decl_kinds, strict_callee
        )


def run_c_call_oracle(
    target: Path,
) -> tuple[set[tuple[str, str]], frozenset[str], frozenset[str]]:
    # File-level C call sites restricted to first-party callees (a callee whose
    # name is a first-party defined function), the declared name universe, and
    # the set of cleanly-parsed source files. libclang resolves the true call
    # graph (independent of cgr's tree-sitter C frontend). Each .c file is
    # parsed directly (no compile_commands.json); C has no overloading, so a
    # simple name is unambiguous. A file whose TU emits an error diagnostic
    # (a missing build-generated header) is not authoritative, so it is left
    # out of the covered set and the cgr side is held to the same files.
    _ensure_libclang()
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
        _collect_decls_and_calls(
            tu.cursor, root, declared, raw_calls if clean else None, _C_DECL_KINDS
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


def _cpp_system_args() -> list[str]:
    # Like _clang_system_args but for C++: the SDK's libc++ headers must precede
    # the clang builtin resource headers, else libc++'s <cstddef> resolves the C
    # <stddef.h> first and the parse fails. isysroot supplies the platform C
    # library; the resource dir supplies clang builtins (stdarg.h, stddef.h).
    args: list[str] = []
    if sdk := _capture_path(ec.XCRUN_SDK_PATH_CMD):
        args.extend((ec.CLANG_ISYSROOT_FLAG, sdk))
        args.extend((ec.CLANG_ISYSTEM_FLAG, str(Path(sdk) / ec.CLANG_LIBCXX_SUBPATH)))
    if resource := _capture_path(ec.CLANG_RESOURCE_DIR_CMD):
        args.extend((ec.CLANG_ISYSTEM_FLAG, str(Path(resource) / ec.CLANG_INCLUDE_DIR)))
    return args


def _cpp_include_args(root: Path) -> list[str]:
    # Root and a conventional include/ root plus every dir holding a C++ header
    # become -I paths so first-party #includes resolve without a compile database.
    dirs = {root, root / ec.CLANG_INCLUDE_DIR}
    for glob in ec.CPP_HEADER_GLOBS:
        for header in root.rglob(glob):
            rel = _rel(str(header), root)
            if rel is not None and not is_ignored(rel):
                dirs.add(header.parent)
    args: list[str] = []
    for directory in sorted(dirs):
        if directory.exists():
            args.extend((ec.CLANG_INCLUDE_FLAG, str(directory)))
    return args


def run_cpp_call_oracle(
    target: Path,
    extra_defines: tuple[str, ...] = (),
) -> tuple[set[tuple[str, str]], frozenset[str], frozenset[str]]:
    # File-level C++ call sites restricted to first-party callees (free functions
    # and member functions), the declared name universe, and the cleanly-parsed
    # source files. libclang resolves the true translation-unit call graph
    # (independent of cgr's tree-sitter C++ frontend). Overloads collapse under
    # the (file, simple-name) metric, so they need no disambiguation. extra_defines
    # carries corpus-specific platform macros (e.g. LEVELDB_PLATFORM_POSIX) that a
    # build system would normally supply; a TU that still errors abstains.
    _ensure_libclang()
    import clang.cindex as ci

    root = target.resolve()
    index = ci.Index.create()
    defines = [ec.CLANG_DEFINE_FLAG + d for d in extra_defines]
    base_args = [
        ec.CLANG_CPP_LANG_FLAG,
        ec.CLANG_CPP_LANG,
        ec.CLANG_CPP_STD,
        *defines,
        *_cpp_system_args(),
        *_cpp_include_args(root),
    ]
    declared: set[str] = set()
    raw_calls: list[tuple[str, str]] = []
    covered: set[str] = set()
    for glob in ec.CPP_SOURCE_GLOBS:
        for source in sorted(root.rglob(glob)):
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
            _collect_decls_and_calls(
                tu.cursor,
                root,
                declared,
                raw_calls if clean else None,
                _CPP_DECL_KINDS,
                strict_callee=True,
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
