# Multi-language retrieval (Java). Extends the file-level call-localization
# benchmark to Java: for each first-party Java symbol, which files call it.
# cgr's Java CALLS edges (reduced to caller file + callee simple name) are
# graded against javac method-invocation sites over the same first-party name
# universe. The oracle uses the JDK's own Compiler Tree API (javac),
# independent of cgr's tree-sitter frontend, so this measures cgr's cross-file
# Java call resolution against ground truth (mirrors evals/rust_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import java_available, run_java_call_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.JAVA_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_java_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_java_call_oracle(target)


def cgr_java_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.JAVA_SUFFIX)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _CALLS:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None:
            continue
        # A Java Method qn carries its parameter signature (Class.name(args)),
        # so strip it to recover the simple callee name the oracle records.
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1].split(cs.CHAR_PAREN_OPEN)[0]
        if name in declared:
            edges.add((path, name))
    return edges


def _edge_repr(edge: CallEdge) -> str:
    return ec.JAVA_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_java_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.JAVA_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.JAVA_RETRIEVAL_DIFF_PREFIX + ec.JAVA_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Java sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for java_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not java_available():
        logger.error(ls.JAVA_ORACLE_MISSING.format(binary=ec.JAVAC_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.JAVA_RETRIEVAL_ORACLE.format(binary=ec.JAVAC_BIN, target=target))
    oracle, declared = oracle_java_call_edges(target)
    logger.success(ls.JAVA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.JAVA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_java_call_edges(target, project, declared)
    logger.success(ls.JAVA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_java_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.JAVA_RETRIEVAL_SCORES_FILENAME,
        ec.JAVA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.JAVA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
