# (H) Dead-code eval. cgr's `dead-code` command reports functions/methods
# (H) unreachable from any entry point via a Cypher reachability query
# (H) (build_dead_code_query). The deterministic in-memory harness cannot run that
# (H) query against a database, so this faithfully re-implements its reachability
# (H) over the captured graph and grades the result on controlled fixtures whose
# (H) dead set is known by construction. The reachability is unit-tested on
# (H) hand-built graphs, so a fixture mismatch indicts cgr's CALLS graph (e.g. a
# (H) missing edge flagging a live function as dead), not the scorer.
import json
from collections import defaultdict
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.types_defs import PropertyDict, PropertyValue

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .score import _prf
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.DEAD_CODE_DEFAULT_TARGET)

_MODULE = cs.NodeLabel.MODULE.value
_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_CLASS = cs.NodeLabel.CLASS.value
_CALLS = cs.RelationshipType.CALLS.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_INHERITS = cs.RelationshipType.INHERITS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

_NodeId = tuple[str, PropertyValue]
_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


class DeadCodeConfig(NamedTuple):
    include_tests: bool
    include_classes: bool
    root_decorators: frozenset[str]
    entry_points: tuple[str, ...]
    test_patterns: tuple[str, ...]


def default_dead_code_config(
    include_tests: bool, include_classes: bool
) -> DeadCodeConfig:
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(d.lower() for d in cs.DEFAULT_ROOT_DECORATORS),
        entry_points=(),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
    )


def _norm_decorator(decorator: str) -> str:
    # (H) Mirror the query: drop '@', take the text before '(', then the last
    # (H) dotted segment, lowercased -> `@app.route(...)` becomes `route`.
    head = decorator.replace(ec.DECORATOR_AT, "").split(ec.DECORATOR_CALL_OPEN)[0]
    return head.split(cs.SEPARATOR_DOT)[-1].lower()


def _is_dunder(name: str) -> bool:
    # (H) A __dunder__ method is invoked by the Python runtime (async with, iteration,
    # (H) operators, ...), never by an explicit call the call graph can see, so it is a
    # (H) reachability root rather than dead code.
    return (
        len(name) > len(cs.PY_NAME_DUNDER) * 2
        and name.startswith(cs.PY_NAME_DUNDER)
        and name.endswith(cs.PY_NAME_DUNDER)
    )


def _has_root_decorator(props: PropertyDict, root_decorators: frozenset[str]) -> bool:
    decorators = props.get(cs.KEY_DECORATORS)
    if not isinstance(decorators, list):
        return False
    return any(_norm_decorator(str(d)) in root_decorators for d in decorators)


def dead_code_from_graph(
    nodes: dict[_NodeId, PropertyDict],
    rels: list[_RelTuple],
    project_prefix: str,
    config: DeadCodeConfig,
) -> set[str]:
    labels = {_FUNCTION, _METHOD}
    traversal = {_CALLS}
    module_rels = {_CALLS}
    if config.include_classes:
        labels.add(_CLASS)
        traversal |= {_INSTANTIATES, _INHERITS}
        module_rels.add(_INSTANTIATES)

    candidates: set[str] = set()
    props_by_qn: dict[str, PropertyDict] = {}
    module_path: dict[str, str] = {}
    for (label, uid), props in nodes.items():
        if label == _MODULE:
            module_path[str(uid)] = str(props.get(cs.KEY_PATH, ""))
        elif label in labels and str(uid).startswith(project_prefix):
            candidates.add(str(uid))
            props_by_qn[str(uid)] = props

    roots: set[str] = set()
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if from_label != _MODULE or rel_type not in module_rels:
            continue
        target_qn = str(to_val)
        if target_qn not in candidates:
            continue
        path = module_path.get(str(from_val), "")
        is_test = any(pattern in path for pattern in config.test_patterns)
        if config.include_tests or not is_test:
            roots.add(target_qn)

    for qn in candidates:
        if qn in roots:
            continue
        props = props_by_qn[qn]
        if _has_root_decorator(props, config.root_decorators):
            roots.add(qn)
        elif props.get(cs.KEY_IS_EXPORTED) is True:
            roots.add(qn)
        elif _is_dunder(qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]) and str(
            props.get(cs.KEY_PATH, "")
        ).endswith(cs.EXT_PY):
            roots.add(qn)
        elif any(qn.endswith(entry) for entry in config.entry_points):
            roots.add(qn)
        elif config.include_tests and any(
            pattern in str(props.get(cs.KEY_PATH, ""))
            for pattern in config.test_patterns
        ):
            roots.add(qn)

    adjacency: dict[str, set[str]] = defaultdict(set)
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type in traversal:
            adjacency[str(from_val)].add(str(to_val))

    live = set(roots)
    stack = list(roots)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in live:
                live.add(nxt)
                stack.append(nxt)

    return candidates - live


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
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the dead-code report json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    # (H) Corpus mode is informational: a real repo has no independent dead-code
    # (H) oracle (true reachability needs the same call graph), so this reports
    # (H) cgr's reachable-from-roots dead set. The graded eval lives in the tests.
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.DEAD_CODE_TARGET.format(target=target, project=project))

    config = default_dead_code_config(include_tests, include_classes)
    dead = cgr_dead_code(target, project, config)
    logger.success(ls.DEAD_CODE_DONE.format(count=len(dead)))

    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / ec.DEAD_CODE_DIFF_FILENAME
    report.write_text(json.dumps(sorted(dead), indent=2), encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
