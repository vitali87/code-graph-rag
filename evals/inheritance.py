# Inheritance eval. Grades cgr's resolved INHERITS (subclass_qn -> base_qn)
# and OVERRIDES (subclass_qn, base_qn, method) against an ast oracle. Unlike
# the L1 check (INHERITS by simple name), this verifies cgr resolves the base
# to the correct first-party class and attributes overrides to the right base.
# The oracle resolves bases only via same-module definitions and `from
# <first-party> import <Base>`, skipping attribute/ambiguous/external bases
# (counted, never dropped), so it stays independent of cgr.
import ast
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NamedTuple

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .ast_oracle import _from_base_parts, _iter_py_files, _module_dotted
from .cgr_graph import _capture
from .oracles.cpp_oracle import cpp_available, run_cpp_oracle

if TYPE_CHECKING:
    from clang.cindex import Cursor
from .oracles.csharp_oracle import (
    csharp_oracle_available,
    run_csharp_oracle,
)
from .oracles.java_oracle import java_available, run_java_oracle
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import DiffBucket, LocationStats, ScoreResult, ScoreRow

console_target = Path(ec.INHERITANCE_DEFAULT_TARGET)

_CLASS = cs.NodeLabel.CLASS.value
_METHOD = cs.NodeLabel.METHOD.value
_INHERITS = cs.RelationshipType.INHERITS.value
_IMPLEMENTS = cs.RelationshipType.IMPLEMENTS.value
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
    # attribution is unambiguous. Multi-base (mixin/MRO) classes are excluded on
    # both sides rather than guessed at.
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
        # Grade overrides only for single first-party-base classes; with
        # multiple bases the MRO decides which base a method overrides, which
        # this ast oracle does not model.
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


def _location_key(file: str, line: int) -> str:
    # Subclass identity by LOCATION, not by name: the javac oracle names a
    # supertype by simple name but pins the subclass to a file and line, and
    # matching the subclass exactly keeps the looseness confined to one side of
    # the comparison.
    return f"{file}{ec.LOCATION_KEY_SEPARATOR}{line}"


def java_oracle_inheritance(target: Path) -> OracleResult:
    # The javac oracle already emits extends/implements as `name_edges`; no
    # oracle-side work is needed (issue #1190). It emits no OVERRIDES, so that
    # category stays empty and _prf omits the row rather than scoring a
    # category the oracle cannot adjudicate.
    graph = run_java_oracle(target)
    inherits: set[InheritEdge] = set()
    subclasses: set[str] = set()
    for edge in graph.name_edges:
        if edge.rel_type not in (_INHERITS, _IMPLEMENTS):
            continue
        key = _location_key(edge.source.file, edge.source.start_line)
        subclasses.add(key)
        inherits.add((key, edge.target_name))
    return OracleResult(
        inherits=inherits,
        overrides=set(),
        top_classes=frozenset(subclasses),
        override_scope=frozenset(),
    )


