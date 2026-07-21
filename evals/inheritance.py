# Inheritance eval. Grades cgr's resolved INHERITS (subclass_qn -> base_qn)
# and OVERRIDES (subclass_qn, base_qn, method) against an ast oracle. The L1
# structure eval only checks INHERITS by the base's simple name; this checks
# that cgr resolves the base to the correct first-party class and that method
# overrides are attributed to the right base. The oracle resolves a base only
# via same-module definitions and `from <first-party> import <Base>`, and
# skips attribute/ambiguous/external bases (counted, never silently dropped),
# so it stays independent of cgr's resolver and never invents an edge.
import ast
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .ast_oracle import _from_base_parts, _iter_py_files, _module_dotted
from .cgr_graph import _capture
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.INHERITANCE_DEFAULT_TARGET)

_CLASS = cs.NodeLabel.CLASS.value
_METHOD = cs.NodeLabel.METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value
_OVERRIDES = cs.RelationshipType.OVERRIDES.value
_EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)

InheritEdge = tuple[str, str]
OverrideEdge = tuple[str, str, str]


class _ClassInfo(NamedTuple):
    qn: str
    module: str
    methods: frozenset[str]
    bases: tuple[ast.expr, ...]


class OracleResult(NamedTuple):
    inherits: set[InheritEdge]
    overrides: set[OverrideEdge]
    # Universe of top-level classes the oracle understands; cgr edges whose
    # subclass is outside it (e.g. a class nested in a function) are not graded.
    top_classes: frozenset[str]
    # Subclasses eligible for OVERRIDES grading: top-level and single-base, so
    # override attribution is unambiguous. Multi-base (mixin/MRO) classes are
    # excluded on both sides rather than guessed at.
    override_scope: frozenset[str]


class CgrResult(NamedTuple):
    inherits: set[InheritEdge]
    overrides: set[OverrideEdge]


def _method_names(node: ast.ClassDef) -> frozenset[str]:
    return frozenset(
        child.name
        for child in node.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    )


def _from_import_map(tree: ast.Module, rel: str, project: str) -> dict[str, str]:
    # name -> source module dotted, for `from <module> import <name>` whose
    # base resolves under the project package (first-party).
    pkg_parts = [project, *Path(rel).parent.parts]
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        base_parts = _from_base_parts(node, pkg_parts)
        if not base_parts or base_parts[0] != project:
            continue
        source = cs.SEPARATOR_DOT.join(base_parts)
        for alias in node.names:
            if alias.name != ec.STAR_IMPORT:
                mapping[alias.asname or alias.name] = source
    return mapping


def _collect(
    target: Path, project: str
) -> tuple[dict[str, _ClassInfo], dict[str, str]]:
    classes: dict[str, _ClassInfo] = {}
    # import_maps is keyed "<module>\x00<name>" and filled after all modules
    # are collected so base resolution can look a name up in its own scope.
    import_maps: dict[str, str] = {}
    per_module_imports: dict[str, dict[str, str]] = {}
    for path in _iter_py_files(target):
        rel = path.relative_to(target).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding=cs.ENCODING_UTF8))
        except (SyntaxError, UnicodeDecodeError, ValueError) as error:
            logger.warning(ls.ORACLE_PARSE_FAILED.format(path=rel, error=error))
            continue
        module = _module_dotted(rel, project)
        per_module_imports[module] = _from_import_map(tree, rel, project)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                qn = f"{module}{cs.SEPARATOR_DOT}{node.name}"
                classes[qn] = _ClassInfo(
                    qn=qn,
                    module=module,
                    methods=_method_names(node),
                    bases=tuple(node.bases),
                )
    # Flatten per-module import maps into a single "<module>\x00<name>" key so
    # base resolution can look up an imported name in its own module's scope.
    for module, mapping in per_module_imports.items():
        for name, source in mapping.items():
            import_maps[f"{module}{ec.SEP_NUL}{name}"] = source
    return classes, import_maps


def _resolve_base(
    base: ast.expr,
    info: _ClassInfo,
    classes: dict[str, _ClassInfo],
    import_maps: dict[str, str],
) -> str | None:
    if not isinstance(base, ast.Name):
        # Attribute (pkg.Base) and other base forms are not resolved here.
        return None
    name = base.id
    same_module = f"{info.module}{cs.SEPARATOR_DOT}{name}"
    if same_module in classes:
        return same_module
    source = import_maps.get(f"{info.module}{ec.SEP_NUL}{name}")
    if source is not None:
        imported = f"{source}{cs.SEPARATOR_DOT}{name}"
        if imported in classes:
            return imported
    return None


