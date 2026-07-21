# Static CALLS eval. Function-level call recall against an ast oracle that
# resolves only the calls a reader can resolve without type inference: a bare
# name call (foo()) whose target is a first-party function reached via a
# `from ... import foo` or a same-module top-level def. Each becomes a
# (caller_qn, callee_qn) edge. Method / attribute / dynamic calls need cgr's
# type inference and are out of scope, so only RECALL is graded: every
# statically-certain call must appear in cgr's CALLS graph (cgr resolving more
# than the oracle is expected, not a false positive). Independent of cgr's
# resolver -- it uses ast import resolution, not the function-registry trie.
import ast
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .ast_oracle import _from_base_parts, _iter_py_files, _module_dotted
from .cgr_graph import _capture
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.STATIC_CALLS_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)

CallEdge = tuple[str, str]


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _node_qn(node: ast.AST, module: str, parents: dict[ast.AST, ast.AST]) -> str:
    parts: list[str] = []
    cur: ast.AST | None = node
    while cur is not None and not isinstance(cur, ast.Module):
        if isinstance(cur, _SCOPE_NODES):
            parts.append(cur.name)
        cur = parents.get(cur)
    return cs.SEPARATOR_DOT.join([module, *reversed(parts)])


def _enclosing_function(
    node: ast.AST, parents: dict[ast.AST, ast.AST]
) -> ast.AST | None:
    cur = parents.get(node)
    while cur is not None and not isinstance(cur, ast.Module):
        if isinstance(cur, _FUNC_NODES):
            return cur
        cur = parents.get(cur)
    return None


def _decorator_calls(tree: ast.Module) -> set[ast.Call]:
    # Calls that live inside a decorator expression (@deco(...)). These are
    # decorator applications, not calls the decorated function makes, so cgr
    # emits no CALLS edge for them and the oracle must exclude them.
    calls: set[ast.Call] = set()
    for node in ast.walk(tree):
        if isinstance(node, _SCOPE_NODES):
            for decorator in node.decorator_list:
                for inner in ast.walk(decorator):
                    if isinstance(inner, ast.Call):
                        calls.add(inner)
    return calls


def _import_map(tree: ast.Module, rel: str, project: str) -> dict[str, str]:
    # local name -> resolved target qn for `from <first-party> import name`.
    pkg_parts = [project, *Path(rel).parent.parts]
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base_parts = _from_base_parts(node, pkg_parts)
        if not base_parts or base_parts[0] != project:
            continue
        for alias in node.names:
            if alias.name != ec.STAR_IMPORT:
                target = cs.SEPARATOR_DOT.join([*base_parts, alias.name])
                mapping[alias.asname or alias.name] = target
    return mapping


def oracle_static_calls(target: Path, project: str) -> set[CallEdge]:
    parsed: list[tuple[str, ast.Module]] = []
    defined: set[str] = set()
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
            continue
        parsed.append((rel, tree))
        module = _module_dotted(rel, project)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, _FUNC_NODES):
                defined.add(_node_qn(node, module, parents))

    edges: set[CallEdge] = set()
    for rel, tree in parsed:
        module = _module_dotted(rel, project)
        parents = _parents(tree)
        imports = _import_map(tree, rel, project)
        decorator_calls = _decorator_calls(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node in decorator_calls:
                continue
            enclosing = _enclosing_function(node, parents)
            if enclosing is None:
                continue
            name = node.func.id
            candidates = (
                imports.get(name),
                cs.SEPARATOR_DOT.join([module, name]),
            )
            callee = next((qn for qn in candidates if qn and qn in defined), None)
            if callee is not None:
                edges.add((_node_qn(enclosing, module, parents), callee))
    return edges


def cgr_static_calls(target: Path, project: str) -> set[CallEdge]:
    ingestor = _capture(target, project)
    return {
        (str(from_val), str(to_val))
        for _fl, from_val, rel_type, _tl, to_val in ingestor.rels
        if rel_type == _CALLS
    }


def _edge_repr(edge: CallEdge) -> str:
    return ec.STATIC_CALL_EDGE_REPR.format(caller=edge[0], callee=edge[1])


def score_static_calls(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    # Recall only: hits are oracle edges cgr also has. cgr's extra edges
    # (method / type-inferred calls) are expected, not false positives, so
    # precision is not graded here.
    hits = oracle & cgr
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.EDGE.value, ec.STATIC_CALLS_LABEL, hits, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.STATIC_CALLS_DIFF_PREFIX + ec.STATIC_CALLS_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate static call recall for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for static_calls_scores.csv and diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.STATIC_CALLS_TARGET.format(target=target, project=project))

    oracle = oracle_static_calls(target, project)
    logger.success(ls.STATIC_CALLS_ORACLE_DONE.format(count=len(oracle)))
    cgr = cgr_static_calls(target, project)
    logger.success(ls.STATIC_CALLS_CGR_DONE.format(count=len(cgr)))

    result = score_static_calls(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.STATIC_CALLS_SCORES_FILENAME,
        ec.STATIC_CALLS_DIFF_FILENAME,
    )
    render(result, ec.STATIC_CALLS_TITLE)


if __name__ == "__main__":
    typer.run(main)
