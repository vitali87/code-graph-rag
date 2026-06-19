import ast
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .types_defs import DefNode, EdgeKey, GraphData, NameEdge, NodeKey

_MODULE = cs.NodeLabel.MODULE.value
_CLASS = cs.NodeLabel.CLASS.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value


def extract_oracle_graph(target: Path) -> GraphData:
    nodes: dict[NodeKey, DefNode] = {}
    edges: set[EdgeKey] = set()
    name_edges: set[NameEdge] = set()
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
            continue
        module_key = NodeKey(_MODULE, rel, ec.MODULE_START_LINE)
        nodes[module_key] = DefNode(module_key, path.stem, 0)
        _walk_scope(tree.body, _MODULE, module_key, rel, nodes, edges, name_edges)
    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)


def _base_name(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript):
        return _base_name(expr.value)
    return None


def _iter_py_files(target: Path) -> Iterator[Path]:
    for path in target.rglob(f"*{ec.PY_SUFFIX}"):
        parts = path.relative_to(target).parts
        if set(parts) & ec.IGNORE_DIRS:
            continue
        if any(part.endswith(ec.EGG_INFO_SUFFIX) for part in parts):
            continue
        yield path


def _end_line(node: ast.stmt) -> int:
    end = node.end_lineno
    return end if end is not None else node.lineno


def _child_stmts(node: ast.stmt) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list):
            out.extend(item for item in value if isinstance(item, ast.stmt))
        elif isinstance(value, ast.stmt):
            out.append(value)
    return out


def _walk_scope(
    stmts: list[ast.stmt],
    scope_kind: str,
    scope_key: NodeKey,
    rel: str,
    nodes: dict[NodeKey, DefNode],
    edges: set[EdgeKey],
    name_edges: set[NameEdge],
) -> None:
    for node in stmts:
        if isinstance(node, ast.ClassDef):
            key = NodeKey(_CLASS, rel, node.lineno)
            nodes[key] = DefNode(key, node.name, _end_line(node))
            if scope_kind == _MODULE:
                edges.add(EdgeKey(_DEFINES, scope_key, key))
            for base in node.bases:
                if base_name := _base_name(base):
                    name_edges.add(NameEdge(_INHERITS, key, base_name))
            _walk_scope(node.body, _CLASS, key, rel, nodes, edges, name_edges)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if scope_kind == _CLASS:
                key = NodeKey(_METHOD, rel, node.lineno)
                nodes[key] = DefNode(key, node.name, _end_line(node))
                edges.add(EdgeKey(_DEFINES_METHOD, scope_key, key))
            else:
                key = NodeKey(_FUNCTION, rel, node.lineno)
                nodes[key] = DefNode(key, node.name, _end_line(node))
                if scope_kind == _MODULE:
                    edges.add(EdgeKey(_DEFINES, scope_key, key))
            _walk_scope(node.body, _FUNCTION, key, rel, nodes, edges, name_edges)
        else:
            _walk_scope(
                _child_stmts(node), scope_kind, scope_key, rel, nodes, edges, name_edges
            )
