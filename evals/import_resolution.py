# Import-resolution eval. For every module, classify each import by its
# top-level package as internal (first-party, resolving into the repo) or
# external (stdlib / third-party), and check cgr against an ast + filesystem
# oracle. The comparison unit is (importing_file, top_level_package,
# is_external): both sides reduce an import to its top-level name the same
# way, so the oracle is independent of cgr. This isolates internal/external
# misclassification (issue #498), which the structural L1 IMPORTS grading
# (internal targets only, by resolved file) does not see.
import ast
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .ast_oracle import _from_base_parts, _iter_py_files
from .cgr_graph import _capture
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.IMPORTS_DEFAULT_TARGET)

_MODULE = cs.NodeLabel.MODULE.value
_EXTERNAL_MODULE = cs.NodeLabel.EXTERNAL_MODULE.value
_IMPORTS = cs.RelationshipType.IMPORTS.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

ImportDep = tuple[str, str, bool]


def _import_deps_for_module(tree: ast.Module, rel: str, project: str) -> set[ImportDep]:
    pkg_parts = [project, *Path(rel).parent.parts]
    deps: set[ImportDep] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(cs.SEPARATOR_DOT, 1)[0]
                if top not in ec.IMPORTS_IGNORED_TOPS:
                    deps.add((rel, top, top != project))
        elif isinstance(node, ast.ImportFrom):
            base_parts = _from_base_parts(node, pkg_parts)
            if not base_parts:
                # A relative import escaping the package root resolves to
                # nothing the repo defines; skip rather than guess.
                continue
            top = base_parts[0]
            if top not in ec.IMPORTS_IGNORED_TOPS:
                deps.add((rel, top, top != project))
    return deps


def oracle_import_deps(target: Path, project: str) -> set[ImportDep]:
    deps: set[ImportDep] = set()
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
            continue
        deps |= _import_deps_for_module(tree, rel, project)
    return deps


def cgr_import_deps(target: Path, project: str) -> set[ImportDep]:
    ingestor = _capture(target, project)
    is_external: dict[str, bool] = {}
    internal_file: dict[str, str] = {}
    for (label, uid), props in ingestor.nodes.items():
        external = label == _EXTERNAL_MODULE
        if label != _MODULE and not external:
            continue
        is_external[str(uid)] = external
        path = props.get(cs.KEY_PATH)
        if not external and path and str(path).endswith(ec.PY_SUFFIX):
            internal_file[str(uid)] = str(path)

    deps: set[ImportDep] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _IMPORTS or from_label != _MODULE:
            continue
        src = internal_file.get(str(from_val))
        if src is None:
            continue
        top = str(to_val).split(cs.SEPARATOR_DOT, 1)[0]
        deps.add((src, top, is_external.get(str(to_val), False)))
    return deps


def _dep_repr(dep: ImportDep) -> str:
    return ec.IMPORT_DEP_REPR.format(file=dep[0], top=dep[1], external=dep[2])


def _row(label: str, cgr: set[ImportDep], oracle: set[ImportDep]) -> ScoreRow | None:
    return _prf(ec.Category.EDGE.value, label, cgr, oracle)


def score_import_deps(cgr: set[ImportDep], oracle: set[ImportDep]) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    subsets: list[tuple[str, set[ImportDep], set[ImportDep]]] = [
        (ec.IMPORTS_ALL_LABEL, cgr, oracle),
        (
            ec.IMPORTS_INTERNAL_LABEL,
            {d for d in cgr if not d[2]},
            {d for d in oracle if not d[2]},
        ),
        (
            ec.IMPORTS_EXTERNAL_LABEL,
            {d for d in cgr if d[2]},
            {d for d in oracle if d[2]},
        ),
    ]
    for label, cgr_set, oracle_set in subsets:
        row = _row(label, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.IMPORTS_DIFF_PREFIX + label] = DiffBucket(
                missing=[_dep_repr(d) for d in sorted(oracle_set - cgr_set)],
                extra=[_dep_repr(d) for d in sorted(cgr_set - oracle_set)],
            )
    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate import resolution for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for imports_scores.csv and the diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.IMPORTS_TARGET.format(target=target, project=project))

    oracle = oracle_import_deps(target, project)
    logger.success(ls.IMPORTS_ORACLE_DONE.format(count=len(oracle)))
    cgr = cgr_import_deps(target, project)
    logger.success(ls.IMPORTS_CGR_DONE.format(count=len(cgr)))

    result = score_import_deps(cgr, oracle)
    write_outputs(result, out_dir, ec.IMPORTS_SCORES_FILENAME, ec.IMPORTS_DIFF_FILENAME)
    render(result, ec.IMPORTS_TITLE)


if __name__ == "__main__":
    typer.run(main)
