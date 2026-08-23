# Agentic A/B benchmark (issue #1374): same model, same agent loop, only the
# toolset differs — cgr graph tools versus a grep/read-file baseline. Each run
# reports, per condition: answer accuracy (per-question F1 against an ast
# oracle, plus strict exact-match rate), wall-clock seconds per question
# (mean/p50/p95), and model tokens per question. One harness therefore fills
# the resolved-rate, latency, and token rows of the README benchmark table.
#
# Questions are derived mechanically from the retrieval eval's oracle over a
# pinned corpus: "which files contain a call to first-party symbol X?" has a
# ground-truth answer (the oracle's caller-file set), so grading needs no LLM
# judge and the accuracy number is as defensible as the retrieval F1.
# The two conditions are supersets on purpose: "grep" is shell (ripgrep/grep)
# plus read_file/list_directory — what any coding agent already has — and
# "graph" is those SAME tools plus cgr's graph tools. The measured delta is
# therefore exactly what a user asks before adding cgr as an MCP server: does
# bolting the graph tools onto my agent make it better, cheaper, faster?
import ast
import asyncio
import json
import random
import re
import time
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from . import constants as ec
from . import logs as ls
from .retrieval import (
    _callee_name,
    first_party_property_names,
    first_party_symbols,
    oracle_call_edges,
    parse_py_trees,
)
from .types_defs import QAGrade, QARecord

console = Console()


class Condition(StrEnum):
    GRAPH = "graph"
    GREP = "grep"


class QType(StrEnum):
    CALLS = "calls"
    MULTIHOP = "multihop"


SYSTEM_PROMPT = (
    "You answer factual questions about the source code of the repository "
    "rooted at {root}. Investigate with the tools available to you. When you "
    "know the answer, reply with ONLY the repo-relative file paths, one per "
    "line — no prose, no bullets."
)

QUESTION_TEMPLATE = (
    "Which Python files in this repository contain at least one call to the "
    "first-party callable named `{name}` (a function, method, or class "
    "constructor defined in this repository)? Answer with the repo-relative "
    "paths only, one per line. Do not include the file(s) that merely define "
    "`{name}` unless they also call it."
)

MULTIHOP_QUESTION_TEMPLATE = (
    "Which Python files in this repository contain at least one call to the "
    "first-party callable named `{name}` (a function, method, or class "
    "constructor defined in this repository), OR at least one call to a "
    "first-party function or method that itself directly calls `{name}`? "
    "Include both the direct callers and those one-hop indirect callers. "
    "Answer with the repo-relative paths only, one per line. Do not include "
    "files that merely define `{name}` or an intermediate without calling one "
    "of them."
)

# Candidate filter: enough caller files to be non-trivial, few enough to grade
# crisply, and a distinctive name so the question is unambiguous for both
# conditions (short names collide with builtins and drown grep in noise).
MIN_CALLER_FILES = 2
MAX_CALLER_FILES = 8
MIN_NAME_LEN = 8
# Multi-hop answers span more files than single-hop before becoming a grading
# swamp, so the cap is looser than MAX_CALLER_FILES.
MULTIHOP_MAX_FILES = 12
_LAMBDA_SCOPE = "<lambda>"

_PATH_TOKEN = re.compile(r"[\w./-]+\.py\b")


class QACase(NamedTuple):
    name: str
    question: str
    expected_files: frozenset[str]


def build_cases(target: Path, sample: int, seed: int = 0) -> list[QACase]:
    trees, _files = parse_py_trees(target)
    first_party = first_party_symbols(trees)
    properties = first_party_property_names(trees)
    edges = oracle_call_edges(trees, first_party, None)
    by_name: dict[str, set[str]] = {}
    for edge in edges:
        by_name.setdefault(edge.target_name, set()).add(edge.source.file)
    candidates = [
        (name, files)
        for name, files in sorted(by_name.items())
        if MIN_CALLER_FILES <= len(files) <= MAX_CALLER_FILES
        and len(name) >= MIN_NAME_LEN
        and not name.startswith("_")
        # A property read "calls" the getter without parens; that nuance is
        # unfair to the grep condition and confusing in a question, so
        # property names are excluded from the question universe entirely.
        and name not in properties
    ]
    rng = random.Random(seed)
    picked = candidates if len(candidates) <= sample else rng.sample(candidates, sample)
    return [
        QACase(name, QUESTION_TEMPLATE.format(name=name), frozenset(files))
        for name, files in sorted(picked)
    ]


