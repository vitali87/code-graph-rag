"""Index each TraceEval program with cgr and name its CALLS the tracer's way."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from tree_sitter import Parser

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import LanguageQueries

from ..cgr_graph import CallCapturingIngestor, _CapturingIngestor
from . import constants as tc
from .corpus import ground_truth, is_source
from .naming import NameTranslator
from .normalize import normalize_edges
from .protocol import drop_class_bodies, python_class_names, see_through_anonymous
from .types_defs import CgrCall, CgrDefinition, Program, ProgramResult, TraceEdge

_DEFINITION_LABELS = tc.CALLER_LABELS | {cs.NodeLabel.CLASS.value}


@dataclass
class _Worker:
    parsers: Mapping[cs.SupportedLanguage, Parser]
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    workspace: Path


_worker: _Worker | None = None


def init_worker(workspace: str, quiet: bool = True) -> None:
    global _worker
    if quiet:
        # cgr logs every pass at DEBUG; thousands of tiny programs would bury
        # the eval's own output.
        logger.remove()
        logger.add(sys.stderr, level=tc.WORKER_LOG_LEVEL)
    root = Path(workspace) / str(os.getpid())
    # Keep cgr's per-checkout state out of the user's CGR_HOME: every program
    # is a fresh checkout and would otherwise leave a state directory behind.
    settings.CGR_HOME = root / tc.CGR_HOME_DIRNAME
    parsers, queries = load_parsers()
    _worker = _Worker(parsers=parsers, queries=queries, workspace=root)


def _stage(program: Program, workspace: Path) -> Path:
    staged = workspace / tc.PROJECT_NAME
    shutil.rmtree(staged, ignore_errors=True)
    source_root = Path(program.root)
    for path in sorted(source_root.rglob("*")):
        if path.is_file() and is_source(path):
            target = staged / path.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    return staged


def _definitions(
    ingestor: _CapturingIngestor,
) -> tuple[dict[str, CgrDefinition], dict[str, int]]:
    definitions: dict[str, CgrDefinition] = {}
    end_lines: dict[str, int] = {}
    for (label, uid), props in ingestor.nodes.items():
        path = props.get(cs.KEY_PATH)
        if label not in _DEFINITION_LABELS or path is None:
            continue
        start = props.get(cs.KEY_START_LINE)
        end = props.get(cs.KEY_END_LINE)
        qn = str(uid)
        definitions[qn] = CgrDefinition(
            label=label,
            qualified_name=qn,
            path=str(path),
            start_line=int(start) if isinstance(start, int | float) else None,
        )
        if isinstance(end, int | float):
            end_lines[qn] = int(end)
    return definitions, end_lines


def cgr_calls(
    ingestor: CallCapturingIngestor,
) -> Iterator[CgrCall]:
    for from_label, from_qn, to_label, to_qn, resolution in ingestor.calls:
        if from_label in tc.CALLER_LABELS and to_label in tc.CALLEE_LABELS:
            yield CgrCall(from_qn, to_qn, resolution)


def predicted_edges(
    language: tc.Language,
    ingestor: CallCapturingIngestor,
    read_source: Callable[[str], str],
) -> tuple[set[TraceEdge], set[TraceEdge]]:
    """cgr's call edges in TraceEval names: all of them, and the `exact` ones."""
    definitions, end_lines = _definitions(ingestor)
    translator = NameTranslator(language, definitions, end_lines, read_source)
    edges: set[TraceEdge] = set()
    exact: set[TraceEdge] = set()
    for call in cgr_calls(ingestor):
        caller = translator.translate(call.caller).owner
        callee = translator.translate(call.callee).name
        if caller is None or callee is None:
            continue
        edges.add((caller, callee))
        if call.resolution == cs.EdgeResolution.EXACT.value:
            exact.add((caller, callee))
    return edges, exact


def _static_truth(program: Program, raw: set[TraceEdge]) -> set[TraceEdge]:
    if program.language == tc.Language.PYTHON:
        root = Path(program.root)
        sources = [p for p in sorted(root.rglob("*")) if p.is_file() and is_source(p)]
        raw = drop_class_bodies(raw, python_class_names(root, sources))
    return normalize_edges(see_through_anonymous(raw), program.language)


def run_program(program: Program) -> ProgramResult:
    if _worker is None:
        raise RuntimeError(tc.WORKER_NOT_STARTED)
    raw_truth = ground_truth(Path(program.root))
    truth = frozenset(normalize_edges(raw_truth, program.language))
    truth_static = frozenset(_static_truth(program, raw_truth))
    try:
        staged = _stage(program, _worker.workspace)
        ingestor = CallCapturingIngestor()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=staged,
            parsers=_worker.parsers,
            queries=_worker.queries,
            project_name=tc.PROJECT_NAME,
        ).run(force=True)

        def read_source(path: str) -> str:
            return (staged / path).read_text(
                encoding=cs.ENCODING_UTF8, errors=tc.DECODE_ERRORS
            )

        edges, exact = predicted_edges(program.language, ingestor, read_source)
    except Exception as error:  # noqa: BLE001 - a crash is scored, not raised
        return ProgramResult(
            language=program.language,
            program_id=program.program_id,
            truth=truth,
            predicted=frozenset(),
            predicted_exact=frozenset(),
            truth_static=truth_static,
            predicted_static=frozenset(),
            error=f"{type(error).__name__}: {error}",
        )
    return ProgramResult(
        language=program.language,
        program_id=program.program_id,
        truth=truth,
        predicted=frozenset(normalize_edges(edges, program.language)),
        predicted_exact=frozenset(normalize_edges(exact, program.language)),
        truth_static=truth_static,
        predicted_static=frozenset(
            normalize_edges(see_through_anonymous(edges), program.language)
        ),
        error="",
    )


def run_programs(
    selected: list[Program], workspace: Path, workers: int, quiet: bool = True
) -> list[ProgramResult]:
    workspace.mkdir(parents=True, exist_ok=True)
    if workers > 1:
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=init_worker,
            initargs=(str(workspace), quiet),
        ) as pool:
            return list(pool.map(run_program, selected, chunksize=tc.POOL_CHUNKSIZE))
    # In-process, the worker setup touches process-wide state (CGR_HOME and
    # the loguru sinks), so it is put back for whatever runs next.
    previous_home = settings.CGR_HOME
    init_worker(str(workspace), quiet)
    try:
        return [run_program(program) for program in selected]
    finally:
        settings.CGR_HOME = previous_home
        if quiet:
            logger.remove()
            logger.add(sys.stderr, level=tc.MAIN_LOG_LEVEL)
