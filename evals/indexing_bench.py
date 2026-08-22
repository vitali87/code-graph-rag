# Indexing-time benchmark over pinned real-world corpora (issue #1374, phase 1).
#
# Measures how long cgr takes to build the knowledge graph for a repository:
# wall-clock for parser load and for the graph build (parse plus cross-file
# resolution), peak RSS, and the size of the resulting graph (nodes, edges,
# modules, files). The graph is built with the same in-memory capturing
# ingestor the other evals use, so the number excludes Memgraph batch writes
# (full-stack timing is a later phase of #1374) and needs no running database.
#
# Each corpus is pinned to an exact commit so results are comparable across
# cgr versions. The measured run happens in a fresh child interpreter so peak
# RSS is attributable to the indexing run alone, not to the parent CLI.
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

from . import constants as ec
from . import logs as ls
from .cgr_graph import _CapturingIngestor
from .types_defs import IndexingStats

console = Console()


class CorpusSpec(NamedTuple):
    name: str
    url: str
    commit: str
    # Subdirectory to index, "" for the repo root. django indexes the whole
    # repo (~2800 py files) — the same universe the retrieval eval reports on.
    subdir: str


CORPORA: dict[str, CorpusSpec] = {
    "django": CorpusSpec(
        name="django",
        url="https://github.com/django/django",
        commit="9e7cc2b628fe8fd3895986af9b7fc9525034c1b0",  # tag 5.2
        subdir="",
    ),
}

_MODULE = cs.NodeLabel.MODULE.value
_FILE = cs.NodeLabel.FILE.value


def _peak_rss_mib() -> float | None:
    try:
        import resource
    except ImportError:  # Windows has no resource module
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS, kibibytes on Linux.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(rss / divisor, 1)


def measure(target: Path, project_name: str) -> IndexingStats:
    t0 = time.perf_counter()
    parsers, queries = load_parsers()
    t1 = time.perf_counter()
    ingestor = _CapturingIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=target,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
    ).run(force=True)
    t2 = time.perf_counter()
    labels = [label for (label, _uid) in ingestor.nodes]
    return IndexingStats(
        corpus=project_name,
        commit="",
        parser_load_s=round(t1 - t0, 2),
        build_s=round(t2 - t1, 2),
        peak_rss_mib=_peak_rss_mib(),
        nodes=len(ingestor.nodes),
        edges=len(ingestor.rels),
        modules=sum(1 for label in labels if label == _MODULE),
        files=sum(1 for label in labels if label == _FILE),
        python=sys.version.split()[0],
        platform=sys.platform,
    )


def _ensure_corpus(spec: CorpusSpec, corpus_dir: Path) -> Path:
    checkout = corpus_dir / spec.name
    target = checkout / spec.subdir if spec.subdir else checkout
    if (checkout / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if head == spec.commit:
            return target
    logger.info(ls.INDEXING_FETCHING.format(name=spec.name, commit=spec.commit))
    checkout.mkdir(parents=True, exist_ok=True)
    if not (checkout / ".git").exists():
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "config", "remote.origin.url", spec.url],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "fetch",
            "-q",
            "--depth",
            "1",
            "origin",
            spec.commit,
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "checkout", "-q", "--force", spec.commit],
        check=True,
    )
    return target


def _run_child(target: Path, project_name: str) -> IndexingStats:
    logger.info(ls.INDEXING_MEASURING.format(target=target, project=project_name))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "evals.indexing_bench",
            "--measure-target",
            str(target),
            "--project-name",
            project_name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(ls.INDEXING_CHILD_FAILED.format(stderr=proc.stderr[-2000:]))
    # The child writes exactly one JSON line as its final stdout line; anything
    # above it is stray output from the indexing run itself.
    stats: IndexingStats = json.loads(proc.stdout.strip().splitlines()[-1])
    return stats


def markdown_row(stats: IndexingStats) -> str:
    rss = (
        f"{stats['peak_rss_mib'] / 1024:.1f} GiB"
        if stats["peak_rss_mib"] is not None
        else "n/a"
    )
    commit = stats["commit"][:9] if stats["commit"] else "local"
    return (
        f"| Indexing — `{stats['corpus']}` @ `{commit}` "
        f"({stats['modules']} modules) "
        f"| {stats['build_s']:.0f} s wall, {rss} peak RSS, "
        f"{stats['nodes']} nodes / {stats['edges']} edges | — |"
    )


def _report(stats: IndexingStats, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ec.INDEXING_RESULTS_FILE
    existing: dict[str, IndexingStats] = {}
    if out_path.exists():
        existing = json.loads(out_path.read_text())
    existing[stats["corpus"]] = stats
    out_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    logger.success(ls.WROTE_SCORES.format(path=out_path))

    table = Table(title=f"Indexing benchmark — {stats['corpus']}")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("commit", stats["commit"] or "local")
    table.add_row("parser load (s)", f"{stats['parser_load_s']:.2f}")
    table.add_row("graph build (s)", f"{stats['build_s']:.2f}")
    rss = stats["peak_rss_mib"]
    table.add_row("peak RSS (MiB)", f"{rss:.0f}" if rss is not None else "n/a")
    table.add_row("nodes", str(stats["nodes"]))
    table.add_row("edges", str(stats["edges"]))
    table.add_row("modules", str(stats["modules"]))
    table.add_row("files", str(stats["files"]))
    console.print(table)
    console.print(ls.INDEXING_ROW_HINT)
    console.print(markdown_row(stats), markup=False, highlight=False)


def main(
    corpus: Annotated[
        str, typer.Option(help=f"Pinned corpus to index: {sorted(CORPORA)}.")
    ] = ec.INDEXING_DEFAULT_CORPUS,
    target: Annotated[
        Path | None,
        typer.Option(help="Ad-hoc directory to index instead of a pinned corpus."),
    ] = None,
    corpus_dir: Annotated[
        Path, typer.Option(help="Cache directory for corpus checkouts.")
    ] = Path(ec.INDEXING_CORPUS_DIR),
    out_dir: Annotated[
        Path, typer.Option(help="Directory for indexing_bench.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to the corpus name.")
    ] = "",
    measure_target: Annotated[
        Path | None,
        typer.Option(hidden=True, help="Internal: measure one target, print JSON."),
    ] = None,
) -> None:
    if measure_target is not None:
        stats = measure(measure_target.resolve(), project_name or measure_target.name)
        sys.stdout.write(json.dumps(stats) + "\n")
        return
    if target is not None:
        stats = _run_child(target.resolve(), project_name or target.resolve().name)
    else:
        if corpus not in CORPORA:
            raise typer.BadParameter(
                ls.INDEXING_UNKNOWN_CORPUS.format(corpus=corpus, known=sorted(CORPORA))
            )
        spec = CORPORA[corpus]
        checkout = _ensure_corpus(spec, corpus_dir)
        stats = _run_child(checkout, project_name or spec.name)
        stats["commit"] = spec.commit
    _report(stats, out_dir)


if __name__ == "__main__":
    typer.run(main)
