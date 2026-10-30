"""The SCIP indexers the call oracle knows how to run, one per language.

Each spec says how to invoke the indexer on a project root and how call
syntax reads in that language. The two npm indexers (scip-python and
scip-typescript) are pinned in `evals/scip/node/package.json` and installed
on first use; the others run when their binary is on PATH. Any other SCIP
emitter can be graded too by passing its `index.scip` with `--index`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from loguru import logger

from codebase_rag import constants as cs

from .. import constants as ec
from ..oracles._common import ensure_node_deps
from . import constants as sc
from . import logs as sl
from .proto import read_index
from .types_defs import ScipIndex


class IndexerSpec(NamedTuple):
    language: sc.ScipLanguage
    suffixes: tuple[str, ...]
    binary: str
    # Argument template; `{output}` and `{project}` are filled per run.
    args: tuple[str, ...]
    # Installed from evals/scip/node when the binary is not on PATH.
    npm_installed: bool = False
    # A file the project root must hold for the indexer to run.
    requires: str = ""
    # Written into the staged copy when the project has no tsconfig.json.
    tsconfig: str = ""
    tagged_templates: bool = False
    type_arguments: bool = False
    decorator_calls: bool = False
    property_decorators: frozenset[str] = frozenset()
    # Python: `Cls(...)` resolves to the class; this member is the callee.
    constructor_member: str = ""
    # JS/TS: `const f = () => ...` defines a variable that is a function.
    function_bindings: bool = False


INDEXERS: dict[sc.ScipLanguage, IndexerSpec] = {
    spec.language: spec
    for spec in (
        IndexerSpec(
            language=sc.ScipLanguage.PYTHON,
            suffixes=(cs.EXT_PY,),
            binary=sc.SCIP_PYTHON,
            args=(
                "index",
                ".",
                "--project-name",
                "{project}",
                "--project-version",
                sc.PROJECT_VERSION,
                "--output",
                "{output}",
            ),
            npm_installed=True,
            decorator_calls=True,
            property_decorators=ec.PROPERTY_DECORATORS,
            constructor_member=ec.INIT_STEM,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.TYPESCRIPT,
            suffixes=ec.TS_SUFFIXES,
            binary=sc.SCIP_TYPESCRIPT,
            args=("index", "--output", "{output}"),
            npm_installed=True,
            tsconfig=sc.TS_TSCONFIG,
            tagged_templates=True,
            type_arguments=True,
            function_bindings=True,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.JAVASCRIPT,
            suffixes=ec.JS_SUFFIXES,
            binary=sc.SCIP_TYPESCRIPT,
            args=("index", "--output", "{output}"),
            npm_installed=True,
            tsconfig=sc.JS_TSCONFIG,
            tagged_templates=True,
            function_bindings=True,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.RUST,
            suffixes=(ec.RS_SUFFIX,),
            binary=sc.RUST_ANALYZER,
            args=("scip", ".", "--output", "{output}"),
            requires=sc.CARGO_TOML,
            type_arguments=True,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.GO,
            suffixes=(ec.GO_SUFFIX,),
            binary=sc.SCIP_GO,
            args=("--output", "{output}"),
            requires=sc.GO_MOD,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.JAVA,
            suffixes=(ec.JAVA_SUFFIX,),
            binary=sc.SCIP_JAVA,
            args=("index", "--output", "{output}"),
            type_arguments=True,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.C,
            suffixes=ec.C_SUFFIXES,
            binary=sc.SCIP_CLANG,
            args=(sc.CLANG_COMPDB_ARG, sc.CLANG_OUTPUT_ARG),
            requires=sc.COMPILE_COMMANDS,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.CPP,
            suffixes=ec.CPP_SUFFIXES,
            binary=sc.SCIP_CLANG,
            args=(sc.CLANG_COMPDB_ARG, sc.CLANG_OUTPUT_ARG),
            requires=sc.COMPILE_COMMANDS,
            type_arguments=True,
        ),
        IndexerSpec(
            language=sc.ScipLanguage.CSHARP,
            suffixes=(ec.CS_SUFFIX,),
            binary=sc.SCIP_DOTNET,
            args=("index", "--output", "{output}"),
            type_arguments=True,
        ),
    )
}


class IndexerUnavailable(RuntimeError):
    pass


def resolve_binary(spec: IndexerSpec) -> str | None:
    if found := shutil.which(spec.binary):
        return found
    if not spec.npm_installed or shutil.which(ec.NPM_BIN) is None:
        return None
    ensure_node_deps(sc.NODE_DIR)
    local = sc.NODE_DIR / ec.NODE_MODULES_DIRNAME / sc.NPM_BIN_DIR / spec.binary
    return str(local) if local.exists() else None


def skip_reason(spec: IndexerSpec, target: Path) -> str | None:
    if spec.requires and not (target / spec.requires).exists():
        return sl.MISSING_PROJECT_FILE.format(
            language=spec.language.value, file=spec.requires, target=target
        )
    if shutil.which(spec.binary) is None and not (
        spec.npm_installed and shutil.which(ec.NPM_BIN)
    ):
        return sl.MISSING_INDEXER.format(
            language=spec.language.value, binary=spec.binary
        )
    return None


def _stage(target: Path, workspace: Path) -> Path:
    staged = workspace / target.name
    shutil.copytree(
        target,
        staged,
        ignore=shutil.ignore_patterns(*ec.IGNORE_DIRS, *sc.STAGE_IGNORE),
        symlinks=True,
    )
    return staged


def run_indexer(
    spec: IndexerSpec, target: Path, project: str, timeout: int = sc.INDEX_TIMEOUT_S
) -> ScipIndex:
    """Index a staged copy of `target`: indexers may write into the tree."""
    if (reason := skip_reason(spec, target)) is not None:
        raise IndexerUnavailable(reason)
    binary = resolve_binary(spec)
    if binary is None:
        raise IndexerUnavailable(
            sl.MISSING_INDEXER.format(language=spec.language.value, binary=spec.binary)
        )
    with tempfile.TemporaryDirectory(prefix=sc.WORKSPACE_PREFIX) as workspace:
        staged = _stage(target, Path(workspace))
        if spec.tsconfig and not (staged / sc.TSCONFIG).exists():
            (staged / sc.TSCONFIG).write_text(spec.tsconfig, encoding=cs.ENCODING_UTF8)
        output = Path(workspace) / sc.INDEX_FILENAME
        args = [a.format(output=output, project=project) for a in spec.args]
        logger.info(sl.INDEXING.format(binary=spec.binary, target=target))
        env = dict(os.environ)
        options = env.get(sc.NODE_OPTIONS_ENV, "")
        if spec.npm_installed and sc.NODE_HEAP_FLAG not in options:
            # Node's default heap is too small for a repository-sized
            # program; a heap size the user already chose wins.
            env[sc.NODE_OPTIONS_ENV] = f"{options} {sc.NODE_HEAP_OPTION}".strip()
        proc = subprocess.run(
            [binary, *args],
            cwd=staged,
            env=env,
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            errors=sc.DECODE_ERRORS,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0 or not output.exists():
            raise RuntimeError(
                sl.INDEXER_FAILED.format(
                    binary=spec.binary,
                    code=proc.returncode,
                    stderr=(proc.stderr or proc.stdout)[-sc.STDERR_TAIL :],
                )
            )
        return read_index(output)