def oracle_inheritance(target: Path, project: str) -> OracleResult:
    classes, import_maps = _collect(target, project)
    inherits: set[InheritEdge] = set()
    overrides: set[OverrideEdge] = set()
    override_scope: set[str] = set()
    skipped = 0
    for info in classes.values():
        resolved_bases: list[str] = []
        for base in info.bases:
            base_qn = _resolve_base(base, info, classes, import_maps)
            if base_qn is None:
                skipped += 1
                continue
            resolved_bases.append(base_qn)
            inherits.add((info.qn, base_qn))
        # Grade overrides only for unambiguous single first-party-base classes;
        # with multiple bases the MRO decides which base a method overrides, a
        # call this ast oracle does not model.
        if len(resolved_bases) == 1:
            override_scope.add(info.qn)
            base_qn = resolved_bases[0]
            for method in info.methods & classes[base_qn].methods:
                overrides.add((info.qn, base_qn, method))
    logger.info(ls.INHERITANCE_SKIPPED_BASES.format(count=skipped))
    return OracleResult(
        inherits=inherits,
        overrides=overrides,
        top_classes=frozenset(classes),
        override_scope=frozenset(override_scope),
    )


def cgr_inheritance(target: Path, project: str) -> CgrResult:
    ingestor = _capture(target, project)
    first_party: set[str] = {
        str(uid)
        for (label, uid), props in ingestor.nodes.items()
        if label == _CLASS
        and props.get(cs.KEY_PATH)
        and str(props[cs.KEY_PATH]).endswith(ec.PY_SUFFIX)
    }
    inherits: set[InheritEdge] = set()
    overrides: set[OverrideEdge] = set()
    for from_label, from_val, rel_type, to_label, to_val in ingestor.rels:
        if rel_type == _INHERITS and from_label == _CLASS and to_label == _CLASS:
            if str(from_val) in first_party and str(to_val) in first_party:
                inherits.add((str(from_val), str(to_val)))
        elif rel_type == _OVERRIDES and from_label == _METHOD and to_label == _METHOD:
            sub, _sep, method = str(from_val).rpartition(cs.SEPARATOR_DOT)
            base, _sep2, _m = str(to_val).rpartition(cs.SEPARATOR_DOT)
            if sub in first_party and base in first_party:
                overrides.add((sub, base, method))
    return CgrResult(inherits=inherits, overrides=overrides)


def _inherit_repr(edge: InheritEdge) -> str:
    return ec.INHERITS_EDGE_REPR.format(sub=edge[0], base=edge[1])


def _override_repr(edge: OverrideEdge) -> str:
    return ec.OVERRIDES_EDGE_REPR.format(sub=edge[0], base=edge[1], method=edge[2])


def score_inheritance(cgr: CgrResult, oracle: OracleResult) -> ScoreResult:
    # Restrict cgr to the oracle's gradeable universe: subclasses the oracle
    # understands (top-level) for INHERITS, and single-base subclasses for
    # OVERRIDES. This drops nested-class and multi-base-MRO edges the oracle
    # cannot adjudicate, rather than scoring cgr against an incomplete oracle.
    cgr_inh = {e for e in cgr.inherits if e[0] in oracle.top_classes}
    cgr_ovr = {e for e in cgr.overrides if e[0] in oracle.override_scope}
    oracle_inh = oracle.inherits
    oracle_ovr = oracle.overrides
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}

    inh_row = _prf(ec.Category.EDGE.value, ec.INHERITS_LABEL, cgr_inh, oracle_inh)
    if inh_row is not None:
        rows.append(inh_row)
        diff[ec.INHERITANCE_DIFF_PREFIX + ec.INHERITS_LABEL] = DiffBucket(
            missing=[_inherit_repr(e) for e in sorted(oracle_inh - cgr_inh)],
            extra=[_inherit_repr(e) for e in sorted(cgr_inh - oracle_inh)],
        )

    ovr_row = _prf(ec.Category.EDGE.value, ec.OVERRIDES_LABEL, cgr_ovr, oracle_ovr)
    if ovr_row is not None:
        rows.append(ovr_row)
        diff[ec.INHERITANCE_DIFF_PREFIX + ec.OVERRIDES_LABEL] = DiffBucket(
            missing=[_override_repr(e) for e in sorted(oracle_ovr - cgr_ovr)],
            extra=[_override_repr(e) for e in sorted(cgr_ovr - oracle_ovr)],
        )

    return ScoreResult(rows=rows, location=_EMPTY_LOCATION, diff=diff)


def main(
    target: Annotated[
        Path, typer.Option(help="cgr source to evaluate inheritance for.")
    ] = console_target,
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for inheritance_scores.csv and diff json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.INHERITANCE_TARGET.format(target=target, project=project))

    oracle = oracle_inheritance(target, project)
    logger.success(
        ls.INHERITANCE_ORACLE_DONE.format(
            inherits=len(oracle[0]), overrides=len(oracle[1])
        )
    )
    cgr = cgr_inheritance(target, project)
    logger.success(
        ls.INHERITANCE_CGR_DONE.format(inherits=len(cgr[0]), overrides=len(cgr[1]))
    )

    result = score_inheritance(cgr, oracle)
    write_outputs(
        result, out_dir, ec.INHERITANCE_SCORES_FILENAME, ec.INHERITANCE_DIFF_FILENAME
    )
    render(result, ec.INHERITANCE_TITLE)


if __name__ == "__main__":
    typer.run(main)
