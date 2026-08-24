# Duplicates eval. cgr's `duplicates` command groups structural clones via
# the engine in codebase_rag.duplicates, which reads fingerprint rows from
# the database. The in-memory harness cannot query a database, so a small
# adapter replays the two duplicate Cypher fetches over the captured graph
# and the same engine runs on top; graded fixtures know their clone pairs by
# construction (codebase_rag/tests/test_duplicates_eval.py). The engine is
# unit-tested, so a fixture mismatch indicts fingerprint ingest or the
# capture, not the grouping.
import json
from itertools import combinations
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.duplicates import (
    collect_duplicates_with_coverage,
    default_duplicates_config,
)
from codebase_rag.types_defs import (
    DuplicateGroup,
    DuplicatesConfig,
    DuplicatesReport,
    PropertyDict,
    PropertyValue,
    ResultRow,
)

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.DUPLICATES_DEFAULT_TARGET)
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
_DUPLICATE_LABELS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})
_ROW_KEYS = (
    cs.KEY_QUALIFIED_NAME,
    cs.KEY_NAME,
    cs.KEY_PATH,
    cs.KEY_START_LINE,
    cs.KEY_START_COL,
    cs.KEY_END_LINE,
    cs.KEY_AST_FINGERPRINT,
    cs.KEY_AST_FINGERPRINT_NODES,
    cs.KEY_AST_BRANCH_FINGERPRINTS,
)


class _GraphRows:
    """Replays the duplicate Cypher fetches over a captured node map.

    Mirrors CYPHER_DUPLICATE_FINGERPRINTS / CYPHER_DUPLICATE_SKIPPED_COUNT
    exactly: Function|Method labels, qualified_name prefix match, split on
    ast_fingerprint presence. Any other query is out of contract and returns
    nothing, like the base capture ingestor.
    """

    def __init__(self, nodes: dict[tuple[str, PropertyValue], PropertyDict]) -> None:
        self._nodes = nodes

    def fetch_all(
        self, query: str, params: dict[str, PropertyValue] | None = None
    ) -> list[ResultRow]:
        prefix = str((params or {}).get(cs.KEY_PROJECT_PREFIX) or "")
        if query == cq.CYPHER_DUPLICATE_FINGERPRINTS:
            return [
                _row(label, props)
                for (label, _uid), props in self._nodes.items()
                if _in_scope(label, props, prefix)
                and props.get(cs.KEY_AST_FINGERPRINT) is not None
            ]
        if query == cq.CYPHER_DUPLICATE_SKIPPED_COUNT:
            skipped = sum(
                1
                for (label, _uid), props in self._nodes.items()
                if _in_scope(label, props, prefix)
                and props.get(cs.KEY_AST_FINGERPRINT) is None
            )
            return [{cs.KEY_SKIPPED: skipped}]
        return []


def _in_scope(label: str, props: PropertyDict, prefix: str) -> bool:
    qualified_name = props.get(cs.KEY_QUALIFIED_NAME)
    return label in _DUPLICATE_LABELS and str(qualified_name or "").startswith(prefix)


def _row(label: str, props: PropertyDict) -> ResultRow:
    row: ResultRow = {cs.KEY_LABEL: label}
    for key in _ROW_KEYS:
        row[key] = props.get(key)  # type: ignore[assignment]
    return row


def cgr_duplicates(
    target: Path, project: str, config: DuplicatesConfig
) -> DuplicatesReport:
    ingestor = _capture(target, project)
    return collect_duplicates_with_coverage(_GraphRows(ingestor.nodes), project, config)


def duplicate_pairs(groups: list[DuplicateGroup]) -> set[tuple[str, str]]:
    # Pair-level grading (the clone-detection standard): every unordered
    # member pair of every group, order-normalized so set algebra works.
    pairs: set[tuple[str, str]] = set()
    for group in groups:
        names = sorted({member[cs.KEY_QUALIFIED_NAME] for member in group["members"]})
        pairs.update(combinations(names, 2))
    return pairs


def _pair_repr(pair: tuple[str, str]) -> str:
    return ec.DUPLICATES_PAIR_SEP.join(pair)


def score_duplicates(
    cgr: set[tuple[str, str]], oracle: set[tuple[str, str]]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.NODE.value, ec.DUPLICATES_LABEL, cgr, oracle)
    if row is not None:
        rows.append(row)
        diff[ec.DUPLICATES_DIFF_PREFIX + ec.DUPLICATES_LABEL] = DiffBucket(
            missing=sorted(_pair_repr(pair) for pair in oracle - cgr),
            extra=sorted(_pair_repr(pair) for pair in cgr - oracle),
        )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to report duplicates for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    threshold: Annotated[
        float, typer.Option(help="Jaccard similarity threshold for edited copies.")
    ] = cs.DUPLICATES_DEFAULT_THRESHOLD,
    min_size: Annotated[
        int, typer.Option(help="Minimum skeleton node count for a candidate.")
    ] = cs.DUPLICATES_DEFAULT_MIN_NODES,
    exact_only: Annotated[
        bool, typer.Option(help="Skip similarity analysis; exact groups only.")
    ] = False,
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob(s) matched against a member's file path to exclude."),
    ] = None,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the duplicates report json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    # Corpus mode is informational: a real repo has no independent clone
    # oracle, so it reports cgr's duplicate groups for inspection. The graded
    # eval lives in the tests.
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.DUPLICATES_TARGET.format(target=target, project=project))

    config = default_duplicates_config(
        threshold=threshold,
        min_nodes=min_size,
        exact_only=exact_only,
        exclude_patterns=tuple(exclude or ()),
    )
    report = cgr_duplicates(target, project, config)
    members = sum(len(group["members"]) for group in report.groups)
    logger.success(
        ls.DUPLICATES_DONE.format(
            groups=len(report.groups),
            members=members,
            skipped=report.skipped_symbols,
            truncated=report.truncated,
        )
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        cs.KEY_DUPLICATE_GROUPS: report.groups,
        cs.KEY_SKIPPED_SYMBOLS: report.skipped_symbols,
        cs.KEY_TRUNCATED: report.truncated,
    }
    report_path = out_dir / ec.DUPLICATES_REPORT_FILENAME
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
