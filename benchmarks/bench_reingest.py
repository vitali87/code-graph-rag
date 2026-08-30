"""Latency of a one-file re-ingest (issue #1524).

An agent that edits a file needs the graph to reflect the edit in hundreds of
milliseconds; `GraphUpdater.reingest(paths)` is the path built for that. This
harness indexes a corpus once, then repeatedly edits one file and re-ingests
it, reporting p50/p95 of the scoped path next to the cost of the whole-tree
`update_repository` path (`GraphUpdater.run()` behind the hash cache) for the
same edit. Both run against the in-memory ingestor `bench_indexing` uses, so
the numbers cover parsing, scoped call resolution and graph construction and
exclude database round-trips by construction.

    uv run python -m benchmarks.bench_reingest . --file codebase_rag/graph_updater.py
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs

NEUTRAL_EDIT = "# bench edit\n"
DEFAULT_ITERATIONS = 20
DEFAULT_FULL_RUNS = 3
PERCENTILE_95 = 0.95


@dataclass(frozen=True)
class ReingestMeasurement:
    corpus_name: str
    corpus_path: str
    target: str
    file_count: int
    line_count: int
    iterations: int
    reingest_p50_ms: float
    reingest_p95_ms: float
    reingest_max_ms: float
    reparsed_files: int
    affected_files: int
    delta_p50_ms: float
    delta_p95_ms: float
    full_update_p50_ms: float
    full_update_runs: int


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _count_lines(root: Path, ingestor_paths: list[str]) -> int:
    total = 0
    for rel in ingestor_paths:
        try:
            with (root / rel).open("rb") as fh:
                total += sum(1 for _ in fh)
        except OSError:
            continue
    return total


def _toggle_edit(path: Path, iteration: int) -> None:
    # Alternate appending and removing a trailing comment so every iteration
    # changes the file's bytes (and hash) without changing its AST.
    text = path.read_text(encoding=cs.ENCODING_UTF8)
    if iteration % 2 == 0:
        path.write_text(text + NEUTRAL_EDIT, encoding=cs.ENCODING_UTF8)
    else:
        path.write_text(text.removesuffix(NEUTRAL_EDIT), encoding=cs.ENCODING_UTF8)


def _bump_mtime(root: Path, path: Path) -> None:
    # The incremental run() skips a file whose mtime is not newer than the
    # hash cache, so a sub-second edit needs an explicit bump.
    cache = root / cs.HASH_CACHE_FILENAME
    if cache.is_file():
        future = cache.stat().st_mtime + 2
        os.utime(path, (future, future))


def measure_reingest(
    corpus: Path,
    target: Path,
    iterations: int = DEFAULT_ITERATIONS,
    full_runs: int = DEFAULT_FULL_RUNS,
) -> ReingestMeasurement:
    # Imported here so `--help` stays cheap.
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.structural_delta import observe
    from evals.cgr_graph import _StatefulIngestor

    if iterations <= 0:
        raise ValueError(f"iterations must be positive, got {iterations}")
    corpus = corpus.resolve()
    target = (target if target.is_absolute() else corpus / target).resolve()
    try:
        target.relative_to(corpus)
    except ValueError as error:
        raise ValueError(f"target file must be inside the corpus: {target}") from error
    if not target.is_file():
        raise ValueError(f"target file does not exist: {target}")
    original = target.read_bytes()
    original_stat = target.stat()

    # The MCP server and the CLI log at INFO; debug-level formatting alone
    # is a measurable share of a sub-second budget.
    logger.remove()
    logger.add(sys.stderr, level=cs.LOG_LEVEL_INFO)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=corpus,
        parsers=parsers,
        queries=queries,
        project_name=corpus.name,
    )
    try:
        updater.run(force=True)
        file_paths = [
            str(props.get(cs.KEY_PATH))
            for (label, _uid), props in store.nodes.items()
            if label == cs.NodeLabel.FILE.value
        ]
        if not file_paths:
            raise ValueError(f"no indexable files under {corpus}")

        samples: list[float] = []
        reparsed = affected = 0
        for i in range(iterations):
            _toggle_edit(target, i)
            started = time.perf_counter()
            report = updater.reingest([target])
            samples.append((time.perf_counter() - started) * 1000.0)
            reparsed = len(report.reparsed)
            affected = len(report.affected)

        # The structural delta (issue #1525) wraps the same re-ingest in two
        # subgraph reads and the diff; its overhead is what is measured.
        delta_samples: list[float] = []
        relative = target.relative_to(corpus).as_posix()
        for i in range(iterations):
            _toggle_edit(target, i)
            delta = observe(
                store.fetch_all,
                corpus.name,
                [relative],
                lambda: updater.reingest([target]),
                repo_root=corpus,
            )
            delta_samples.append(delta["delta_ms"])

        full_samples: list[float] = []
        for i in range(full_runs):
            _toggle_edit(target, i)
            _bump_mtime(corpus, target)
            started = time.perf_counter()
            updater.run()
            full_samples.append((time.perf_counter() - started) * 1000.0)
    finally:
        target.write_bytes(original)
        # Bytes AND timestamps: a changed mtime would wake a watcher or skew
        # the next mtime-gated update.
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        for name in (cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME):
            stale = corpus / name
            if stale.is_file():
                stale.unlink()

    return ReingestMeasurement(
        corpus_name=corpus.name,
        corpus_path=str(corpus),
        target=target.relative_to(corpus).as_posix(),
        file_count=len(file_paths),
        line_count=_count_lines(corpus, file_paths),
        iterations=iterations,
        reingest_p50_ms=statistics.median(samples),
        reingest_p95_ms=_percentile(samples, PERCENTILE_95),
        reingest_max_ms=max(samples),
        reparsed_files=reparsed,
        affected_files=affected,
        delta_p50_ms=statistics.median(delta_samples),
        delta_p95_ms=_percentile(delta_samples, PERCENTILE_95),
        full_update_p50_ms=statistics.median(full_samples) if full_samples else 0.0,
        full_update_runs=len(full_samples),
    )


def format_markdown_row(result: ReingestMeasurement) -> str:
    return (
        f"| {result.corpus_name} ({result.file_count} files, "
        f"{result.line_count:,} lines) | `{result.target}` "
        f"(+{result.affected_files} dependents) | "
        f"{result.reingest_p50_ms:.0f} ms | {result.reingest_p95_ms:.0f} ms | "
        f"{result.delta_p50_ms:.0f} ms | {result.delta_p95_ms:.0f} ms | "
        f"{result.full_update_p50_ms:.0f} ms |"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="repository to index")
    parser.add_argument(
        "--file", type=Path, required=True, help="file to edit and re-ingest"
    )
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--full-runs", type=int, default=DEFAULT_FULL_RUNS)
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table row"
    )
    args = parser.parse_args()
    result = measure_reingest(args.corpus, args.file, args.iterations, args.full_runs)
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(format_markdown_row(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