class _CallSite(NamedTuple):
    file: str
    # Name of the innermost enclosing def, None at module/class level, or
    # _LAMBDA_SCOPE inside a lambda (whose deferred body makes "the enclosing
    # function calls X" unknowable by name, poisoning the case).
    func: str | None
    callee: str


def _definition_time_exprs(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.expr]:
    # Decorators, argument defaults, and annotations evaluate at DEFINITION
    # time in the enclosing scope; only the body runs inside the new function
    # (Greptile review on PR #1388).
    args = fn.args
    exprs: list[ast.expr | None] = [
        *fn.decorator_list,
        *args.defaults,
        *args.kw_defaults,
        fn.returns,
        *(a.annotation for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)),
    ]
    exprs.extend(a.annotation for a in (args.vararg, args.kwarg) if a is not None)
    return [e for e in exprs if e is not None]


def _visit_call_site(
    rel: str,
    node: ast.AST,
    stack: list[str],
    first_party: set[str],
    sites: list[_CallSite],
) -> None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        for expr in _definition_time_exprs(node):
            _visit_call_site(rel, expr, stack, first_party, sites)
        inner = [*stack, node.name]
        for stmt in node.body:
            _visit_call_site(rel, stmt, inner, first_party, sites)
        return
    if isinstance(node, ast.Lambda):
        for expr in (*node.args.defaults, *node.args.kw_defaults):
            if expr is not None:
                _visit_call_site(rel, expr, stack, first_party, sites)
        _visit_call_site(rel, node.body, [*stack, _LAMBDA_SCOPE], first_party, sites)
        return
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Lambda):
            # `(lambda ...: body)(args)` runs the body immediately in THIS
            # scope, so its calls belong to the enclosing function, not an
            # anonymous lambda scope (Greptile review on PR #1388). Visit the
            # arguments here and the body with the current stack, bypassing
            # the deferred-lambda marker below.
            for arg in (*node.args, *(kw.value for kw in node.keywords)):
                _visit_call_site(rel, arg, stack, first_party, sites)
            lam = node.func
            for expr in (*lam.args.defaults, *lam.args.kw_defaults):
                if expr is not None:
                    _visit_call_site(rel, expr, stack, first_party, sites)
            _visit_call_site(rel, lam.body, stack, first_party, sites)
            return
        if (name := _callee_name(node.func)) and name in first_party:
            sites.append(_CallSite(rel, stack[-1] if stack else None, name))
    for child in ast.iter_child_nodes(node):
        _visit_call_site(rel, child, stack, first_party, sites)


def _collect_call_sites(
    rel: str,
    node: ast.AST,
    stack: list[str],
    first_party: set[str],
    sites: list[_CallSite],
) -> None:
    for child in ast.iter_child_nodes(node):
        _visit_call_site(rel, child, stack, first_party, sites)


