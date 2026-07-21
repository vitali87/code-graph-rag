# Incremental-update correctness eval. cgr's incremental indexer re-parses
# only changed files; the promise is that the resulting graph equals a clean
# forced re-index of the same tree. This eval verifies that promise: index a
# repo, apply a semantically neutral edit to one file (a trailing comment
# that changes the hash but not the AST), run an incremental update, then
# diff the mutated graph against a clean re-index of the identical on-disk
# state. The clean re-index is the oracle, so any divergence is a real
# incremental-update bug (e.g. dropped inbound CALLS, issue #532).
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from tree_sitter import Parser

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import LanguageQueries

from . import constants as ec
from . import logs as ls
from .ast_oracle import _iter_py_files
from .cgr_graph import _StatefulIngestor
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, GraphState, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.INCREMENTAL_DEFAULT_TARGET)

_Parsers = Mapping[cs.SupportedLanguage, Parser]
_Queries = Mapping[cs.SupportedLanguage, LanguageQueries]
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)


def neutral_edit(content: bytes) -> bytes:
    return content + ec.NEUTRAL_EDIT_COMMENT.encode(cs.ENCODING_UTF8)


def snapshot(store: _StatefulIngestor) -> GraphState:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
    )
    return GraphState(nodes=nodes, edges=edges)


def _purge_index_state(work: Path) -> None:
    # A copied tree may carry cgr's own hash/dir-mtime caches. Left in place, a
    # future-dated cache makes the baseline index skip every file, so remove
    # such state before indexing.
    for name in (cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME):
        for stale in work.rglob(name):
            stale.unlink()


def _index(
    store: _StatefulIngestor,
    repo: Path,
    project: str,
    parsers: _Parsers,
    queries: _Queries,
    force: bool,
) -> None:
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=force)


def run_neutral_edit_scenario(
    repo_src: Path,
    project: str,
    target_rel: str,
    parsers: _Parsers,
    queries: _Queries,
    work_root: Path,
) -> tuple[GraphState, GraphState]:
    work = work_root / ec.INCREMENTAL_WORK_DIRNAME
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(repo_src, work)
    _purge_index_state(work)

    store = _StatefulIngestor()
    _index(store, work, project, parsers, queries, force=False)

    # The neutral edit must read as "changed": bump its mtime past the hash
    # cache so the in-sync fast path and per-file mtime gate both fire.
    cache = work / cs.HASH_CACHE_FILENAME
    future = cache.stat().st_mtime + ec.INCREMENTAL_MTIME_BUMP
    target = work / target_rel
    target.write_bytes(neutral_edit(target.read_bytes()))
    os.utime(target, (future, future))

    _index(store, work, project, parsers, queries, force=False)
    incremental = snapshot(store)

    clean_store = _StatefulIngestor()
    _index(clean_store, work, project, parsers, queries, force=True)
    clean = snapshot(clean_store)
    return incremental, clean


def _recompute(category: str, label: str, tp: int, fp: int, fn: int) -> ScoreRow:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ScoreRow(
        category=category,
        label=label,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=round(precision, ec.ROUND_DIGITS),
        recall=round(recall, ec.ROUND_DIGITS),
        f1=round(f1, ec.ROUND_DIGITS),
    )


def _node_repr(node: tuple[str, str]) -> str:
    return ec.STATE_NODE_REPR.format(label=node[0], uid=node[1])


def _edge_repr(edge: tuple[str, str, str, str, str]) -> str:
    return ec.STATE_EDGE_REPR.format(
        rel=edge[2], fl=edge[0], fv=edge[1], tl=edge[3], tv=edge[4]
    )


