"""FLOWS_TO ground truth, stage 1 (issue #1190): a hand-labelled fixture
corpus scored per language.

Nothing graded FLOWS_TO anywhere before this: the newest and subtlest edge
type had zero ground truth. Stage 1 is a small corpus in
`evals/flows_corpus/<language>/` where every source->sink flow (and every
deliberate NON-flow) was labelled by hand in `expected.json`; each language
directory is indexed as its own repo and the emitted FLOWS_TO triples
`(source_resource, sink_resource, kind)` are scored precision/recall against
the labels. The corpus is intentionally executable-small so the labels stay
reviewable; compiler-generated ground truth at scale (Roslyn AnalyzeDataFlow,
go/ssa def-use) is the staged follow-up in #1190.

The corpus already pays for itself: it caught the Scala descriptor's
subscript_type mis-wiring (every nested source call swallowed by the
member/subscript branch -> zero Scala FLOWS_TO) and the Python deep walk's
os.environ[...] subscript-source gap on its first run.

```bash
uv run python -m evals.flow_ground_truth
```
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Annotated, NamedTuple
from unittest.mock import MagicMock

import typer
from rich.console import Console
from rich.table import Table

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

CORPUS_DIR = Path(__file__).parent / "flows_corpus"
EXPECTED_FILE = CORPUS_DIR / "expected.json"

console = Console()


class FlowTriple(NamedTuple):
    source: str
    sink: str
    kind: str


class FlowScore(NamedTuple):
    language: str
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float


def load_expected() -> dict[str, set[FlowTriple]]:
    payload = json.loads(EXPECTED_FILE.read_text(encoding="utf-8"))
    return {
        language: {FlowTriple(*entry) for entry in entries}
        for language, entries in payload.items()
    }


def cgr_flow_triples(language_dir: Path) -> set[FlowTriple]:
    parsers, queries = load_parsers()
    ingestor = MagicMock()
    with tempfile.TemporaryDirectory() as scratch:
        # Index a COPY: GraphUpdater writes cache artifacts (.cgr-hash-cache,
        # parser fingerprint) into the repo root, which must never dirty the
        # committed corpus fixtures.
        run_dir = Path(scratch) / language_dir.name
        shutil.copytree(language_dir, run_dir)
        GraphUpdater(
            ingestor=ingestor,
            repo_path=run_dir,
            parsers=parsers,
            queries=queries,
            capture=resolve_capture([cs.CaptureGroup.IO.value]),
        ).run(force=True)
    flows: set[FlowTriple] = set()
    for call in ingestor.ensure_relationship_batch.call_args_list:
        if str(call.args[1]) != cs.RelationshipType.FLOWS_TO.value:
            continue
        properties = call.kwargs.get("properties") or (
            call.args[3] if len(call.args) > 3 else {}
        )
        kind = str((properties or {}).get("kind", ""))
        flows.add(FlowTriple(str(call.args[0][2]), str(call.args[2][2]), kind))
    return flows


def score_language(
    cgr: set[FlowTriple], expected: set[FlowTriple]
) -> tuple[int, int, int, float, float]:
    tp = len(cgr & expected)
    fp = len(cgr - expected)
    fn = len(expected - cgr)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return tp, fp, fn, precision, recall


def score_corpus() -> list[FlowScore]:
    expected = load_expected()
    scores: list[FlowScore] = []
    for language in sorted(expected):
        language_dir = CORPUS_DIR / language
        cgr = cgr_flow_triples(language_dir)
        tp, fp, fn, precision, recall = score_language(cgr, expected[language])
        scores.append(FlowScore(language, tp, fp, fn, precision, recall))
    return scores


def main(
    results: Annotated[Path, typer.Option(help="CSV output path.")] = Path(
        __file__
    ).parent
    / "results"
    / "flows.csv",
) -> None:
    scores = score_corpus()
    table = Table(title="FLOWS_TO vs hand-labelled corpus (stage 1, #1190)")
    for col in ("language", "tp", "fp", "fn", "precision", "recall"):
        table.add_column(col, justify="right")
    for s in scores:
        table.add_row(
            s.language,
            str(s.tp),
            str(s.fp),
            str(s.fn),
            f"{s.precision:.4f}",
            f"{s.recall:.4f}",
        )
    console.print(table)
    results.parent.mkdir(parents=True, exist_ok=True)
    lines = ["language,tp,fp,fn,precision,recall"]
    lines += [
        f"{s.language},{s.tp},{s.fp},{s.fn},{s.precision:.4f},{s.recall:.4f}"
        for s in scores
    ]
    results.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
