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
from fnmatch import fnmatch
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
_REFERENCES = cs.RelationshipType.REFERENCES.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_INHERITS = cs.RelationshipType.INHERITS.value
_DEFINES = cs.RelationshipType.DEFINES.value
_DEFINES_METHOD = cs.RelationshipType.DEFINES_METHOD.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

_NodeId = tuple[str, PropertyValue]
_RelTuple = tuple[str, PropertyValue, str, str, PropertyValue]


class DeadCodeConfig(NamedTuple):
    include_tests: bool
    include_classes: bool
    root_decorators: frozenset[str]
    entry_points: tuple[str, ...]
    test_patterns: tuple[str, ...]
    exclude_patterns: tuple[str, ...] = ()


def default_dead_code_config(
    include_tests: bool,
    include_classes: bool,
    exclude_patterns: tuple[str, ...] = (),
) -> DeadCodeConfig:
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(d.lower() for d in cs.DEFAULT_ROOT_DECORATORS),
        entry_points=(),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
        exclude_patterns=exclude_patterns,
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


def _walk(frontier: set[str], adjacency: dict[str, set[str]], live: set[str]) -> None:
    stack = list(frontier)
    while stack:
        current = stack.pop()
        for nxt in adjacency.get(current, ()):
            if nxt not in live:
                live.add(nxt)
                stack.append(nxt)


def dead_code_from_graph(
    nodes: dict[_NodeId, PropertyDict],
    rels: list[_RelTuple],
    project_prefix: str,
    config: DeadCodeConfig,
) -> set[str]:
    labels = {_FUNCTION, _METHOD}
    traversal = {_CALLS, _REFERENCES}
    module_rels = {_CALLS, _REFERENCES}
    if config.include_classes:
        labels.add(_CLASS)
        traversal |= {_INSTANTIATES, _INHERITS}
        module_rels.add(_INSTANTIATES)

    candidates: set[str] = set()
    props_by_qn: dict[str, PropertyDict] = {}
    method_qns: set[str] = set()
    module_path: dict[str, str] = {}
    for (label, uid), props in nodes.items():
        if label == _MODULE:
            module_path[str(uid)] = str(props.get(cs.KEY_PATH, ""))
        elif label in labels and str(uid).startswith(project_prefix):
            # (H) Mirror the query's candidate-side test filter: with tests
            # (H) excluded, a test-file symbol's only callers are excluded as
            # (H) roots, so reporting it is unconditional noise (test helpers
            # (H) and mocks are infrastructure, not dead production code).
            if not config.include_tests and any(
                pattern in str(props.get(cs.KEY_PATH) or "")
                for pattern in config.test_patterns
            ):
                continue
            candidates.add(str(uid))
            props_by_qn[str(uid)] = props
            if label == _METHOD:
                method_qns.add(str(uid))

    roots: set[str] = set()
    # (H) Mirror the query's protocol root clause and closure expansion inputs: a
    # (H) method of a typing.Protocol subclass is an interface stub whose callers
    # (H) resolve to the implementations, and DEFINES edges from functions/methods
    # (H) feed the live-owner registration round below.
    defines_pairs: list[tuple[str, str]] = []
    protocol_classes: set[str] = set()
    class_methods: list[tuple[str, str]] = []
    for from_label, from_val, rel_type, _to_label, to_val in rels:
        if rel_type == _DEFINES and from_label in (_FUNCTION, _METHOD):
            defines_pairs.append((str(from_val), str(to_val)))
        elif rel_type == _INHERITS and str(to_val) in cs.PROTOCOL_BASE_QNS:
            protocol_classes.add(str(from_val))
        elif rel_type == _DEFINES_METHOD:
            class_methods.append((str(from_val), str(to_val)))
        if from_label != _MODULE or rel_type not in module_rels:
            continue
        target_qn = str(to_val)
        if target_qn not in candidates:
            continue
        path = module_path.get(str(from_val), "")
        is_test = any(pattern in path for pattern in config.test_patterns)
        if config.include_tests or not is_test:
            roots.add(target_qn)
    protocol_stubs = {m for c, m in class_methods if c in protocol_classes}

    for qn in candidates:
        if qn in roots:
            continue
        props = props_by_qn[qn]
        if _has_root_decorator(props, config.root_decorators):
            roots.add(qn)
        elif props.get(cs.KEY_IS_EXPORTED) is True:
            roots.add(qn)
        elif qn in protocol_stubs:
            roots.add(qn)
        elif (
            qn in method_qns
            and _is_dunder(qn.rsplit(cs.SEPARATOR_DOT, 1)[-1])
            and str(props.get(cs.KEY_PATH, "")).endswith(cs.EXT_PY)
        ):
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
    _walk(roots, adjacency, live)

    # (H) Second expansion, mirroring the query: a decorated function DEFINED by a
    # (H) LIVE owner is framework-registered when the owner runs, so it and its
    # (H) callees are live; the closure of a DEAD owner never registers and stays
    # (H) in the reported cluster. ponytail: one round, same depth-2 ceiling as the
    # (H) Cypher template (see _DEAD_CODE_QUERY_TEMPLATE).
    closure_roots = {
        c
        for o, c in defines_pairs
        if o in live
        and c not in live
        and c in props_by_qn
        and props_by_qn[c].get(cs.KEY_DECORATORS)
    }
    live |= closure_roots
    _walk(closure_roots, adjacency, live)

    dead = candidates - live
    # (H) Suppress generated files (openapi-ts client/core, routeTree.gen.ts) from
    # (H) the REPORT only, after reachability: they stay full participants as roots
    # (H) and callers, so a real function invoked only from generated glue is not
    # (H) newly flagged -- excluding earlier would drop those live edges.
    if config.exclude_patterns:
        dead = {
            qn
            for qn in dead
            if not any(
                fnmatch(str(props_by_qn[qn].get(cs.KEY_PATH) or ""), pattern)
                for pattern in config.exclude_patterns
            )
        }
    return dead


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
    exclude: Annotated[
        list[str] | None,
        typer.Option(help="Glob(s) matched against a symbol's file path to exclude."),
    ] = None,
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

    config = default_dead_code_config(
        include_tests, include_classes, tuple(exclude or ())
    )
    dead = cgr_dead_code(target, project, config)
    logger.success(ls.DEAD_CODE_DONE.format(count=len(dead)))

    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / ec.DEAD_CODE_DIFF_FILENAME
    report.write_text(json.dumps(sorted(dead), indent=2), encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
