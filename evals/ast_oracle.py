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
_IMPORTS = cs.RelationshipType.IMPORTS.value


def extract_oracle_graph(target: Path, project_name: str) -> GraphData:
    nodes: dict[NodeKey, DefNode] = {}
    edges: set[EdgeKey] = set()
    name_edges: set[NameEdge] = set()

    parsed: list[tuple[str, ast.Module]] = []
    module_index: dict[str, str] = {}
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
            continue
        parsed.append((rel, tree))
        module_index[_module_dotted(rel, project_name)] = rel

    for rel, tree in parsed:
        module_key = NodeKey(_MODULE, rel, ec.MODULE_START_LINE)
        nodes[module_key] = DefNode(module_key, Path(rel).stem, 0)
        _walk_scope(tree.body, _MODULE, module_key, rel, nodes, edges, name_edges)
        for target_file in _import_targets(tree, rel, module_index, project_name):
            name_edges.add(NameEdge(_IMPORTS, module_key, target_file))

    return GraphData(nodes=nodes, edges=edges, name_edges=name_edges)


def _module_dotted(rel: str, project_name: str) -> str:
    parts = list(Path(rel).with_suffix("").parts)
    if parts and parts[-1] == ec.INIT_STEM:
        parts = parts[:-1]
    return cs.SEPARATOR_DOT.join([project_name, *parts])


def _from_base_parts(node: ast.ImportFrom, pkg_parts: list[str]) -> list[str] | None:
    if node.level == 0:
        return node.module.split(cs.SEPARATOR_DOT) if node.module else None
    keep = len(pkg_parts) - (node.level - 1)
    if keep < 0:
        return None
    parts = pkg_parts[:keep]
    if node.module:
        parts = parts + node.module.split(cs.SEPARATOR_DOT)
    return parts


def _lookup_module(
    dotted: str, module_index: dict[str, str], project_name: str
) -> str | None:
    if dotted in module_index:
        return module_index[dotted]
    # (H) A src-root distribution (setup.py maps src/ to the package named
    # (H) after the project) writes imports against the DISTRIBUTION name
    # (H) (`thrift.Thrift`) while the index keys are path-based
    # (H) (`thrift.src.Thrift`). An import claiming the project's own
    # (H) top-level name must be internal; a UNIQUE whole-segment suffix
    # (H) match recovers the file, ambiguity resolves nothing.
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    if not dotted.startswith(prefix):
        return None
    tail = dotted[len(prefix) :]
    if not tail:
        return None
    suffix = f"{cs.SEPARATOR_DOT}{tail}"
    matches = {rel for key, rel in module_index.items() if key.endswith(suffix)}
    if len(matches) == 1:
        return matches.pop()
    return None


def _import_targets(
    tree: ast.Module, rel: str, module_index: dict[str, str], project_name: str
) -> set[str]:
    pkg_parts = [project_name, *Path(rel).parent.parts]
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if target := _lookup_module(alias.name, module_index, project_name):
                    targets.add(target)
        elif isinstance(node, ast.ImportFrom):
            base_parts = _from_base_parts(node, pkg_parts)
            if base_parts is None:
                continue
            base_dotted = cs.SEPARATOR_DOT.join(base_parts)
            for alias in node.names:
                if alias.name == "*":
                    if target := _lookup_module(
                        base_dotted, module_index, project_name
                    ):
                        targets.add(target)
                    continue
                sub = cs.SEPARATOR_DOT.join([*base_parts, alias.name])
                if target := _lookup_module(sub, module_index, project_name):
                    targets.add(target)
                elif target := _lookup_module(
                    base_dotted, module_index, project_name
                ):
                    targets.add(target)
    return targets


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
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, ast.stmt):
                out.append(item)
            elif isinstance(item, ast.ExceptHandler | ast.match_case):
                # (H) except handlers and match cases are not ast.stmt but hold
                # (H) statement bodies that may define functions/classes.
                out.extend(s for s in item.body if isinstance(s, ast.stmt))
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
