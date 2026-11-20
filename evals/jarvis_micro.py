# Call-graph precision and recall on the JARVIS micro-benchmark (Huang et al.,
# "JARVIS: Scalable and Precise Application-Centered Call Graph Construction
# for Python", arXiv 2305.05949): PyCG's 112 programs extended with context
# managers and a control-flow category, 138 at the pinned commit, each with a
# hand-written ground-truth call graph. It is the benchmark the paper reports
# PyCG and JARVIS on, so cgr's numbers here sit next to theirs.
#
# Scored edges are first-party: caller and callee are both defined in the
# program. cgr does not model builtins (`<builtin>.id`) or library internals
# as call targets, and counting them would measure that choice, not how well
# cgr resolves the program's own calls.
import json
import subprocess
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

console = Console()

Edge = tuple[str, str]


class Program(NamedTuple):
    category: str
    name: str
    source: Path
    truth: Path


class Score(NamedTuple):
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0


def checkout(cache: Path) -> Path:
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", ec.JARVIS_DATASET_URL, str(cache)], check=True
        )
    logger.info(ls.JARVIS_FETCHING.format(commit=ec.JARVIS_DATASET_COMMIT))
    subprocess.run(
        [
            "git",
            "-C",
            str(cache),
            "checkout",
            "-q",
            "--force",
            ec.JARVIS_DATASET_COMMIT,
        ],
        check=True,
    )
    return cache


def programs(root: Path) -> list[Program]:
    truth_root = root / ec.JARVIS_TRUTH_DIR
    program_root = root / ec.JARVIS_PROGRAMS_DIR
    # The dataset spells one category `new_direct calls` and its ground truth
    # `new_direct_calls`; matching paths exactly dropped its five programs.
    sources = {
        _spelling(d.relative_to(program_root)): d
        for d in program_root.glob("*/*")
        if d.is_dir()
    }
    found: list[Program] = []
    for truth in sorted(truth_root.rglob(ec.JARVIS_TRUTH_FILE)):
        rel = truth.parent.relative_to(truth_root)
        if (source := sources.get(_spelling(rel))) is not None:
            found.append(Program(rel.parts[0], "/".join(rel.parts[1:]), source, truth))
    return found


def _spelling(rel: Path) -> str:
    return rel.as_posix().replace(" ", "_")


def modules(source: Path) -> set[str]:
    """Dotted names of the program's own modules: `pkg/__init__.py` is `pkg`."""
    names: set[str] = set()
    for path in source.rglob("*" + cs.EXT_PY):
        dotted = path.relative_to(source).with_suffix("").as_posix().replace("/", ".")
        names.add(dotted.removesuffix(".__init__"))
    return names


def first_party(name: str, owned: set[str]) -> bool:
    return any(name == m or name.startswith(m + ".") for m in owned)


def truth_edges(truth: Path, owned: set[str]) -> set[Edge]:
    graph: dict[str, list[str]] = json.loads(truth.read_text(encoding="utf-8"))
    return {
        (caller, callee)
        for caller, callees in graph.items()
        for callee in callees
        if first_party(caller, owned) and first_party(callee, owned)
    }


def cgr_edges(source: Path, owned: set[str], parsers, queries) -> set[Edge]:
    ingestor = _CapturingIngestor()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=source,
        parsers=parsers,
        queries=queries,
        project_name=ec.JARVIS_PROJECT,
    ).run(force=True)
    module_qns = {
        str(uid)
        for (label, uid) in ingestor.nodes
        if label == cs.NodeLabel.MODULE.value
    }
    edges: set[Edge] = set()
    for _fl, caller, rel, _tl, callee in ingestor.rels:
        if rel != cs.RelationshipType.CALLS.value:
            continue
        a = _module_name(str(caller), module_qns)
        b = _module_name(str(callee), module_qns)
        if first_party(a, owned) and first_party(b, owned):
            edges.add((a, b))
    return edges


def _module_name(qn: str, module_qns: set[str]) -> str:
    """cgr's qualified name in the ground truth's spelling.

    The project prefix goes, and a package's own module (`bench.pkg.__init__`
    when cgr keeps the file stem) is the package; a method named `__init__`
    is left alone because its qn is not a Module node.
    """
    name = qn.removeprefix(ec.JARVIS_PROJECT + cs.SEPARATOR_DOT)
    if qn in module_qns:
        name = name.removesuffix(".__init__")
    return name


def score_program(found: set[Edge], truth: set[Edge]) -> Score:
    return Score(len(found & truth), len(found - truth), len(truth - found))


def add(a: Score, b: Score) -> Score:
    return Score(a.tp + b.tp, a.fp + b.fp, a.fn + b.fn)


def main(
    cache: Annotated[Path, typer.Option(help=ec.JARVIS_HELP_CACHE)] = Path(
        ec.JARVIS_CACHE_DIR
    ),
    out: Annotated[Path, typer.Option(help=ec.JARVIS_HELP_OUT)] = Path(
        ec.JARVIS_RESULTS_FILE
    ),
    category: Annotated[str | None, typer.Option(help=ec.JARVIS_HELP_CATEGORY)] = None,
) -> None:
    root = checkout(cache.resolve())
    logger.remove()
    parsers, queries = load_parsers()
    by_category: dict[str, Score] = {}
    total = Score(0, 0, 0)
    diffs: dict[str, dict[str, list[list[str]]]] = {}
    chosen = [p for p in programs(root) if category in (None, p.category)]
    for p in chosen:
        owned = modules(p.source)
        truth = truth_edges(p.truth, owned)
        found = cgr_edges(p.source, owned, parsers, queries)
        s = score_program(found, truth)
        by_category[p.category] = add(by_category.get(p.category, Score(0, 0, 0)), s)
        total = add(total, s)
        if s.fp or s.fn:
            diffs[f"{p.category}/{p.name}"] = {
                "missing": sorted(map(list, truth - found)),
                "extra": sorted(map(list, found - truth)),
            }
    table = Table(title=ec.JARVIS_TABLE_TITLE.format(count=len(chosen)))
    for i, col in enumerate(ec.JARVIS_COLUMNS):
        table.add_column(col, justify="left" if i == 0 else "right")
    rows = {**by_category, ec.JARVIS_TOTAL_ROW: total}
    for name, s in rows.items():
        table.add_row(
            name,
            f"{s.precision:.3f}",
            f"{s.recall:.3f}",
            str(s.tp),
            str(s.fp),
            str(s.fn),
        )
    console.print(table)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "dataset_commit": ec.JARVIS_DATASET_COMMIT,
                "programs": len(chosen),
                "scores": {
                    k: {
                        "precision": round(s.precision, 4),
                        "recall": round(s.recall, 4),
                        **s._asdict(),
                    }
                    for k, s in rows.items()
                },
                "diffs": diffs,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    console.print(ls.WROTE_SCORES.format(path=out))


if __name__ == "__main__":
    typer.run(main)