def _definition_counts(trees: list[tuple[str, ast.Module]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for _rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                counts[node.name] += 1
    return counts


def build_multihop_cases(target: Path, sample: int, seed: int = 0) -> list[QACase]:
    """Depth-2 questions: files that call `name` or call a caller of `name`.

    The oracle matches call sites by name, so a case is only kept when its
    ground truth is unambiguous under name matching: every named enclosing
    function of a direct call site must be defined exactly once in the repo
    (else "callers of that intermediate" may target a different definition),
    no direct call site may sit inside a lambda, and hop 2 must add at least
    one file beyond the direct callers (else the question collapses to the
    single-hop variant).
    """
    trees, _files = parse_py_trees(target)
    first_party = first_party_symbols(trees)
    properties = first_party_property_names(trees)
    def_counts = _definition_counts(trees)
    sites: list[_CallSite] = []
    for rel, tree in trees:
        _collect_call_sites(rel, tree, [], first_party, sites)
    caller_files: dict[str, set[str]] = {}
    enclosing: dict[str, set[str | None]] = {}
    for site in sites:
        caller_files.setdefault(site.callee, set()).add(site.file)
        enclosing.setdefault(site.callee, set()).add(site.func)
    candidates: list[QACase] = []
    for name in sorted(caller_files):
        if len(name) < MIN_NAME_LEN or name.startswith("_") or name in properties:
            continue
        direct = caller_files[name]
        if not (MIN_CALLER_FILES <= len(direct) <= MAX_CALLER_FILES):
            continue
        scopes = enclosing[name]
        if _LAMBDA_SCOPE in scopes:
            continue
        intermediates = {fn for fn in scopes if fn and fn != name}
        if any(def_counts[fn] != 1 or fn in properties for fn in intermediates):
            continue
        hop2 = set().union(*(caller_files.get(fn, set()) for fn in intermediates))
        expected = direct | hop2
        if not hop2 - direct or len(expected) > MULTIHOP_MAX_FILES:
            continue
        candidates.append(
            QACase(
                name,
                MULTIHOP_QUESTION_TEMPLATE.format(name=name),
                frozenset(expected),
            )
        )
    rng = random.Random(seed)
    picked = candidates if len(candidates) <= sample else rng.sample(candidates, sample)
    return sorted(picked)


def extract_answer_files(answer: str, expected_root: str = "") -> frozenset[str]:
    found = set()
    for token in _PATH_TOKEN.findall(answer):
        path = token.lstrip("./")
        if expected_root and path.startswith(expected_root + "/"):
            path = path[len(expected_root) + 1 :]
        found.add(path)
    return frozenset(found)


def grade_answer_files(answered: frozenset[str], expected: frozenset[str]) -> QAGrade:
    tp = len(answered & expected)
    precision = tp / len(answered) if answered else 0.0
    recall = tp / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return QAGrade(
        f1=round(f1, 4),
        exact=answered == expected,
        answered=sorted(answered),
        expected=sorted(expected),
    )


def latency_percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
    return ordered[idx]


class UsageMeter:
    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def add(self, usage: object) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0


class _MeteredAgent:
    # Wraps the CypherGenerator's inner agent so the graph condition's hidden
    # second-model spend is counted; without this the token comparison would
    # flatter the graph condition.
    __slots__ = ("_inner", "_meter")

    def __init__(self, inner: object, meter: UsageMeter) -> None:
        self._inner = inner
        self._meter = meter

    async def run(self, *args: object, **kwargs: object) -> object:
        result = await self._inner.run(*args, **kwargs)  # type: ignore[attr-defined]
        self._meter.add(result.usage)
        return result


def _build_tools(
    condition: Condition,
    root: Path,
    ingestor: object | None,
    project_name: str,
    cypher_meter: UsageMeter,
) -> list[object]:
    from codebase_rag.config import settings
    from codebase_rag.services.llm import CypherGenerator
    from codebase_rag.tools.directory_lister import (
        DirectoryLister,
        create_directory_lister_tool,
    )
    from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
    from codebase_rag.tools.shell_command import (
        ShellCommander,
        create_noninteractive_shell_command_tool,
    )

    commander = ShellCommander(
        project_root=str(root),
        timeout=settings.SHELL_COMMAND_TIMEOUT,
    )
    tools: list[object] = [
        # A benchmark run has no operator to approve commands, so anything
        # that would need approval is denied (never yolo-bypassed): the model
        # only needs read-only search commands, and a mutating command would
        # corrupt the pinned corpus (Greptile security review on PR #1388).
        create_noninteractive_shell_command_tool(commander),
        create_file_reader_tool(FileReader(project_root=str(root))),
        create_directory_lister_tool(DirectoryLister(project_root=str(root))),
    ]
    if condition is Condition.GRAPH:
        from codebase_rag.tools.codebase_query import create_query_tool
        from codebase_rag.tools.semantic_search import (
            create_get_function_source_tool,
        )

        cypher_gen = CypherGenerator(active_projects=[project_name])
        cypher_gen.agent = _MeteredAgent(cypher_gen.agent, cypher_meter)
        quiet = Console(stderr=True, quiet=True)
        tools.append(create_query_tool(ingestor, cypher_gen, quiet))  # type: ignore[arg-type]
        tools.append(create_get_function_source_tool(ingestor))  # type: ignore[arg-type]
    return tools


def _build_agent(tools: list[object], root: Path) -> object:
    from pydantic_ai import Agent

    from codebase_rag.config import settings
    from codebase_rag.providers.base import get_provider_from_config

    config = settings.active_orchestrator_config
    model = get_provider_from_config(config).create_model(config.model_id)
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT.format(root=root),
        tools=tools,
        output_type=str,
        retries=settings.AGENT_RETRIES,
    )


async def _run_condition(
    condition: Condition,
    cases: list[QACase],
    root: Path,
    ingestor: object | None,
    project_name: str,
    records_path: Path | None = None,
) -> list[QARecord]:
    cypher_meter = UsageMeter()
    tools = _build_tools(condition, root, ingestor, project_name, cypher_meter)
    agent = _build_agent(tools, root)
    records: list[QARecord] = []
    for i, case in enumerate(cases, 1):
        cypher_before = (cypher_meter.input_tokens, cypher_meter.output_tokens)
        start = time.perf_counter()
        error = ""
        answer = ""
        in_tokens = out_tokens = 0
        try:
            result = await asyncio.wait_for(
                agent.run(case.question, message_history=[]),  # type: ignore[attr-defined]
                timeout=ec.AGENTIC_QA_TIMEOUT_S,
            )
            answer = str(result.output)
            in_tokens = result.usage.input_tokens or 0
            out_tokens = result.usage.output_tokens or 0
        except Exception as exc:  # one bad question must not sink the run
            error = f"{type(exc).__name__}: {exc}"
        seconds = round(time.perf_counter() - start, 2)
        in_tokens += cypher_meter.input_tokens - cypher_before[0]
        out_tokens += cypher_meter.output_tokens - cypher_before[1]
        graded = grade_answer_files(extract_answer_files(answer), case.expected_files)
        records.append(
            QARecord(
                condition=condition.value,
                name=case.name,
                f1=graded["f1"],
                exact=graded["exact"],
                seconds=seconds,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                answered=graded["answered"],
                expected=graded["expected"],
                error=error,
            )
        )
        # A model run can take hours; append each record as it lands so a
        # crash mid-run loses one question, not the whole benchmark.
        if records_path is not None:
            with records_path.open("a") as sink:
                sink.write(json.dumps(records[-1]) + "\n")
        logger.info(
            ls.AGENTIC_QA_PROGRESS.format(
                condition=condition.value,
                done=i,
                total=len(cases),
                name=case.name,
                f1=graded["f1"],
                seconds=seconds,
            )
        )
    return records


def summarize_qa_records(records: list[QARecord]) -> dict[str, float]:
    if not records:
        return {}
    seconds = [r["seconds"] for r in records]
    return {
        "questions": len(records),
        "mean_f1": round(sum(r["f1"] for r in records) / len(records), 4),
        "exact_rate": round(sum(1 for r in records if r["exact"]) / len(records), 4),
        "errors": sum(1 for r in records if r["error"]),
        "mean_seconds": round(sum(seconds) / len(seconds), 2),
        "p50_seconds": round(latency_percentile(seconds, 50), 2),
        "p95_seconds": round(latency_percentile(seconds, 95), 2),
        "mean_input_tokens": round(
            sum(r["input_tokens"] for r in records) / len(records)
        ),
        "mean_output_tokens": round(
            sum(r["output_tokens"] for r in records) / len(records)
        ),
    }


def _run_fingerprint(
    corpus: str,
    commit: str,
    qtype: str,
    sample: int,
    seed: int,
    config: object,
    *,
    agent_retries: int | None = None,
    shell_timeout: int | None = None,
) -> dict[str, object]:
    # Backend identity beyond the model id: the same model served through a
    # different provider, endpoint, provider type, project, or region has
    # different latency and token behavior, so records from one backend must
    # never resume into a run on another (Greptile review on PR #1388), and
    # thinking_budget changes model behavior the same way (CodeRabbit review
    # on PR #1388). Execution settings that shape a record's outcome (agent
    # retry count, shell probe timeout) are part of the identity too
    # (Greptile review on PR #1388). All fields are non-secret configuration.
    return {
        "corpus": corpus,
        "commit": commit,
        "qtype": qtype,
        "sample": sample,
        "seed": seed,
        "model": getattr(config, "model_id", None),
        "provider": getattr(config, "provider", None),
        "endpoint": getattr(config, "endpoint", None),
        "provider_type": getattr(config, "provider_type", None),
        "project": getattr(config, "project_id", None),
        "region": getattr(config, "region", None),
        "thinking_budget": getattr(config, "thinking_budget", None),
        "agent_retries": agent_retries,
        "shell_timeout": shell_timeout,
    }


def _init_records_file(
    records_path: Path, fingerprint: dict[str, object], resume: bool
) -> dict[str, list[QARecord]]:
    """Prepare the JSONL records file and load resumable prior records.

    The first line of the file is a fingerprint header describing the run
    configuration; --resume refuses a file whose header does not match, so
    records from a different corpus, commit, question set, seed, or model can
    never be merged into one summary. A fresh run truncates the file instead
    of appending, so a crashed-then-rerun file cannot double-count questions.
    Loading keeps only the LAST record per (condition, name). A completed
    answer or a timeout is a real benchmark outcome and is kept; an API-error
    record (e.g. exhausted credits) is not — the question is re-run so the
    final data holds no infrastructure failures. (Greptile P1 + CodeRabbit
    review on PR #1388.)
    """
    header = json.dumps({"fingerprint": fingerprint})
    if not resume or not records_path.exists():
        records_path.write_text(header + "\n")
        return {}
    lines = records_path.read_text().splitlines()
    stored: object = None
    if lines:
        try:
            stored = json.loads(lines[0]).get("fingerprint")
        except (json.JSONDecodeError, AttributeError):
            stored = None
    if stored != fingerprint:
        raise typer.BadParameter(
            ls.AGENTIC_QA_RESUME_MISMATCH.format(
                path=records_path, stored=stored, current=fingerprint
            )
        )
    latest: dict[tuple[str, str], QARecord] = {}
    for line in lines[1:]:
        try:
            rec: QARecord = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not rec["error"] or rec["error"].startswith("TimeoutError"):
            latest[(rec["condition"], rec["name"])] = rec
    prior: dict[str, list[QARecord]] = {}
    for (condition, _name), rec in latest.items():
        prior.setdefault(condition, []).append(rec)
    return prior


def _reindex_into_memgraph(root: Path, project_name: str, ingestor: object) -> None:
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
    ).run(force=True)


