from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from .. import constants as ec
from ..structure_report import render, write_outputs
from . import constants as sc
from . import logs as sl
from .corpora import SCIP_CORPORA, resolve_corpus
from .evaluate import evaluate, load_index
from .indexers import INDEXERS, IndexerUnavailable

_REPO_ROOT = Path(__file__).resolve().parents[2]


def main(
    corpus: Annotated[
        str | None,
        typer.Option(help=f"A pinned corpus: {', '.join(SCIP_CORPORA)}."),
    ] = None,
    language: Annotated[
        sc.ScipLanguage | None, typer.Option(help="Language to grade.")
    ] = None,
    target: Annotated[
        Path | None, typer.Option(help="Project root to index and grade.")
    ] = None,
    index: Annotated[
        Path | None,
        typer.Option(help="A prebuilt index.scip to grade instead of indexing."),
    ] = None,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to the target dir name.")
    ] = "",
    corpus_dir: Annotated[
        Path, typer.Option(help="Where pinned corpus checkouts are cached.")
    ] = Path(ec.DEFAULT_OUT_DIR) / sc.CORPORA_DIRNAME,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the scores CSV and diff JSON.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if corpus is not None:
        if corpus not in SCIP_CORPORA:
            logger.error(
                sl.UNKNOWN_CORPUS.format(name=corpus, known=", ".join(SCIP_CORPORA))
            )
            raise typer.Exit(code=1)
        language, target = resolve_corpus(corpus, corpus_dir, _REPO_ROOT)
    if language is None or target is None:
        logger.error(sl.NEED_TARGET)
        raise typer.Exit(code=1)
    spec = INDEXERS[language]
    target = target.resolve()
    project = project_name or target.name
    try:
        scip_index = load_index(spec, target, project, index)
    except IndexerUnavailable as unavailable:
        logger.error(str(unavailable))
        raise typer.Exit(code=1) from unavailable
    result = evaluate(spec, target, project, scip_index)
    write_outputs(
        result,
        out_dir,
        sc.SCORES_FILENAME.format(language=language.value),
        sc.DIFF_FILENAME.format(language=language.value),
    )
    render(
        result,
        sc.TABLE_TITLE.format(
            language=language.value, tool=scip_index.tool, version=scip_index.version
        ),
    )


if __name__ == "__main__":
    typer.run(main)
