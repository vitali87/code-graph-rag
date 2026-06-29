# (H) Multi-language retrieval (Go). Extends the file-level call-localization
# (H) benchmark to a second language: for each first-party Go symbol, which files
# (H) call it. cgr's Go CALLS edges (reduced to caller file + callee simple name)
# (H) are graded against go/ast call sites over the same first-party name universe.
# (H) The oracle uses Go's own parser, independent of cgr's tree-sitter frontend,
# (H) so this measures cgr's cross-file Go call resolution against ground truth.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import go_available, run_go_call_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.GO_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_go_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_go_call_oracle(target)


def cgr_go_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.GO_SUFFIX)
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
    return ec.GO_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_go_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.GO_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.GO_RETRIEVAL_DIFF_PREFIX + ec.GO_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Go sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for go_retrieval_scores.csv and diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not go_available():
        logger.error(ls.GO_ORACLE_MISSING.format(binary=ec.GO_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.GO_RETRIEVAL_ORACLE.format(binary=ec.GO_BIN, target=target))
    oracle, declared = oracle_go_call_edges(target)
    logger.success(ls.GO_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.GO_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_go_call_edges(target, project, declared)
    logger.success(ls.GO_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_go_retrieval(cgr, oracle)
    write_outputs(
        result, out_dir, ec.GO_RETRIEVAL_SCORES_FILENAME, ec.GO_RETRIEVAL_DIFF_FILENAME
    )
    render(result, ec.GO_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
