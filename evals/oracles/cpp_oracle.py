from __future__ import annotations

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
from ._common import payload_to_graph

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
