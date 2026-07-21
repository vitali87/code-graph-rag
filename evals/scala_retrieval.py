# Multi-language retrieval (Scala). Extends the file-level call-localization
# benchmark to Scala: for each first-party Scala symbol, which files call it.
# cgr's Scala CALLS edges (reduced to caller file + callee simple name) are
# graded against scalameta call sites over the same first-party name universe.
# scalameta (via scala-cli) is independent of cgr's tree-sitter frontend, so
# this measures cgr's cross-file Scala call resolution (mirrors
# evals/java_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import run_scala_call_oracle, scala_available
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.SCALA_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_scala_call_edges(
    target: Path,
) -> tuple[set[CallEdge], frozenset[str], frozenset[str]]:
    return run_scala_call_oracle(target)


def cgr_scala_call_edges(
    target: Path, project: str, declared: frozenset[str], covered: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH)
        and str(props[cs.KEY_PATH]).endswith(ec.SCALA_SUFFIXES)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _CALLS:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        # Grade only files the oracle parsed cleanly (its authoritative set).
        if path is None or path not in covered:
            continue
        # Reduce a callee qn to its trailing simple name to match the oracle,
        # dropping dotted scope and (defensively) a parameter signature.
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1].split(cs.CHAR_PAREN_OPEN)[0]
        if name in declared:
            edges.add((path, name))
    return edges


def _edge_repr(edge: CallEdge) -> str:
    return ec.SCALA_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_scala_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.SCALA_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.SCALA_RETRIEVAL_DIFF_PREFIX + ec.SCALA_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of Scala sources to evaluate call retrieval."),
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for scala_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not scala_available():
        logger.error(ls.SCALA_ORACLE_MISSING.format(binary=ec.SCALA_CLI_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(
        ls.SCALA_RETRIEVAL_ORACLE.format(binary=ec.SCALA_CLI_BIN, target=target)
    )
    oracle, declared, covered = oracle_scala_call_edges(target)
    logger.success(ls.SCALA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))
    logger.info(ls.SCALA_RETRIEVAL_COVERED.format(count=len(covered)))

    logger.info(ls.SCALA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_scala_call_edges(target, project, declared, covered)
    logger.success(ls.SCALA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_scala_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.SCALA_RETRIEVAL_SCORES_FILENAME,
        ec.SCALA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.SCALA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
