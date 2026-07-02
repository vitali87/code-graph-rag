# (H) Retrieval benchmark: graph-augmented call localization vs grep. For every
# (H) first-party symbol S, the task is to find the files that call S. The graph
# (H) condition uses cgr's resolved CALLS/INSTANTIATES edges; the grep conditions
# (H) use ripgrep (bare-name and call-tuned patterns). All three are scored
# (H) against the same Python ast oracle over the same file and symbol universe,
# (H) as a set of (caller_file, callee_simple_name) name-edges restricted to
# (H) first-party, non-dunder callees -- the set cgr can emit. This isolates
# (H) retrieval quality (does the graph beat grep) from any LLM in the loop, the
# (H) decoupled measurement the GitLab GKG eval flagged as out of scope.
import ast
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .ast_oracle import _iter_py_files
from .cgr_graph import _capture
from .module_calls import _callee_name, _first_party_names, _is_dunder
from .score import _name_edge_bucket, _prf
from .structure_report import render, write_outputs
from .types_defs import (
    DiffBucket,
    LocationStats,
    NameEdge,
    NodeKey,
    ScoreResult,
    ScoreRow,
)

console_target = Path(ec.RETRIEVAL_DEFAULT_TARGET)

_CALLS = cs.RelationshipType.CALLS.value
_INSTANTIATES = cs.RelationshipType.INSTANTIATES.value
_MODULE = cs.NodeLabel.MODULE.value
_METHOD = cs.NodeLabel.METHOD.value
_IDENTIFIER = re.compile(ec.IDENTIFIER_PATTERN)
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)


def parse_py_trees(target: Path) -> tuple[list[tuple[str, ast.Module]], set[str]]:
    trees: list[tuple[str, ast.Module]] = []
    files: set[str] = set()
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        files.add(rel)
        try:
            trees.append((rel, ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
    return trees, files


def first_party_symbols(trees: list[tuple[str, ast.Module]]) -> set[str]:
    names = _first_party_names([tree for _rel, tree in trees])
    return {name for name in names if not _is_dunder(name)}


def _decorator_name(dec: ast.expr) -> str | None:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return None


def first_party_property_names(trees: list[tuple[str, ast.Module]]) -> set[str]:
    names: set[str] = set()
    for _rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                _decorator_name(dec) in ec.PROPERTY_DECORATORS
                for dec in node.decorator_list
            ):
                names.add(node.name)
    return {name for name in names if not _is_dunder(name)}


def _edge(file: str, name: str) -> NameEdge:
    return NameEdge(_CALLS, NodeKey(_MODULE, file, ec.MODULE_START_LINE), name)


def oracle_call_edges(
    trees: list[tuple[str, ast.Module]],
    first_party: set[str],
    property_names: set[str] | None = None,
) -> set[NameEdge]:
    properties = property_names or set()
    edges: set[NameEdge] = set()
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (name := _callee_name(node.func)):
                if name in first_party:
                    edges.add(_edge(rel, name))
            # (H) A bare attribute read of a first-party property getter invokes it;
            # (H) cgr emits a CALLS edge, so credit it here too (a property is never
            # (H) syntactically called with parens, so this cannot double-count).
            elif isinstance(node, ast.Attribute) and node.attr in properties:
                edges.add(_edge(rel, node.attr))
    return edges


def cgr_call_edges(
    target: Path, project_name: str, first_party: set[str]
) -> set[NameEdge]:
    ingestor = _capture(target, project_name)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(ec.PY_SUFFIX)
    }

    edges: set[NameEdge] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        if rel_type not in (_CALLS, _INSTANTIATES):
            continue
        path = caller_path.get((from_label, str(from_val)))
        if path is None:
            continue
        segments = str(to_val).split(ec.SEP)
        name = segments[-1]
        # (H) A constructor call resolves via CALLS to `X.__init__` (a METHOD); the
        # (H) oracle sees the class name `X`, so credit the class, matching L2.
        if name == ec.INIT_STEM and to_label == _METHOD and len(segments) >= 2:
            name = segments[-2]
        if _is_dunder(name) or name not in first_party:
            continue
        edges.add(_edge(path, name))
    return edges


