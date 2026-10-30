"""The pinned corpora the committed SCIP scores were measured on."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from ..indexing_bench import CorpusSpec, _ensure_corpus
from . import constants as sc


class ScipCorpus(NamedTuple):
    language: sc.ScipLanguage
    # A pinned external checkout, or None for a directory of this repository.
    checkout: CorpusSpec | None
    in_repo: str = ""


SCIP_CORPORA: dict[str, ScipCorpus] = {
    "codebase_rag": ScipCorpus(sc.ScipLanguage.PYTHON, None, "codebase_rag"),
    "cgr_graph_rs": ScipCorpus(sc.ScipLanguage.RUST, None, "rust/cgr_graph_rs"),
    "zod": ScipCorpus(
        sc.ScipLanguage.TYPESCRIPT,
        CorpusSpec(
            name="zod",
            url="https://github.com/colinhacks/zod",
            commit="923af801fde9f033cfd7e0e753b421a554fe3be8",  # tag v4.1.9
            subdir="packages/zod/src/v4",
        ),
    ),
    "axios": ScipCorpus(
        sc.ScipLanguage.JAVASCRIPT,
        CorpusSpec(
            name="axios",
            url="https://github.com/axios/axios",
            commit="e5a33366d75b65f88052b230b103731eb7dcb793",  # tag v1.12.2
            subdir="lib",
        ),
    ),
}


def resolve_corpus(
    name: str, corpus_dir: Path, repo_root: Path
) -> tuple[sc.ScipLanguage, Path]:
    corpus = SCIP_CORPORA[name]
    if corpus.checkout is None:
        return corpus.language, repo_root / corpus.in_repo
    return corpus.language, _ensure_corpus(corpus.checkout, corpus_dir)
