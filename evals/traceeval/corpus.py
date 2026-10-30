"""Fetch, verify and read the pinned TraceEval corpus."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from loguru import logger

from codebase_rag import constants as cs

from ..indexing_bench import CorpusSpec, _ensure_corpus
from . import constants as tc
from . import logs as tl
from .types_defs import Program, TraceEdge

CORPUS = CorpusSpec(
    name=tc.CORPUS_NAME, url=tc.CORPUS_URL, commit=tc.CORPUS_COMMIT, subdir=""
)


class CorpusError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(tc.HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename
            if name.startswith(tc.ZIP_SKIP_PREFIX):
                continue
            parts = PurePosixPath(name).parts
            if not parts or parts[-1] in tc.ZIP_SKIP_NAMES:
                continue
            # Refuse anything that would land outside the destination: the
            # archive is downloaded, so its member names are untrusted.
            if PurePosixPath(name).is_absolute() or ".." in parts:
                raise CorpusError(tl.UNSAFE_MEMBER.format(name=name))
            bundle.extract(member, destination)


def ensure_corpus(corpus_dir: Path) -> Path:
    """The pinned checkout's extracted `benchmark/` root, fetched on first use."""
    checkout = _ensure_corpus(CORPUS, corpus_dir)
    archive = checkout / tc.CORPUS_ZIP
    # Beside the checkout, not in it: an untracked tree inside makes the
    # pinned checkout read as dirty, which resets it on every run.
    extracted = corpus_dir / tc.EXTRACT_DIRNAME.format(name=CORPUS.name)
    marker = extracted / tc.EXTRACT_DONE_MARKER
    done = marker.read_text(encoding=cs.ENCODING_UTF8) if marker.exists() else ""
    if done != tc.CORPUS_ZIP_SHA256:
        digest = _sha256(archive)
        if digest != tc.CORPUS_ZIP_SHA256:
            raise CorpusError(
                tl.ARCHIVE_HASH_MISMATCH.format(
                    path=archive, expected=tc.CORPUS_ZIP_SHA256, actual=digest
                )
            )
        logger.info(tl.EXTRACTING.format(path=archive))
        shutil.rmtree(extracted, ignore_errors=True)
        _extract(archive, extracted)
        marker.write_text(digest, encoding=cs.ENCODING_UTF8)
    return extracted / tc.BENCHMARK_DIRNAME


def split_ids(corpus_dir: Path, split: tc.Split) -> dict[tc.Language, list[str]]:
    path = (
        corpus_dir
        / CORPUS.name
        / tc.CORPUS_SPLIT_DIR
        / tc.SPLIT_IDS_FILENAME.format(split=split.value)
    )
    raw: dict[str, list[str]] = json.loads(path.read_text(encoding=cs.ENCODING_UTF8))
    return {language: list(raw.get(language.value, [])) for language in tc.Language}


def programs(
    benchmark: Path,
    ids: dict[tc.Language, list[str]],
    languages: list[tc.Language],
    limit: int | None = None,
) -> list[Program]:
    selected: list[Program] = []
    for language in languages:
        chosen = ids.get(language, [])
        for program_id in chosen[:limit] if limit else chosen:
            root = benchmark / language.value / program_id
            if not (root / tc.GROUND_TRUTH_FILENAME).is_file():
                logger.warning(tl.PROGRAM_MISSING.format(path=root))
                continue
            selected.append(Program(language, program_id, str(root)))
    return selected


def ground_truth(root: Path) -> set[TraceEdge]:
    """The tracer-witnessed edges of one program, as (caller, callee) pairs."""
    graph: dict[str, list[str]] = json.loads(
        (root / tc.GROUND_TRUTH_FILENAME).read_text(encoding=cs.ENCODING_UTF8)
    )
    return {
        (caller, callee)
        for caller, callees in graph.items()
        if isinstance(callees, list)
        for callee in callees
        if isinstance(callee, str)
    }


def is_source(path: Path) -> bool:
    name = path.name
    return name not in tc.NON_SOURCE_FILENAMES and not name.startswith(
        tc.NON_SOURCE_PREFIXES
    )
