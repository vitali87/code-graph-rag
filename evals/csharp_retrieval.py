# (H) Multi-language retrieval (C#). Extends the file-level call-localization
# (H) benchmark to C#: for each first-party C# symbol, which files call it.
# (H) cgr's C# CALLS edges (reduced to caller file + callee simple name) are
# (H) graded against Roslyn invocation sites over the same first-party name
# (H) universe. The oracle uses Roslyn's own syntax parser, independent of cgr's
# (H) tree-sitter frontend, so this measures cgr's cross-file C# call resolution
# (H) against ground truth (mirrors evals/java_retrieval.py). Run with
# (H) CSHARP_FRONTEND=hybrid to grade the opt-in Roslyn semantic frontend.
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import csharp_oracle_available, run_csharp_call_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.CSHARP_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_csharp_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_csharp_call_oracle(target)


def cgr_csharp_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.CS_SUFFIX)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        # (H) INSTANTIATES counts too (as in the Python retrieval): `new T()`
        # (H) on a type with no explicit constructor has no ctor node to CALL,
        # (H) only an INSTANTIATES edge to the class, while the oracle records
        # (H) the creation site by type name.
        if rel_type not in (_CALLS, _INSTANTIATES):
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None:
            continue
        # (H) A C# Method qn carries its overload signature (Class.Name(args)),
        # (H) so strip it to recover the simple callee name the oracle records.
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1].split(cs.CHAR_PAREN_OPEN)[0]
        if name in declared:
            edges.add((path, name))
    return edges


def _edge_repr(edge: CallEdge) -> str:
    return ec.CSHARP_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_csharp_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.CSHARP_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.CSHARP_RETRIEVAL_DIFF_PREFIX + ec.CSHARP_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of C# sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for csharp_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not csharp_oracle_available():
        logger.error(ls.CSHARP_ORACLE_MISSING)
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.CSHARP_RETRIEVAL_ORACLE.format(binary=ec.DOTNET_BIN, target=target))
    oracle, declared = oracle_csharp_call_edges(target)
    logger.success(ls.CSHARP_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.CSHARP_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_csharp_call_edges(target, project, declared)
    logger.success(ls.CSHARP_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_csharp_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.CSHARP_RETRIEVAL_SCORES_FILENAME,
        ec.CSHARP_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.CSHARP_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
