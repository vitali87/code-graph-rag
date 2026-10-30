from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from .. import constants as ec
from ..structure_report import render, write_outputs
from . import constants as tc
from . import logs as tl
from .corpus import ensure_corpus, programs, split_ids
from .runner import run_programs
from .score import score_results


def main(
    split: Annotated[
        tc.Split, typer.Option(help="TraceEval split to score against.")
    ] = tc.Split.TEST,
    language: Annotated[
        list[tc.Language] | None,
        typer.Option(help="Languages to score (repeatable); default all three."),
    ] = None,
    corpus_dir: Annotated[
        Path,
        typer.Option(help="Where the pinned TraceEval checkout is cached."),
    ] = Path(ec.DEFAULT_OUT_DIR) / tc.CORPORA_DIRNAME,
    limit: Annotated[
        int | None,
        typer.Option(help="Score only the first N programs per language."),
    ] = None,
    workers: Annotated[
        int, typer.Option(help="Parallel cgr indexing processes.")
    ] = tc.DEFAULT_WORKERS,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the scores CSV and diff JSON.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    languages = list(language) if language else list(tc.Language)
    benchmark = ensure_corpus(corpus_dir)
    selected = programs(benchmark, split_ids(corpus_dir, split), languages, limit)
    logger.info(
        tl.SELECTED.format(
            split=split.value,
            count=len(selected),
            languages=", ".join(lang.value for lang in languages),
        )
    )
    logger.info(tl.RUNNING.format(workers=workers))
    results = run_programs(selected, out_dir / tc.WORKSPACE_DIRNAME, workers)
    if failed := [r for r in results if r.error]:
        for failure in failed:
            logger.warning(
                tl.PROGRAM_FAILED.format(
                    language=failure.language.value,
                    program=failure.program_id,
                    error=failure.error,
                )
            )
        logger.warning(tl.FAILED_SUMMARY.format(count=len(failed)))
    logger.success(tl.DONE.format(count=len(results)))
    result = score_results(results)
    write_outputs(result, out_dir, tc.SCORES_FILENAME, tc.DIFF_FILENAME)
    render(result, tc.TABLE_TITLE.format(split=split.value))


if __name__ == "__main__":
    typer.run(main)
