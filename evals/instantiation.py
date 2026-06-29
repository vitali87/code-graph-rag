# (H) Instantiation eval. File-level constructor localization: for each
# (H) first-party class, which files instantiate it. cgr's INSTANTIATES edges are
# (H) compared against an ast oracle of calls whose callee simple name is a
# (H) first-party class, over the same file and class universe. This isolates the
# (H) INSTANTIATES signal that the retrieval eval folds into CALLS, so a
# (H) constructor-resolution regression shows up on its own.
import ast
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .module_calls import _callee_name, _is_dunder
from .retrieval import parse_py_trees
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.INSTANTIATION_DEFAULT_TARGET)

_CLASS = cs.NodeLabel.CLASS.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

InstantiationEdge = tuple[str, str]


def _class_names(trees: list[tuple[str, ast.Module]]) -> set[str]:
    names: set[str] = set()
    for _rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and not _is_dunder(node.name):
                names.add(node.name)
    return names


def oracle_instantiations(target: Path, project: str) -> set[InstantiationEdge]:
    trees, _files = parse_py_trees(target)
    classes = _class_names(trees)
    edges: set[InstantiationEdge] = set()
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (name := _callee_name(node.func)):
                if name in classes:
                    edges.add((rel, name))
    return edges


def cgr_instantiations(target: Path, project: str) -> set[InstantiationEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.PY_SUFFIX)
    }
    edges: set[InstantiationEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _INSTANTIATES:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None:
            continue
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1]
        if not _is_dunder(name):
            edges.add((path, name))
    return edges


def _edge_repr(edge: InstantiationEdge) -> str:
    return ec.INSTANTIATION_EDGE_REPR.format(file=edge[0], cls=edge[1])


def score_instantiations(
    cgr: set[InstantiationEdge], oracle: set[InstantiationEdge]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.EDGE.value, ec.INSTANTIATES_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.INSTANTIATION_DIFF_PREFIX + ec.INSTANTIATES_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate instantiation for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for instantiation_scores.csv and diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.INSTANTIATION_TARGET.format(target=target, project=project))

    oracle = oracle_instantiations(target, project)
    logger.success(ls.INSTANTIATION_ORACLE_DONE.format(count=len(oracle)))
    cgr = cgr_instantiations(target, project)
    logger.success(ls.INSTANTIATION_CGR_DONE.format(count=len(cgr)))

    result = score_instantiations(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.INSTANTIATION_SCORES_FILENAME,
        ec.INSTANTIATION_DIFF_FILENAME,
    )
    render(result, ec.INSTANTIATION_TITLE)


if __name__ == "__main__":
    typer.run(main)
