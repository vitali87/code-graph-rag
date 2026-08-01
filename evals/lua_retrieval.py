# Multi-language retrieval (Lua). File-level call-localization: for each
# first-party Lua function, which files call it. cgr's Lua CALLS edges
# ((caller_file, callee_simple_name)) are graded against call sites extracted
# by luaparse, over the same first-party name universe. luaparse is independent
# of cgr's tree-sitter Lua frontend, so this measures cgr's cross-file Lua call
# resolution against ground truth (mirrors evals/php_retrieval.py /
# java_retrieval.py / ts_retrieval.py).
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracles import lua_oracle_available, run_lua_call_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.LUA_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

CallEdge = tuple[str, str]


def oracle_lua_call_edges(target: Path) -> tuple[set[CallEdge], frozenset[str]]:
    return run_lua_call_oracle(target)


def cgr_lua_call_edges(
    target: Path, project: str, declared: frozenset[str]
) -> set[CallEdge]:
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.LUA_SUFFIX)
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
    return ec.LUA_CALL_EDGE_REPR.format(file=edge[0], name=edge[1])


def score_lua_retrieval(cgr: set[CallEdge], oracle: set[CallEdge]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, ec.LUA_RETRIEVAL_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.LUA_RETRIEVAL_DIFF_PREFIX + ec.LUA_RETRIEVAL_LABEL] = DiffBucket(
            missing=[_edge_repr(e) for e in sorted(oracle - cgr)],
            extra=[_edge_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="Directory of Lua sources to evaluate call retrieval.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for lua_retrieval_scores.csv and diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if not lua_oracle_available():
        logger.error(ls.LUA_ORACLE_MISSING.format(binary=ec.NODE_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    logger.info(ls.LUA_RETRIEVAL_ORACLE.format(binary=ec.NODE_BIN, target=target))
    oracle, declared = oracle_lua_call_edges(target)
    logger.success(ls.LUA_RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.LUA_RETRIEVAL_CGR.format(target=target, project=project))
    cgr = cgr_lua_call_edges(target, project, declared)
    logger.success(ls.LUA_RETRIEVAL_CGR_DONE.format(count=len(cgr)))

    result = score_lua_retrieval(cgr, oracle)
    write_outputs(
        result,
        out_dir,
        ec.LUA_RETRIEVAL_SCORES_FILENAME,
        ec.LUA_RETRIEVAL_DIFF_FILENAME,
    )
    render(result, ec.LUA_RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
