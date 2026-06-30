# (H) Multi-language retrieval (C). Extends the file-level call-localization
# (H) benchmark to C: for each first-party C function, which files call it.
# (H) cgr's C CALLS edges (reduced to (caller_file, callee_simple_name)) are
# (H) graded against call sites extracted by libclang, over the same first-party
# (H) name universe. libclang resolves the true translation-unit call graph,
# (H) independent of cgr's tree-sitter C frontend (cgr parses C with tree-sitter
# (H) by default; CPP_FRONTEND=libclang is off), so this measures cgr's cross-file
# (H) C call resolution against ground truth (mirrors evals/lua_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import cpp_available, run_c_call_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.C_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_c_call_edges(
    target: Path,
) -> tuple[set[CallEdge], frozenset[str], frozenset[str]]:
    return run_c_call_oracle(target)


def cgr_c_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.C_SUFFIXES)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _CALLS:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        # (H) Grade only files the oracle parsed cleanly (its authoritative set).
        if path is None or path not in covered:
            continue
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1]
        if name in declared:
            edges.add((path, name))
    return edges


def _edge_repr(edge: CallEdge) -> str:
    return ec.C_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_c_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.C_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.C_RETRIEVAL_DIFF_PREFIX + ec.C_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of C sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for c_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not cpp_available():
        logger.error(ls.C_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.C_RETRIEVAL_ORACLE.format(target=target))
    oracle, declared, covered = oracle_c_call_edges(target)
    logger.success(ls.C_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))
    logger.info(ls.C_RETRIEVAL_COVERED.format(count=len(covered)))

    logger.info(ls.C_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_c_call_edges(target, project, declared, covered)
    logger.success(ls.C_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_c_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.C_RETRIEVAL_SCORES_FILENAME,
        ec.C_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.C_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
