# Dead-code eval. cgr's `dead-code` command reports functions/methods
# unreachable from any entry point via the shared reachability engine in
# codebase_rag.dead_code. The deterministic in-memory harness cannot query a
# database, so it runs the same engine over the captured graph and grades the
# result on controlled fixtures whose dead set is known by construction. The
# engine is unit-tested on hand-built graphs, so a fixture mismatch indicts
# cgr's CALLS graph (e.g. a missing edge flagging a live function as dead),
# not the scorer.
import json
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.dead_code import dead_code_from_graph, default_dead_code_config
from codebase_rag.types_defs import DeadCodeConfig

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.DEAD_CODE_DEFAULT_TARGET)
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)


def cgr_dead_code(target: Path, project: str, config: DeadCodeConfig) -> set[str]:
    ingestor = _capture(target, project)
    prefix = project + cs.SEPARATOR_DOT
    return dead_code_from_graph(ingestor.nodes, list(ingestor.rels), prefix, config)


def score_dead_code(cgr: set[str], oracle: set[str]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.NODE.value, ec.DEAD_CODE_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.DEAD_CODE_DIFF_PREFIX + ec.DEAD_CODE_LABEL] = DiffBucket(
            missing=sorted(oracle - cgr),
            extra=sorted(cgr - oracle),
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to report dead code for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    include_tests: Annotated[
        bool, typer.Option(help="Treat test functions/modules as roots.")
    ] = False,
    include_classes: Annotated[
        bool, typer.Option(help="Also report unreachable classes.")
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob(s) matched against a symbol's file path to exclude."),
    ] = None,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the dead-code report json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    # Corpus mode is informational: a real repo has no independent dead-code
    # oracle (true reachability needs the same call graph), so this reports
    # cgr's reachable-from-roots dead set. The graded eval lives in the tests.
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.DEAD_CODE_TARGET.format(target=target, project=project))

    config = default_dead_code_config(
        include_tests, include_classes, tuple(exclude or ())
    )
    dead = cgr_dead_code(target, project, config)
    logger.success(ls.DEAD_CODE_DONE.format(count=len(dead)))

    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / ec.DEAD_CODE_DIFF_FILENAME
    report.write_text(json.dumps(sorted(dead), indent=2), encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
