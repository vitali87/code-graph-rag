#!/bin/bash -eu
# Builds one fuzz target per harness in fuzz/.
#
# The harnesses import the package under test, so the project and its parsing
# extras must be installed into the image's interpreter before
# compile_python_fuzzer freezes each target. `treesitter-full` is required:
# fuzz_parse_source refuses to start with no grammars, which would otherwise
# show up as a target that builds and then exits immediately.

python3 -m pip install --upgrade pip
python3 -m pip install ".[treesitter-full]"

# `evals` is NOT part of the installed wheel -- pyproject's package discovery
# includes only codebase_rag*, codec* and cgr* -- but fuzz_incremental_update
# imports evals.cgr_graph for the in-memory graph store it compares against.
# Without the repo root on PYTHONPATH, pyinstaller cannot resolve that import
# and the target builds into something that fails on first run, which reads as
# a fuzzing crash rather than a build mistake. Verified: importing evals with
# the repo root off sys.path raises ModuleNotFoundError.
export PYTHONPATH="$SRC/code-graph-rag${PYTHONPATH:+:$PYTHONPATH}"

for harness in "$SRC/code-graph-rag"/fuzz/fuzz_*.py; do
  # --paths makes the repo root importable to the frozen target too, so the
  # bundled analysis picks up `evals` rather than only the installed packages.
  compile_python_fuzzer "$harness" --paths="$SRC/code-graph-rag"
done

# Ship the seed corpora alongside their targets. compile_python_fuzzer names
# each target after the harness basename, and ClusterFuzzLite picks up
# <target_name>_seed_corpus.zip, so the corpus directory names must match the
# harness basenames -- they do.
for corpus in "$SRC/code-graph-rag"/fuzz/corpus/*/; do
  name="$(basename "$corpus")"
  if [ -d "$corpus" ]; then
    zip -jq "$OUT/${name}_seed_corpus.zip" "$corpus"* || true
  fi
done
