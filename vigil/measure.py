# Runs inside a candidate's own venv, one build per fresh interpreter, so
# in-process caches never carry between repeats and peak RSS belongs to this
# build alone. The parent reads one JSON Measurement from stdout.
import hashlib
import json
import resource
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from . import constants as vc
from .types_defs import Measurement


def canonical_lines(
    nodes: Iterable[tuple[tuple[str, object], Mapping[str, object]]],
    rels: Iterable[Sequence[object]],
) -> list[str]:
    """One sorted line per node and per relationship.

    Property values go through str() so a value that round-trips as 3 in one
    build and "3" in another still compares equal: the digest is about which
    graph was built, not about how a capture typed its scalars.
    """
    node_lines = sorted(
        json.dumps(
            [label, str(uid), sorted((k, str(v)) for k, v in props.items())],
            ensure_ascii=False,
        )
        for (label, uid), props in nodes
    )
    rel_lines = sorted(
        json.dumps([str(x) for x in r], ensure_ascii=False) for r in rels
    )
    return [*node_lines, "--", *rel_lines]


def digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode(vc.ENCODING)).hexdigest()


def _peak_rss_mib() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS, kibibytes on Linux.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(rss / divisor, 1)


def build(target: Path, corpus: str, dump: Path | None) -> Measurement:
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers
    from evals.cgr_graph import _CapturingIngestor

    t0 = time.perf_counter()
    parsers, queries = load_parsers()
    ingestor = _CapturingIngestor()
    # force: the hash cache must not turn a repeat into an empty
    # incremental update that measures nothing.
    GraphUpdater(
        ingestor=ingestor,
        repo_path=target,
        parsers=parsers,
        queries=queries,
        project_name=corpus,
    ).run(force=True)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    lines = canonical_lines(ingestor.nodes.items(), ingestor.rels)
    if dump is not None:
        dump.write_text("\n".join(lines), encoding=vc.ENCODING)
    return Measurement(
        corpus=corpus,
        build_ms=round(elapsed_ms, 1),
        peak_rss_mib=_peak_rss_mib(),
        nodes=len(ingestor.nodes),
        rels=len(ingestor.rels),
        digest=digest(lines),
    )


def main(
    target: Annotated[Path, typer.Option(help="Corpus checkout to index.")],
    corpus: Annotated[str, typer.Option(help="Corpus name, used as project.")],
    dump: Annotated[
        Path | None, typer.Option(help="Write the canonical graph lines here.")
    ] = None,
) -> None:
    # The build logs every edge at DEBUG; formatting those lines is a
    # measurable share of a build's wall time.
    logger.remove()
    typer.echo(build(target.resolve(), corpus, dump).model_dump_json())


if __name__ == "__main__":
    typer.run(main)
