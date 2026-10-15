"""Shared driver for the per-language call-retrieval evals.

Every evals/*_retrieval.py ran the same program: refuse to run without the
language oracle, capture the oracle's first-party call sites, capture cgr's
CALLS edges for the same name universe, reduce each to (caller_file,
callee_simple_name), score the two sets, then write and render the result.
The languages differ only in the settings each registers in
`RETRIEVAL_SPECS`, so the program lives here once and the language modules
keep just their CLI and the public names their tests import.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import _capture
from .oracle_guard import oracle_unavailable_exits, require_oracle
from .oracles import (
    cpp_available,
    csharp_oracle_available,
    csharp_oracle_skip_reason,
    go_available,
    java_available,
    lua_oracle_available,
    lua_oracle_skip_reason,
    php_oracle_available,
    php_oracle_skip_reason,
    run_c_call_oracle,
    run_cpp_call_oracle,
    run_csharp_call_oracle,
    run_go_call_oracle,
    run_java_call_oracle,
    run_javascript_call_oracle,
    run_lua_call_oracle,
    run_php_call_oracle,
    run_rust_call_oracle,
    run_scala_call_oracle,
    run_typescript_call_oracle,
    rust_available,
    scala_available,
    typescript_available,
    typescript_skip_reason,
)
from .score import _prf
from .structure_report import render, write_outputs
from .types_defs import (
    CallEdge,
    CallOracle,
    DiffBucket,
    LocationStats,
    ScoreResult,
    ScoreRow,
)

EMPTY_LOCATION = LocationStats(0, 0, 0, 0.0, 0)
CALLS = cs.RelationshipType.CALLS.value
INSTANTIATES = cs.RelationshipType.INSTANTIATES.value


class RetrievalSpec(NamedTuple):
    lang: ec.RetrievalLanguage
    language: str
    oracle: str
    suffixes: str | tuple[str, ...]
    available: Callable[[], bool]
    run_oracle: Callable[[Path], CallOracle]
    oracle_missing: str
    binary: str = ""
    skip_reason: Callable[[], str | None] | None = None
    rel_types: tuple[str, ...] = (CALLS,)
    # Whether a callee qn carries an overload signature (Class.name(args))
    # that has to go before it can match the oracle's simple name.
    strip_signature: bool = False


RETRIEVAL_SPECS: dict[ec.RetrievalLanguage, RetrievalSpec] = {
    spec.lang: spec
    for spec in (
        RetrievalSpec(
            lang=ec.RetrievalLanguage.C,
            language=ec.C_LANGUAGE_NAME,
            oracle=ec.LIBCLANG_ORACLE_NAME,
            suffixes=ec.C_SUFFIXES,
            available=cpp_available,
            run_oracle=run_c_call_oracle,
            oracle_missing=ls.C_ORACLE_MISSING,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.CPP,
            language=ec.CPP_LANGUAGE_NAME,
            oracle=ec.LIBCLANG_ORACLE_NAME,
            suffixes=ec.CPP_SUFFIXES,
            available=cpp_available,
            run_oracle=run_cpp_call_oracle,
            oracle_missing=ls.CPP_RETRIEVAL_ORACLE_MISSING,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.CSHARP,
            language=ec.CSHARP_LANGUAGE_NAME,
            oracle=ec.ROSLYN_ORACLE_NAME,
            suffixes=ec.CS_SUFFIX,
            available=csharp_oracle_available,
            run_oracle=run_csharp_call_oracle,
            oracle_missing=ls.CSHARP_ORACLE_MISSING,
            binary=ec.DOTNET_BIN,
            skip_reason=csharp_oracle_skip_reason,
            # INSTANTIATES counts too (as in the Python retrieval): `new T()`
            # on a type with no explicit constructor has no ctor node to CALL,
            # only an INSTANTIATES edge to the class, which the oracle records
            # by type name.
            rel_types=(CALLS, INSTANTIATES),
            strip_signature=True,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.GO,
            language=ec.GO_LANGUAGE_NAME,
            oracle=ec.GO_AST_ORACLE_NAME,
            suffixes=ec.GO_SUFFIX,
            available=go_available,
            run_oracle=run_go_call_oracle,
            oracle_missing=ls.GO_ORACLE_MISSING,
            binary=ec.GO_BIN,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.JAVA,
            language=ec.JAVA_LANGUAGE_NAME,
            oracle=ec.JAVAC_ORACLE_NAME,
            suffixes=ec.JAVA_SUFFIX,
            available=java_available,
            run_oracle=run_java_call_oracle,
            oracle_missing=ls.JAVA_ORACLE_MISSING,
            binary=ec.JAVAC_BIN,
            strip_signature=True,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.JS,
            language=ec.JS_LANGUAGE_NAME,
            oracle=ec.TSC_ORACLE_NAME,
            suffixes=ec.JS_SUFFIXES,
            available=typescript_available,
            run_oracle=run_javascript_call_oracle,
            oracle_missing=ls.TS_ORACLE_MISSING,
            binary=ec.NODE_BIN,
            skip_reason=typescript_skip_reason,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.LUA,
            language=ec.LUA_LANGUAGE_NAME,
            oracle=ec.LUAPARSE_ORACLE_NAME,
            suffixes=ec.LUA_SUFFIX,
            available=lua_oracle_available,
            run_oracle=run_lua_call_oracle,
            oracle_missing=ls.LUA_ORACLE_MISSING,
            binary=ec.NODE_BIN,
            skip_reason=lua_oracle_skip_reason,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.PHP,
            language=ec.PHP_LANGUAGE_NAME,
            oracle=ec.PHP_PARSER_ORACLE_NAME,
            suffixes=ec.PHP_SUFFIX,
            available=php_oracle_available,
            run_oracle=run_php_call_oracle,
            oracle_missing=ls.PHP_ORACLE_MISSING,
            binary=ec.NODE_BIN,
            skip_reason=php_oracle_skip_reason,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.RUST,
            language=ec.RUST_LANGUAGE_NAME,
            oracle=ec.SYN_ORACLE_NAME,
            suffixes=ec.RS_SUFFIX,
            available=rust_available,
            run_oracle=run_rust_call_oracle,
            oracle_missing=ls.RS_ORACLE_MISSING,
            binary=ec.CARGO_BIN,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.SCALA,
            language=ec.SCALA_LANGUAGE_NAME,
            oracle=ec.SCALAMETA_ORACLE_NAME,
            suffixes=ec.SCALA_SUFFIXES,
            available=scala_available,
            run_oracle=run_scala_call_oracle,
            oracle_missing=ls.SCALA_ORACLE_MISSING,
            binary=ec.SCALA_CLI_BIN,
            # Defensive: a Scala qn is dotted scope, but a parameter signature
            # must not survive into the simple name either.
            strip_signature=True,
        ),
        RetrievalSpec(
            lang=ec.RetrievalLanguage.TS,
            language=ec.TS_LANGUAGE_NAME,
            oracle=ec.TSC_ORACLE_NAME,
            suffixes=ec.TS_SUFFIXES,
            available=typescript_available,
            run_oracle=run_typescript_call_oracle,
            oracle_missing=ls.TS_ORACLE_MISSING,
            binary=ec.NODE_BIN,
            skip_reason=typescript_skip_reason,
        ),
    )
}


def cgr_call_edges(
    spec: RetrievalSpec,
    target: Path,
    project: str,
    declared: frozenset[str],
    covered: frozenset[str] | None = None,
) -> set[CallEdge]:
    """cgr's call edges for `target`, as (caller_file, callee_simple_name).

    `covered` restricts grading to the files the oracle parsed cleanly (its
    authoritative set); languages whose oracle covers everything pass None.
    """
    ingestor = _capture(target, project)
    caller_path: dict[tuple[str, str], str] = {
        (str(label), str(uid)): str(props[cs.KEY_PATH])
        for (label, uid), props in ingestor.nodes.items()
        if props.get(cs.KEY_PATH) and str(props[cs.KEY_PATH]).endswith(spec.suffixes)
    }
    edges: set[CallEdge] = set()
    for from_label, from_val, rel_type, _to_label, to_val in ingestor.rels:
        if rel_type not in spec.rel_types:
            continue
        path = caller_path.get((str(from_label), str(from_val)))
        if path is None or (covered is not None and path not in covered):
            continue
        name = str(to_val).split(cs.SEPARATOR_DOT)[-1]
        if spec.strip_signature:
            name = name.split(cs.CHAR_PAREN_OPEN)[0]
        if name in declared:
            edges.add((path, name))
    return edges


def score_retrieval(
    spec: RetrievalSpec, cgr: set[CallEdge], oracle: set[CallEdge]
) -> ScoreResult:
    """One RETRIEVAL row plus its missing/extra diff bucket."""
    label = ec.RetrievalCondition.GRAPH.value
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    row = _prf(ec.Category.RETRIEVAL.value, label, cgr, oracle)
    if row is not None:
        rows.append(row)

        def _repr(edge: CallEdge) -> str:
            return ec.CALL_EDGE_REPR.format(file=edge[0], name=edge[1])

        diff[ec.LANG_RETRIEVAL_DIFF_PREFIX.format(lang=spec.lang) + label] = DiffBucket(
            missing=[_repr(e) for e in sorted(oracle - cgr)],
            extra=[_repr(e) for e in sorted(cgr - oracle)],
        )
    return ScoreResult(rows=rows, location=EMPTY_LOCATION, diff=diff)


def run_retrieval_eval(
    spec: RetrievalSpec, target: Path, project_name: str, out_dir: Path
) -> None:
    require_oracle(spec.available, spec.oracle_missing, spec.binary, spec.skip_reason)

    target = target.resolve()
    project = project_name or target.name

    # The oracle runs first: its declared names are the universe cgr's edges
    # are held to, and its covered files the set they are graded within.
    logger.info(
        ls.LANG_RETRIEVAL_ORACLE.format(
            oracle=spec.oracle, language=spec.language, target=target
        )
    )
    with oracle_unavailable_exits():
        oracle = spec.run_oracle(target)
    logger.success(
        ls.LANG_RETRIEVAL_ORACLE_DONE.format(
            oracle=spec.oracle, language=spec.language, count=len(oracle.edges)
        )
    )
    if oracle.covered is not None:
        logger.info(
            ls.LANG_RETRIEVAL_COVERED.format(
                language=spec.language, count=len(oracle.covered)
            )
        )

    logger.info(
        ls.LANG_RETRIEVAL_CGR.format(
            language=spec.language, target=target, project=project
        )
    )
    cgr = cgr_call_edges(spec, target, project, oracle.declared, oracle.covered)
    logger.success(
        ls.LANG_RETRIEVAL_CGR_DONE.format(language=spec.language, count=len(cgr))
    )

    result = score_retrieval(spec, cgr, oracle.edges)
    write_outputs(
        result,
        out_dir,
        ec.LANG_RETRIEVAL_SCORES_FILENAME.format(lang=spec.lang),
        ec.LANG_RETRIEVAL_DIFF_FILENAME.format(lang=spec.lang),
    )
    render(
        result,
        ec.LANG_RETRIEVAL_TITLE.format(language=spec.language, oracle=spec.oracle),
    )