def _print_summary(results: dict[str, dict[str, float]]) -> None:
    table = Table(title="Agentic QA — graph vs grep")
    table.add_column("metric")
    for condition in results:
        table.add_column(condition, justify="right")
    metrics = next(iter(results.values()), {})
    for key in metrics:
        table.add_row(key, *(str(results[c].get(key, "")) for c in results))
    console.print(table)


def main(
    corpus: Annotated[str, typer.Option(help="Pinned corpus name.")] = "django",
    condition: Annotated[str, typer.Option(help="graph, grep, or both.")] = "both",
    qtype: Annotated[
        str, typer.Option(help="Question type: calls (direct) or multihop (depth 2).")
    ] = QType.CALLS.value,
    sample: Annotated[
        int, typer.Option(help="Number of oracle-derived questions.")
    ] = ec.AGENTIC_DEFAULT_SAMPLE,
    seed: Annotated[int, typer.Option(help="Sampling seed.")] = 0,
    dry_run: Annotated[
        bool, typer.Option(help="Print the question set and exit (no model).")
    ] = False,
    reindex: Annotated[
        bool, typer.Option(help="(Re)index the corpus into Memgraph first.")
    ] = False,
    resume: Annotated[
        bool,
        typer.Option(
            help="Skip questions already present in the records file (crash "
            "recovery). API-error records are retried; completed answers and "
            "timeouts are kept."
        ),
    ] = False,
    corpus_dir: Annotated[
        Path, typer.Option(help="Cache directory for corpus checkouts.")
    ] = Path(ec.INDEXING_CORPUS_DIR),
    out_dir: Annotated[
        Path, typer.Option(help="Directory for agentic_qa.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    from .indexing_bench import CORPORA, _ensure_corpus

    if corpus not in CORPORA:
        raise typer.BadParameter(
            ls.INDEXING_UNKNOWN_CORPUS.format(corpus=corpus, known=sorted(CORPORA))
        )
    spec = CORPORA[corpus]
    root = _ensure_corpus(spec, corpus_dir)
    builder = {QType.CALLS: build_cases, QType.MULTIHOP: build_multihop_cases}[
        QType(qtype)
    ]
    cases = builder(root, sample, seed)
    logger.info(ls.AGENTIC_QA_CASES.format(count=len(cases), corpus=corpus))
    if dry_run:
        for case in cases:
            console.print(
                f"{case.name}: {len(case.expected_files)} files",
                markup=False,
            )
        return

    conditions = (
        [Condition(condition)]
        if condition != "both"
        else [Condition.GRAPH, Condition.GREP]
    )
    from codebase_rag.config import settings
    from codebase_rag.services.graph_service import MemgraphIngestor

    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if QType(qtype) is QType.CALLS else f"_{qtype}"
    records_path = out_dir / ec.AGENTIC_RECORDS_FILE.format(suffix=suffix)
    fingerprint = _run_fingerprint(
        corpus,
        spec.commit,
        qtype,
        sample,
        seed,
        settings.active_orchestrator_config,
        agent_retries=settings.AGENT_RETRIES,
        shell_timeout=settings.SHELL_COMMAND_TIMEOUT,
    )
    prior = _init_records_file(records_path, fingerprint, resume)
    done = {(c, r["name"]) for c, rs in prior.items() for r in rs}

    needs_graph = Condition.GRAPH in conditions
    results: dict[str, dict[str, float]] = {}
    all_records: list[QARecord] = []
    if needs_graph:
        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST,
            port=settings.MEMGRAPH_PORT,
            batch_size=settings.MEMGRAPH_BATCH_SIZE,
        ) as ingestor:
            # The CLI path creates constraints and indexes before ingesting;
            # this harness must too, or every generated query runs against an
            # unindexed graph and the latency numbers measure Memgraph scans,
            # not the toolset.
            ingestor.ensure_constraints()
            if reindex:
                logger.info(ls.AGENTIC_QA_REINDEXING.format(corpus=corpus))
                _reindex_into_memgraph(root, spec.name, ingestor)
            for cond in conditions:
                todo = [c for c in cases if (cond.value, c.name) not in done]
                records = prior.get(cond.value, []) + asyncio.run(
                    _run_condition(
                        cond,
                        todo,
                        root,
                        ingestor if cond is Condition.GRAPH else None,
                        spec.name,
                        records_path,
                    )
                )
                results[cond.value] = summarize_qa_records(records)
                all_records.extend(records)
    else:
        for cond in conditions:
            todo = [c for c in cases if (cond.value, c.name) not in done]
            records = prior.get(cond.value, []) + asyncio.run(
                _run_condition(cond, todo, root, None, spec.name, records_path)
            )
            results[cond.value] = summarize_qa_records(records)
            all_records.extend(records)

    out_path = out_dir / ec.AGENTIC_RESULTS_FILE.format(suffix=suffix)
    payload = {
        "corpus": corpus,
        "commit": spec.commit,
        "qtype": qtype,
        "sample": len(cases),
        "seed": seed,
        "summary": results,
        "records": all_records,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    logger.success(ls.WROTE_SCORES.format(path=out_path))
    _print_summary(results)


if __name__ == "__main__":
    typer.run(main)
