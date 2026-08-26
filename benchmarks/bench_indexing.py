"""Indexing-time benchmark on a pinned corpus (issue #1374, deliverable 4).

The first of the four benchmarks that issue asks for, and deliberately the
first: it needs no model key and no databases, so it runs in CI and produces a
README row immediately. The other three (agentic resolved-rate, query latency,
token cost) all require a live stack or an LLM key.

**What this measures.** The real `GraphUpdater` pipeline -- the same parse,
definition and call passes production runs -- against an in-memory ingestor
rather than Memgraph. So the number is honest about parsing and graph
construction, and excludes database round-trips by construction. That
exclusion is the point rather than a shortcut: it isolates the cost this
project controls from the cost of the two databases, which is precisely the
question the field-test review asked (is the graph worth feeding?), and it is
what makes the measurement reproducible without infrastructure.

Do not read the result as end-to-end `cgr index` wall-clock. Deliverable 2 in
#1374 covers the real stack, and that number will be larger.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from codebase_rag import constants as cs

# macOS reports ru_maxrss in bytes, Linux in kibibytes. Getting this wrong is a
# silent factor-of-1024 error in a published table, so it is converted rather
# than reported raw.
_RSS_SCALE = 1 if sys.platform == "darwin" else 1024


@dataclass(frozen=True)
class IndexingMeasurement:
    """One indexing run, with everything needed to reproduce it.

    The provenance fields are not decoration: a timing without its corpus and
    version is a number nobody can check, and #1374 is explicit that the
    credibility of the table is the reproducibility of its cells.
    """

    corpus_name: str
    corpus_path: str
    cgr_version: str
    file_count: int
    node_count: int
    edge_count: int
    duration_seconds: float
    peak_rss_bytes: int


def _peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_SCALE


def _cgr_version() -> str:
    try:
        from importlib.metadata import version

        return version("code-graph-rag")
    except Exception:
        # A source checkout without an installed distribution still benchmarks;
        # it just cannot pin a version, and saying so beats inventing one.
        return "unknown"


def measure_indexing(corpus: Path, project_name: str) -> IndexingMeasurement:
    """Index `corpus` once and report what it cost.

    Raises on a corpus with no indexable files rather than reporting zeroes:
    "0 files in 0.01s" reads like a very fast index rather than like a
    benchmark that measured nothing, and it is the published-number version of
    the silent-smaller-than-truth failure.
    """
    # Imported here rather than at module scope so that `--help` and the
    # dataclass stay usable without paying the parser-loading cost.
    from evals.cgr_graph import _capture

    corpus = corpus.resolve()
    rss_before = _peak_rss_bytes()

    start = time.perf_counter()
    ingestor = _capture(corpus, project_name)
    duration = time.perf_counter() - start

    # FILE only. cgr emits BOTH a File and a Module node for every parsed
    # source file, so counting either-or double-counts each one and would have
    # published a file count twice the truth.
    file_count = sum(
        1 for (label, _uid) in ingestor.nodes if label == cs.NodeLabel.FILE.value
    )
    if not file_count:
        raise ValueError(
            f"no indexable files under {corpus}: refusing to report a benchmark "
            "that measured nothing"
        )

    return IndexingMeasurement(
        corpus_name=corpus.name,
        corpus_path=str(corpus),
        cgr_version=_cgr_version(),
        file_count=file_count,
        node_count=len(ingestor.nodes),
        edge_count=len(ingestor.rels),
        duration_seconds=duration,
        # The delta would be negative whenever the peak was already set by
        # earlier work in this process, so report the peak itself.
        peak_rss_bytes=max(_peak_rss_bytes(), rss_before),
    )


def format_markdown_row(result: IndexingMeasurement) -> str:
    """One README table row, carrying its own provenance."""
    mib = result.peak_rss_bytes / (1024 * 1024)
    return (
        f"| Indexing time — {result.corpus_name}, {result.file_count} files "
        f"(cgr {result.cgr_version}) | {result.duration_seconds:.1f}s | "
        f"{result.node_count} nodes / {result.edge_count} edges | {mib:.0f} MiB |"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="repository to index")
    parser.add_argument(
        "--project-name",
        default=None,
        help="graph project name (defaults to the corpus directory name)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a table row"
    )
    args = parser.parse_args()

    result = measure_indexing(
        args.corpus, project_name=args.project_name or args.corpus.resolve().name
    )
    print(
        json.dumps(asdict(result), indent=2)
        if args.json
        else format_markdown_row(result)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
