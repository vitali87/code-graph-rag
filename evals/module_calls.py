"""L2 module-call attribution: does cgr attribute the right calls to the module?

The L3 trace (calls_trace) records the innermost *function* frame as the caller
and drops `<module>` frames, so it is structurally blind to module-level call
attribution. This eval fills that gap with a sound AST oracle: a call is
module-attributed iff it runs at module-load time -- a top-level statement, a
decorator, or a default-argument expression -- i.e. it is NOT inside a function
body. Both sides are compared as (module_file, callee_simple_name) name-edges,
restricted to first-party callees (names defined somewhere in the target) and
excluding dunders, since cgr only emits first-party CALLS and resolves
constructors to `__init__`.
"""

import ast
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from codebase_rag import constants as cs

from . import constants as ec
from .ast_oracle import _iter_py_files
from .cgr_graph import _capture
from .types_defs import NameEdge, NodeKey

console = Console()

_CALLS = cs.RelationshipType.CALLS.value


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _ModuleCallVisitor(ast.NodeVisitor):
    # (H) Collect callee names of calls that execute at module-load time. A
    # (H) function's decorators and argument defaults run in the enclosing scope,
    # (H) so they are visited at the current depth; only its body is function
    # (H) scope. Class bodies execute at definition time, so they stay at the
    # (H) enclosing depth (their method bodies are entered as functions).
    def __init__(self) -> None:
        self.names: set[str] = set()
        self._func_depth = 0

    def visit_Call(self, node: ast.Call) -> None:
        if self._func_depth == 0 and (name := _callee_name(node.func)):
            self.names.add(name)
        self.generic_visit(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self._func_depth += 1
        for stmt in node.body:
            self.visit(stmt)
        self._func_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)


def _first_party_names(trees: list[ast.Module]) -> set[str]:
    names: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
    return names


def oracle_module_calls(target: Path, project_name: str) -> set[NameEdge]:
    parsed: list[tuple[str, ast.Module]] = []
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            parsed.append((rel, ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
    first_party = _first_party_names([tree for _rel, tree in parsed])

    edges: set[NameEdge] = set()
    for rel, tree in parsed:
        visitor = _ModuleCallVisitor()
        visitor.visit(tree)
        module_key = NodeKey(cs.NodeLabel.MODULE.value, rel, ec.MODULE_START_LINE)
        for name in visitor.names:
            if name in first_party and not _is_dunder(name):
                edges.add(NameEdge(_CALLS, module_key, name))
    return edges


def cgr_module_calls(target: Path, project_name: str) -> set[NameEdge]:
    ingestor = _capture(target, project_name)
    module_label = cs.NodeLabel.MODULE.value
    module_paths: dict[str, str] = {
        str(uid): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if label == module_label
        and props.get(cs.KEY_PATH)
        and str(props[cs.KEY_PATH]).endswith(ec.PY_SUFFIX)
    }

    edges: set[NameEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _CALLS or from_label != module_label:
            continue
        path = module_paths.get(str(from_val))
        if path is None:
            continue
        segments = str(to_val).split(ec.SEP)
        name = segments[-1]
        # (H) A constructor call `X()` resolves to `X.__init__`; the oracle sees
        # (H) the class name `X`, so credit it to the class, not the dunder.
        if name == ec.INIT_STEM and len(segments) >= 2:
            name = segments[-2]
        if _is_dunder(name):
            continue
        module_key = NodeKey(module_label, path, ec.MODULE_START_LINE)
        edges.add(NameEdge(_CALLS, module_key, name))
    return edges


def score_module_calls(
    cgr: set[NameEdge], oracle: set[NameEdge]
) -> tuple[int, int, int, float, float]:
    tp = len(cgr & oracle)
    fp = len(cgr - oracle)
    fn = len(oracle - cgr)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return tp, fp, fn, precision, recall


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate module-call attribution for.")
    ] = Path(ec.DEFAULT_TARGET),
    project_name: Annotated[str, typer.Option(help="cgr project name.")] = "",
) -> None:
    target = target.resolve()
    project = project_name or target.name

    logger.info("Building cgr module-call edges for {}", target)
    cgr = cgr_module_calls(target, project)
    logger.info("Building oracle module-call edges for {}", target)
    oracle = oracle_module_calls(target, project)

    tp, fp, fn, precision, recall = score_module_calls(cgr, oracle)
    table = Table(title="cgr L2 module-call attribution (ast oracle ground truth)")
    for col in ("tp", "fp", "fn", "precision", "recall"):
        table.add_column(col, justify="right")
    table.add_row(str(tp), str(fp), str(fn), f"{precision:.4f}", f"{recall:.4f}")
    console.print(table)


if __name__ == "__main__":
    typer.run(main)
