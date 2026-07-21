# Multi-language retrieval (JavaScript). Mirrors ts_retrieval.py for .js/.jsx:
# cgr's JS CALLS edges (reduced to (caller_file, callee_simple_name)) are graded
# against call sites the TypeScript compiler API (tsc) extracts from the same
# files, over the same first-party name universe. tsc parses JS syntactically
# and is independent of cgr's tree-sitter JS frontend, so this measures cgr's
# cross-file JS call resolution against ground truth.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import run_javascript_call_oracle, typescript_available
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.JS_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_js_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_javascript_call_oracle(target)


def cgr_js_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.JS_SUFFIXES)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _CALLS:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None:
            continue
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1]
        if name in declared:
            edges.add((path, name))
    return edges


def _edge_repr(edge: CallEdge) -> str:
    return ec.JS_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_js_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.JS_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.JS_RETRIEVAL_DIFF_PREFIX + ec.JS_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of JavaScript sources to evaluate.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for js_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not typescript_available():
        logger.error(ls.TS_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.JS_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_js_call_edges(target)
    logger.success(ls.JS_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.JS_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_js_call_edges(target, project, declared)
    logger.success(ls.JS_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_js_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.JS_RETRIEVAL_SCORES_FILENAME,
        ec.JS_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.JS_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
