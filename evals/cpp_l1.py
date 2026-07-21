from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from codebase_rag import constants as cs

from . import constants as ec
from . import logs as ls
from .cgr_graph import extract_cgr_cpp_graph, restrict_to_files
from .oracles import cpp_available, run_cpp_oracle
from .score import score_structure
from .structure_report import render, write_outputs

_TITLE = "cgr L1 structure eval (C/C++ vs libclang)"


def main(
    target: Annotated[
        Path,
        typer.Option(help="Directory of C/C++ sources with a compile_commands.json."),
    ] = Path(ec.CPP_DEFAULT_TARGET),
    project_name: Annotated[
        str, typer.Option(help="cgr project name; defaults to target dir name.")
    ] = "",
    out_dir: Annotated[
        Path, typer.Option(help="Directory for cpp_scores.csv and cpp_diff.json.")
    ] = Path(ec.DEFAULT_OUT_DIR),
) -> None:
    # The L1 structure eval grades the TREE-SITTER extraction against the
    # libclang oracle; the default HYBRID frontend would add macro Function
    # nodes the oracle does not model (the required compdb triggers it).
    from codebase_rag.config import settings

    settings.CPP_FRONTEND = cs.CppFrontend.TREESITTER
    target = target.resolve()
    if not cpp_available() or not (target / ec.CPP_COMPDB_FILENAME).is_file():
        logger.error(
            ls.CPP_ORACLE_MISSING.format(compdb=ec.CPP_COMPDB_FILENAME, target=target)
        )
        raise typer.Exit(code=1)

    project = project_name or target.name

    logger.info(ls.CPP_EXTRACTING_CGR.format(target=target, project=project))
    cgr = extract_cgr_cpp_graph(target, project)
    logger.success(ls.CPP_CGR_DONE.format(count=len(cgr.nodes)))

    logger.info(ls.CPP_EXTRACTING_ORACLE.format(target=target))
    oracle = run_cpp_oracle(target)
    logger.success(ls.CPP_ORACLE_DONE.format(count=len(oracle.nodes)))

    # The compile_commands.json defines the gradeable universe: the oracle only
    # sees files its compiled TUs reach, so scope cgr to those files before
    # scoring. Without this, cgr's whole-tree index (bundled test deps,
    # uncompiled sources) is graded as false positives against a partial oracle.
    cgr = restrict_to_files(cgr, {key.file for key in oracle.nodes})
    logger.success(ls.CPP_CGR_SCOPED.format(count=len(cgr.nodes)))

    result = score_structure(
        cgr, oracle, ec.CPP_SCORED_NODE_KINDS, ec.SCORED_EDGE_TYPES, grade_spans=True
    )
    write_outputs(result, out_dir, ec.CPP_SCORES_FILENAME, ec.CPP_DIFF_FILENAME)
    render(result, _TITLE)


if __name__ == "__main__":
    typer.run(main)
