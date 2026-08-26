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
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from codebase_rag import constants as cs

# macOS reports ru_maxrss in bytes, Linux in kibibytes. Getting this wrong is a
# silent factor-of-1024 error in a published table, so it is converted rather
# than reported raw.
_RSS_SCALE = 1 if sys.platform == "darwin" else 1024

# `resource` is Unix-only and this repo runs Windows CI, so an unconditional
# top-level import makes the module unimportable there -- collection fails
# before any measurement can run. Absent it, memory is reported as unavailable
# rather than guessed.
try:  # pragma: no cover - platform-dependent
    import resource
except ModuleNotFoundError:  # pragma: no cover - Windows
    resource = None  # type: ignore[assignment]

# Sentinel for "this platform cannot report peak memory". Distinct from 0,
# which would read as "the run used no memory".
RSS_UNAVAILABLE = -1

_CHILD_ENV_FLAG = "CGR_BENCH_INDEXING_CHILD"


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


def _proc_peak_rss_bytes() -> int | None:
    """This process's peak RSS from `/proc/self/status`, or None if unreadable.

    `VmHWM` is per-process and genuinely reset by exec, which `ru_maxrss` is
    NOT on Linux -- there the child inherits the parent's high-water mark
    across fork/exec, so a freshly spawned child reports whatever the parent
    had already peaked at. Measured: a released 192 MiB parent ballast moved
    the child's reported peak by 201,375,744 bytes.

    Returns None rather than a sentinel so the caller can fall back; absent
    `/proc` (macOS, Windows) is not the same as "cannot measure".
    """
    try:
        with open("/proc/self/status", encoding=cs.ENCODING_UTF8) as handle:
            for line in handle:
                if line.startswith("VmHWM:"):
                    # "VmHWM:\t  123456 kB"
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _self_peak_rss_bytes() -> int:
    """This process's peak RSS, or `RSS_UNAVAILABLE` where unsupported.

    Only meaningful in the freshly-spawned child, and the source depends on
    the platform because `ru_maxrss` is not child-local everywhere:

    - Linux: `/proc/self/status` `VmHWM`, which IS exec-scoped. If it cannot
      be read, this reports unavailable rather than falling back --
      `ru_maxrss` survives fork/exec here, so the fallback would report the
      parent's history as the child's peak. Measured: a released 192 MiB
      parent allocation moved the fallback's answer by 199,208,960 bytes for
      an identical one-file workload.
    - macOS and other non-Linux Unix: `ru_maxrss`, where fork/exec does reset
      the high-water mark, so the value is already child-local.
    - Windows: no `resource` module, so unavailable.

    Reporting unavailable beats reporting a number that is wrong in the
    direction of "this run used memory it never touched".
    """
    from_proc = _proc_peak_rss_bytes()
    if from_proc is not None:
        return from_proc
    if resource is None or sys.platform.startswith("linux"):
        return RSS_UNAVAILABLE
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_SCALE


def _module_name() -> str:
    """This module's importable name, for relaunching it with `-m`.

    `__spec__` is None when the file is run directly as a script
    (`python benchmarks/bench_indexing.py`), so reading `__spec__.name`
    raises AttributeError before the child is ever spawned -- the benchmark
    dies on its most obvious invocation. Falling back to the literal keeps
    both `python -m benchmarks.bench_indexing` and direct execution working.
    """
    spec = globals().get("__spec__")
    if spec is not None and getattr(spec, "name", None):
        return str(spec.name)
    return "benchmarks.bench_indexing"


def _cgr_version() -> str:
    try:
        from importlib.metadata import version

        return version("code-graph-rag")
    except Exception:
        # A source checkout without an installed distribution still benchmarks;
        # it just cannot pin a version, and saying so beats inventing one.
        return "unknown"


def measure_indexing(corpus: Path, project_name: str) -> IndexingMeasurement:
    """Index `corpus` once in a fresh subprocess and report what it cost.

    The subprocess is what makes the memory figure meaningful. `ru_maxrss` is
    a process high-water mark, so measuring in-process attributes any earlier
    allocation to the indexing run: a released 128 MiB buffer makes a one-file
    index report 128 MiB. Subtracting two readings does not rescue it either,
    because once the prior high-water exceeds the run's own peak the
    difference is zero or negative. Only a new process starts clean.

    Raises on a corpus with no indexable files rather than reporting zeroes:
    "0 files in 0.01s" reads like a very fast index rather than like a
    benchmark that measured nothing, and it is the published-number version of
    the silent-smaller-than-truth failure.
    """
    corpus = corpus.resolve()
    if os.environ.get(_CHILD_ENV_FLAG):
        # Already the child: measure directly, and let the peak be this
        # process's, which is exactly the run's.
        return _measure_in_this_process(corpus, project_name)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            _module_name(),
            str(corpus),
            "--project-name",
            project_name,
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        # Handled below: the child's stderr is surfaced in the raised message,
        # which says more than CalledProcessError's return code alone.
        check=False,
        env={**os.environ, _CHILD_ENV_FLAG: "1"},
        cwd=Path(__file__).resolve().parent.parent,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"benchmark subprocess failed ({completed.returncode}): "
            f"{completed.stderr.strip()[-2000:]}"
        )
    return IndexingMeasurement(**json.loads(completed.stdout))


def _measure_in_this_process(corpus: Path, project_name: str) -> IndexingMeasurement:
    # Imported here rather than at module scope so that `--help` and the
    # dataclass stay usable without paying the parser-loading cost.
    from evals.cgr_graph import _capture

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
        # This process was spawned for this one measurement, so its own peak
        # IS the run's peak -- no baseline to subtract and nothing earlier to
        # attribute.
        peak_rss_bytes=_self_peak_rss_bytes(),
    )


def format_markdown_row(result: IndexingMeasurement) -> str:
    """One README table row, carrying its own provenance."""
    if result.peak_rss_bytes == RSS_UNAVAILABLE:
        # "n/a" rather than 0 MiB: a platform that cannot report memory must
        # not publish a figure that reads as a measurement.
        memory = "n/a"
    else:
        memory = f"{result.peak_rss_bytes / (1024 * 1024):.0f} MiB"
    return (
        f"| Indexing time — {result.corpus_name}, {result.file_count} files "
        f"(cgr {result.cgr_version}) | {result.duration_seconds:.1f}s | "
        f"{result.node_count} nodes / {result.edge_count} edges | {memory} |"
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