def compare_states(incremental: GraphState, clean: GraphState) -> ScoreResult:
    # clean is the oracle: missing = present in clean, absent from incremental
    # (fn); stale = present in incremental, absent from clean (fp).
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}

    for label in sorted({n[0] for n in incremental.nodes | clean.nodes}):
        got = {n for n in incremental.nodes if n[0] == label}
        want = {n for n in clean.nodes if n[0] == label}
        row = _prf(ec.Category.NODE.value, label, got, want)
        if row is not None:
            rows.append(row)
            diff[ec.INCREMENTAL_NODE_DIFF_PREFIX + label] = DiffBucket(
                missing=[_node_repr(n) for n in sorted(want - got)],
                extra=[_node_repr(n) for n in sorted(got - want)],
            )

    for rel in sorted({e[2] for e in incremental.edges | clean.edges}):
        got_e = {e for e in incremental.edges if e[2] == rel}
        want_e = {e for e in clean.edges if e[2] == rel}
        row = _prf(ec.Category.EDGE.value, rel, got_e, want_e)
        if row is not None:
            rows.append(row)
            diff[ec.INCREMENTAL_EDGE_DIFF_PREFIX + rel] = DiffBucket(
                missing=[_edge_repr(e) for e in sorted(want_e - got_e)],
                extra=[_edge_repr(e) for e in sorted(got_e - want_e)],
            )

    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def _merge(results: list[ScoreResult]) -> ScoreResult:
    totals: dict[tuple[str, str], tuple[int, int, int]] = {}
    diff: dict[str, DiffBucket] = {}
    for result in results:
        for row in result.rows:
            key = (row["category"], row["label"])
            tp, fp, fn = totals.get(key, (0, 0, 0))
            totals[key] = (tp + row["tp"], fp + row["fp"], fn + row["fn"])
        for bucket_key, bucket in result.diff.items():
            merged = diff.setdefault(bucket_key, DiffBucket(missing=[], extra=[]))
            merged["missing"].extend(bucket["missing"])
            merged["extra"].extend(bucket["extra"])

    rows = [
        _recompute(category, label, tp, fp, fn)
        for (category, label), (tp, fp, fn) in sorted(totals.items())
    ]
    capped = {
        key: DiffBucket(
            missing=sorted(set(bucket["missing"]))[: ec.INCREMENTAL_DIFF_SAMPLE_CAP],
            extra=sorted(set(bucket["extra"]))[: ec.INCREMENTAL_DIFF_SAMPLE_CAP],
        )
        for key, bucket in diff.items()
    }
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=capped)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate incremental updates for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    sample: Annotated[
        int, typer.Option(help="Number of python files to probe with a neutral edit.")
    ] = ec.INCREMENTAL_DEFAULT_SAMPLE,
    out_dir: Annotated[
        Path,
        typer.Option(help="Directory for incremental_scores.csv and the diff json."),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.INCREMENTAL_TARGET.format(target=target, project=project))

    py_files = sorted(p.relative_to(target).as_posix() for p in _iter_py_files(target))
    if not py_files:
        logger.error(ls.INCREMENTAL_NO_PY.format(target=target))
        raise typer.Exit(code=1)

    probes = py_files[:sample] if sample > 0 else py_files
    logger.info(ls.INCREMENTAL_SAMPLED.format(count=len(probes), total=len(py_files)))

    parsers, queries = load_parsers()
    results: list[ScoreResult] = []
    clean_equivalent = 0
    # Work outside the repo tree: each probe copies the whole target, so a work
    # dir under out_dir would pollute the repo and be scanned by hooks.
    work_root = Path(tempfile.mkdtemp(prefix=ec.INCREMENTAL_TMP_PREFIX))
    try:
        for index, rel in enumerate(probes, start=1):
            logger.info(
                ls.INCREMENTAL_PROBE.format(index=index, total=len(probes), path=rel)
            )
            incremental, clean = run_neutral_edit_scenario(
                target, project, rel, parsers, queries, work_root
            )
            if incremental == clean:
                clean_equivalent += 1
            else:
                logger.warning(
                    ls.INCREMENTAL_PROBE_DIVERGED.format(
                        path=rel,
                        missing=len(clean.edges - incremental.edges),
                        stale=len(incremental.edges - clean.edges),
                    )
                )
            results.append(compare_states(incremental, clean))
    finally:
        shutil.rmtree(work_root, ignore_errors=True)

    logger.success(
        ls.INCREMENTAL_DONE.format(clean=clean_equivalent, total=len(probes))
    )
    merged = _merge(results)
    write_outputs(
        merged, out_dir, ec.INCREMENTAL_SCORES_FILENAME, ec.INCREMENTAL_DIFF_FILENAME
    )
    render(merged, ec.INCREMENTAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