def java_cgr_inheritance(target: Path, project: str) -> CgrResult:
    ingestor = _capture(target, project)
    props_by_node = {
        (label, str(uid)): props for (label, uid), props in ingestor.nodes.items()
    }
    inherits: set[InheritEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in (_INHERITS, _IMPLEMENTS):
            continue
        props = props_by_node.get((from_label, str(from_val)))
        path = str(props.get(cs.KEY_PATH, "")) if props else ""
        if not path.endswith(ec.JAVA_SUFFIX):
            continue
        line = props.get(cs.KEY_START_LINE) if props else None
        # Node properties are a heterogeneous union; a non-integer start_line
        # is unusable as a location key, so skip rather than coerce it.
        if not isinstance(line, int):
            continue
        # The oracle names the supertype simply, so the qn is reduced to its
        # last segment to compare like with like.
        base = str(to_val).rsplit(cs.SEPARATOR_DOT, 1)[-1]
        inherits.add((_location_key(path, line), base))
    return CgrResult(inherits=inherits, overrides=set())


def _resolves_inside(path: str, target: Path) -> bool:
    """Whether `path` lies within `target`, mirroring the oracle's `_rel`."""
    try:
        Path(path).resolve().relative_to(target.resolve())
    except (ValueError, OSError):
        return False
    return True


def _reaches_inside(cursor: "Cursor", target: Path) -> bool:
    """Whether any cursor in this unit's tree resolves inside `target`.

    Walks the whole tree rather than the top-level children, because the
    oracle's own `_walk` does: an in-target declaration can sit inside a
    namespace, class or extern block whose enclosing cursor is out-of-target,
    and checking only the immediate children misses it (Greptile, PR #1513).
    Iterative to avoid recursion limits on deep headers.
    """
    stack = list(cursor.get_children())
    while stack:
        cur = stack.pop()
        location = cur.location.file
        if location is not None and _resolves_inside(location.name, target):
            return True
        stack.extend(cur.get_children())
    return False


def _cpp_compile_db_units(target: Path) -> int | None:
    """Translation units the target's compilation database yields, or None.

    `None` means the database could not be OPENED (absent or unreadable); a
    count of zero means it opened and yielded nothing gradeable (empty, or
    naming files that no longer exist). Both stop the run, but the remedies
    differ -- "create one" versus "the one you have is stale" -- and reporting
    the wrong cause sends the reader to the wrong fix (CodeRabbit, PR #1513).

    A negative count means the database is PARTIALLY readable: at least one
    in-target entry could not be read while others could. That must stop the
    run too. Grading what remains looks clean -- `score_inheritance` restricts
    cgr to the oracle's `top_classes`, so the unread file's edges are filtered
    out on both sides and a half-covered project scores 1.0 (Greptile,
    PR #1513). The magnitude is the number of unreadable in-target entries.
    """
    try:
        import clang.cindex as ci

        db = ci.CompilationDatabase.fromDirectory(str(target.resolve()))
    except Exception:
        # fromDirectory raises CompilationDatabaseError when the file is absent
        # or unreadable; libclang may be missing entirely on some platforms.
        return None
    commands = db.getAllCompileCommands()
    if commands is None:  # an empty database returns None, not an empty list
        return 0  # opened, but declares nothing
    # Count units that actually PARSE, not entries that merely exist: a stale
    # database naming files that have since been deleted has entries and
    # yields no AST, which is the fail-open case this guard is for.
    #
    # "Parsed" means the source exists and libclang reported no FATAL
    # diagnostic -- deliberately NOT "the AST has children". A valid but empty
    # or comment-only translation unit parses perfectly and has no children,
    # and rejecting it would refuse a legitimate zero-inheritance grade
    # (Greptile, PR #1513). Emptiness is a fact about the code; the guard is
    # about whether the oracle could read it.
    parsed = 0
    unreadable = 0
    index = ci.Index.create()
    for command in commands:
        # Per the Clang JSON Compilation Database spec, `file` and any relative
        # paths in the command are resolved against that entry's `directory`,
        # not the reader's cwd. Checking them as given skips spec-valid entries
        # and parses others from the wrong working directory, so a usable
        # database is reported ungradable (CodeRabbit, PR #1513).
        directory = Path(command.directory)
        source = directory / command.filename
        # Whether the entry's own source sits inside the target decides only
        # whether its ABSENCE is a hole. It does NOT decide whether the entry
        # is worth parsing: the oracle keeps cursors by the CURSOR's file, not
        # the translation unit's, so an out-of-target driver that includes an
        # in-target header yields gradeable edges. Skipping such an entry
        # refused targets that could be graded (Greptile, PR #1513).
        try:
            source.resolve().relative_to(target.resolve())
            in_target = True
        except (ValueError, OSError):
            in_target = False
        if not source.exists():
            # A missing in-target source is a hole in the grade; a missing
            # out-of-target one is simply not our concern.
            if in_target:
                unreadable += 1
            continue
        cwd = Path.cwd()
        try:
            # A stale database can name a build directory that no longer
            # exists; chdir raises OSError there. That is an unusable entry,
            # not a crash -- the caller's job is to report the database as
            # ungradable, and an escaping FileNotFoundError denies it the
            # chance (Greptile, PR #1513).
            os.chdir(directory)
        except OSError:
            # Unreadable is a hole wherever the source sits: after admitting
            # out-of-target drivers for their in-target cursors, an entry we
            # cannot read might have been the one exposing in-target
            # declarations, and we cannot know (Greptile, PR #1513).
            unreadable += 1
            continue
        try:
            try:
                tu = index.parse(None, args=list(command.arguments)[1:])
            except ci.TranslationUnitLoadError:
                unreadable += 1
                continue
            if any(d.severity >= ci.Diagnostic.Fatal for d in tu.diagnostics):
                unreadable += 1
                continue
            # An entry counts when the oracle can take something from it OR
            # when it is an in-target unit that legitimately declares nothing
            # (a comment-only source is a valid empty grade, not a refusal --
            # an earlier round on this PR fixed exactly that). What must NOT
            # count is an out-of-target driver that reaches no in-target
            # declaration: it is neither a hole nor gradeable.
            #
            # Evaluated INSIDE the chdir: cursor locations come back as the
            # relative paths the command used, and resolving them after
            # restoring the cwd points them outside the target (the same
            # cwd-dependence fixed in `run_cpp_oracle`'s walk).
            if not in_target and not _reaches_inside(tu.cursor, target):
                continue
        finally:
            os.chdir(cwd)
        parsed += 1
    # An in-target entry the oracle cannot read is a hole in the grade, not a
    # file to skip: report it rather than grading the remainder.
    return -unreadable if unreadable else parsed


def cpp_oracle_inheritance(target: Path) -> OracleResult:
    # The libclang oracle emits base-specifiers as `name_edges` keyed the same
    # way javac's are: the subclass pinned to a file and line, the base by
    # SIMPLE name (`_base_simple_name` already collapses `::` and keeps the
    # last component, mirroring cgr's normalisation). So no oracle-side work is
    # needed here either (issue #1190).
    #
    # C++ has no interfaces, so every inheritance edge arrives as INHERITS and
    # there is no IMPLEMENTS counterpart to fold in. The oracle emits no
    # OVERRIDES -- a virtual override is not distinguishable from a shadowing
    # redeclaration without more analysis than the oracle does -- so that
    # category stays empty and _prf omits the row rather than scoring a
    # category the oracle cannot adjudicate.
    graph = run_cpp_oracle(target)
    inherits: set[InheritEdge] = set()
    subclasses: set[str] = set()
    for edge in graph.name_edges:
        # Defensive rather than load-bearing today: the C++ oracle has a single
        # name-edge construction site and it is hard-coded to INHERITS, so this
        # filter currently rejects nothing and no test can distinguish its
        # removal. It stays because the oracle gaining a second edge kind must
        # not silently start scoring that kind as inheritance; the guard in
        # test_cpp_inheritance_eval.py fails if that assumption stops holding.
        if edge.rel_type != _INHERITS:
            continue
        key = _location_key(edge.source.file, edge.source.start_line)
        subclasses.add(key)
        inherits.add((key, edge.target_name))
    return OracleResult(
        inherits=inherits,
        overrides=set(),
        top_classes=frozenset(subclasses),
        override_scope=frozenset(),
    )


def cpp_cgr_inheritance(target: Path, project: str) -> CgrResult:
    ingestor = _capture(target, project)
    props_by_node = {
        (label, str(uid)): props for (label, uid), props in ingestor.nodes.items()
    }
    inherits: set[InheritEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type != _INHERITS:
            continue
        props = props_by_node.get((from_label, str(from_val)))
        path = str(props.get(cs.KEY_PATH, "")) if props else ""
        # A C++ class can be declared in any of the header or source suffixes,
        # so the whole set is eligible rather than one extension.
        if not path.endswith(ec.CPP_SUFFIXES):
            continue
        line = props.get(cs.KEY_START_LINE) if props else None
        # Node properties are a heterogeneous union; a non-integer start_line
        # is unusable as a location key, so skip rather than coerce it.
        if not isinstance(line, int):
            continue
        # The oracle names the base simply, so the qn is reduced to its last
        # segment to compare like with like.
        base = str(to_val).rsplit(cs.SEPARATOR_DOT, 1)[-1]
        inherits.add((_location_key(path, line), base))
    return CgrResult(inherits=inherits, overrides=set())


def csharp_oracle_inheritance(target: Path) -> OracleResult:
    # The Roslyn oracle emits base classes as INHERITS and base interfaces as
    # IMPLEMENTS, both by SIMPLE name with the subtype pinned to a location --
    # the same shape javac's oracle produces, so both kinds fold together here
    # exactly as they do for Java (issue #1190).
    #
    # Unlike the C++ arm, the rel_type filter is load-bearing rather than
    # defensive: this oracle really does emit two kinds, and the pair is
    # deliberately kept rather than split, because cgr's own edges are graded
    # as one supertype relation.
    graph = run_csharp_oracle(target)
    inherits: set[InheritEdge] = set()
    subclasses: set[str] = set()
    for edge in graph.name_edges:
        if edge.rel_type not in (_INHERITS, _IMPLEMENTS):
            continue
        key = _location_key(edge.source.file, edge.source.start_line)
        subclasses.add(key)
        inherits.add((key, edge.target_name))
    return OracleResult(
        inherits=inherits,
        overrides=set(),
        top_classes=frozenset(subclasses),
        override_scope=frozenset(),
    )


def csharp_cgr_inheritance(target: Path, project: str) -> CgrResult:
    ingestor = _capture(target, project)
    props_by_node = {
        (label, str(uid)): props for (label, uid), props in ingestor.nodes.items()
    }
    inherits: set[InheritEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in (_INHERITS, _IMPLEMENTS):
            continue
        props = props_by_node.get((from_label, str(from_val)))
        path = str(props.get(cs.KEY_PATH, "")) if props else ""
        if not path.endswith(ec.CS_SUFFIX):
            continue
        line = props.get(cs.KEY_START_LINE) if props else None
        # Node properties are a heterogeneous union; a non-integer start_line
        # is unusable as a location key, so skip rather than coerce it.
        if not isinstance(line, int):
            continue
        # The oracle names the supertype simply, so the qn is reduced to its
        # last segment to compare like with like.
        base = str(to_val).rsplit(cs.SEPARATOR_DOT, 1)[-1]
        inherits.add((_location_key(path, line), base))
    return CgrResult(inherits=inherits, overrides=set())


def score_inheritance(
    cgr: CgrResult,
    oracle: OracleResult,
    inherits_label: str = ec.INHERITS_LABEL,
) -> ScoreResult:
    # Restrict cgr to the oracle's gradeable universe: top-level subclasses for
    # INHERITS, single-base subclasses for OVERRIDES. This drops nested-class
    # and multi-base-MRO edges the oracle cannot adjudicate, rather than scoring
    # cgr against an incomplete oracle.
    cgr_inh = {e for e in cgr.inherits if e[0] in oracle.top_classes}
    cgr_ovr = {e for e in cgr.overrides if e[0] in oracle.override_scope}
    oracle_inh = oracle.inherits
    oracle_ovr = oracle.overrides
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}

    inh_row = _prf(ec.Category.EDGE.value, inherits_label, cgr_inh, oracle_inh)
    if inh_row is not None:
        rows.append(inh_row)
        diff[ec.INHERITANCE_DIFF_PREFIX + inherits_label] = DiffBucket(
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
    language: Annotated[
        ec.InheritanceLanguage, typer.Option(help=ec.INHERITANCE_LANGUAGE_HELP)
    ] = ec.InheritanceLanguage.PYTHON,
) -> None:
    target = target.resolve()
    project = project_name or target.name
    logger.info(ls.INHERITANCE_TARGET.format(target=target, project=project))

    if language == ec.InheritanceLanguage.CSHARP:
        # Same reasoning as the Java and C++ branches: without the oracle this
        # would write a header-only CSV and exit 0, reporting "no gradeable
        # edges" when the truth is "the grader never ran".
        if not csharp_oracle_available():
            logger.error(ls.CSHARP_ORACLE_MISSING)
            raise typer.Exit(code=1)
        result = score_inheritance(
            csharp_cgr_inheritance(target, project),
            csharp_oracle_inheritance(target),
            inherits_label=ec.CSHARP_SUPERTYPES_LABEL,
        )
        write_outputs(
            result,
            out_dir,
            ec.INHERITANCE_SCORES_FILENAME,
            ec.INHERITANCE_DIFF_FILENAME,
        )
        render(result, ec.INHERITANCE_TITLE)
        return

    if language == ec.InheritanceLanguage.CPP:
        # Same reasoning as the Java branch: without libclang the oracle yields
        # nothing, and scoring that would write a header-only CSV and an empty
        # diff while exiting 0 -- reporting "no gradeable edges" when the truth
        # is "the grader never ran".
        if not cpp_available():
            logger.error(ls.CPP_ORACLE_MISSING)
            raise typer.Exit(code=1)
        # A compilation database is as much a precondition as libclang itself.
        # Absent, libclang raises; present but yielding no translation unit
        # (empty, or naming files that no longer exist), the oracle returns an
        # empty graph and the run scores 0 edges against 0 edges -- an UNGRADED
        # target reported as a clean result. Same fail-open shape the
        # cpp_available() check above exists to prevent (Greptile, PR #1513).
        units = _cpp_compile_db_units(target)
        if units is None:
            logger.error(ls.CPP_ORACLE_NO_COMPILE_DB.format(target=target))
            raise typer.Exit(code=1)
        if units == 0:
            logger.error(ls.CPP_ORACLE_EMPTY_COMPILE_DB.format(target=target))
            raise typer.Exit(code=1)
        if units < 0:
            # Partially readable: some in-target entries parsed and some did
            # not. Grading the remainder scores 1.0 on a half-covered project,
            # because score_inheritance filters the unread files out of BOTH
            # sides via top_classes (Greptile, PR #1513).
            count = -units
            logger.error(
                ls.CPP_ORACLE_PARTIAL_COMPILE_DB.format(
                    count=count, suffix="y" if count == 1 else "ies", target=target
                )
            )
            raise typer.Exit(code=1)
        # The libclang oracle names bases by SIMPLE name while it pins the
        # subclass to a location, so the row carries its own label: a C++ 1.0
        # is not measuring the same unit as the Python one (issue #1190).
        result = score_inheritance(
            cpp_cgr_inheritance(target, project),
            cpp_oracle_inheritance(target),
            inherits_label=ec.CPP_BASES_LABEL,
        )
        write_outputs(
            result,
            out_dir,
            ec.INHERITANCE_SCORES_FILENAME,
            ec.INHERITANCE_DIFF_FILENAME,
        )
        render(result, ec.INHERITANCE_TITLE)
        return

    if language == ec.InheritanceLanguage.JAVA:
        # Without a JDK the oracle yields nothing, and scoring that would write
        # a header-only CSV and an empty diff while exiting 0 -- reporting "no
        # gradeable edges" when the truth is "the grader never ran".
        if not java_available():
            logger.error(ls.JAVA_ORACLE_MISSING)
            raise typer.Exit(code=1)
        # The javac oracle names supertypes by SIMPLE name while it pins the
        # subclass to a location, so the row carries its own label: a Java 1.0
        # is not measuring the same unit as the Python one (issue #1190).
        result = score_inheritance(
            java_cgr_inheritance(target, project),
            java_oracle_inheritance(target),
            inherits_label=ec.JAVA_SUPERTYPES_LABEL,
        )
        write_outputs(
            result,
            out_dir,
            ec.INHERITANCE_SCORES_FILENAME,
            ec.INHERITANCE_DIFF_FILENAME,
        )
        render(result, ec.INHERITANCE_TITLE)
        return

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