def _grep_patterns(first_party: set[str], mode: ec.GrepMode) -> str:
    template = (
        ec.GREP_CALL_TEMPLATE if mode == ec.GrepMode.CALL else ec.GREP_NAME_TEMPLATE
    )
    return ec.PATTERN_SEP.join(
        template.format(name=re.escape(name)) for name in sorted(first_party)
    )


def grep_call_edges(
    target: Path, first_party: set[str], files: set[str], mode: ec.GrepMode
) -> set[NameEdge]:
    if not first_party:
        return set()
    completed = subprocess.run(
        [
            ec.RG_BIN,
            ec.RG_ONLY_MATCHING,
            ec.RG_WITH_FILENAME,
            ec.RG_NO_LINE_NUMBER,
            ec.RG_NO_HEADING,
            ec.RG_NULL,
            ec.RG_GLOB_FLAG,
            ec.RG_PY_GLOB,
            ec.RG_PATTERN_FILE_FLAG,
            ec.RG_STDIN,
            ec.RG_SEARCH_PATH,
        ],
        cwd=target,
        input=_grep_patterns(first_party, mode),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in ec.RG_OK_RETURNCODES:
        logger.warning(completed.stderr.strip())
        return set()

    edges: set[NameEdge] = set()
    for line in completed.stdout.splitlines():
        path_text, sep, matched = line.partition(ec.RG_NULL_SEP)
        if not sep:
            continue
        # (H) Path(...).as_posix() strips the leading ./ and folds Windows
        # (H) backslashes to the forward-slash form parse_py_trees keys files on.
        rel = Path(path_text).as_posix()
        if rel not in files:
            continue
        token = _IDENTIFIER.match(matched)
        if token is not None and token.group(0) in first_party:
            edges.add(_edge(rel, token.group(0)))
    return edges


def score_retrieval(
    conditions: list[tuple[str, set[NameEdge]]], oracle: set[NameEdge]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    for label, retrieved in conditions:
        row = _prf(ec.Category.RETRIEVAL.value, label, retrieved, oracle)
        if row is not None:
            rows.append(row)
        diff[ec.RETRIEVAL_DIFF_PREFIX + label] = _name_edge_bucket(retrieved, oracle)
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate call retrieval for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path,
        typer.Option(
            help="Directory for retrieval_scores.csv and retrieval_diff.json."
        ),
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    if shutil.which(ec.RG_BIN) is None:
        logger.error(ls.RETRIEVAL_RG_MISSING.format(binary=ec.RG_BIN))
        raise typer.Exit(code=1)

    target = target.resolve()
    project = project_name or target.name

    trees, files = parse_py_trees(target)
    first_party = first_party_symbols(trees)
    logger.info(ls.RETRIEVAL_SYMBOLS.format(count=len(first_party)))

    logger.info(ls.RETRIEVAL_EXTRACTING_ORACLE.format(target=target))
    oracle = oracle_call_edges(trees, first_party, first_party_property_names(trees))
    logger.success(ls.RETRIEVAL_ORACLE_DONE.format(count=len(oracle)))

    logger.info(ls.RETRIEVAL_EXTRACTING_CGR.format(target=target, project=project))
    graph = cgr_call_edges(target, project, first_party)
    logger.success(ls.RETRIEVAL_CGR_DONE.format(count=len(graph)))

    conditions: list[tuple[str, set[NameEdge]]] = [
        (ec.RetrievalCondition.GRAPH.value, graph)
    ]
    for mode, label in (
        (ec.GrepMode.NAME, ec.RetrievalCondition.GREP_NAME.value),
        (ec.GrepMode.CALL, ec.RetrievalCondition.GREP_CALL.value),
    ):
        logger.info(ls.RETRIEVAL_EXTRACTING_GREP.format(mode=mode.value, target=target))
        grep_edges = grep_call_edges(target, first_party, files, mode)
        logger.success(
            ls.RETRIEVAL_GREP_DONE.format(mode=mode.value, count=len(grep_edges))
        )
        conditions.append((label, grep_edges))

    result = score_retrieval(conditions, oracle)
    write_outputs(
        result, out_dir, ec.RETRIEVAL_SCORES_FILENAME, ec.RETRIEVAL_DIFF_FILENAME
    )
    render(result, ec.RETRIEVAL_TITLE)


if __name__ == "__main__":
    typer.run(main)
