#!/bin/bash -eu
# Builds one fuzz target per harness in fuzz/.
#
# The harnesses import the package under test, so the project and its parsing
# extras must be installed into the image's interpreter before
# compile_python_fuzzer freezes each target. `treesitter-full` is required:
# fuzz_parse_source refuses to start with no grammars, which would otherwise
# show up as a target that builds and then exits immediately.

# OSS-Fuzz exports sanitizer CFLAGS/CXXFLAGS for the fuzz targets, but pip
# also hands them to every C extension it builds from source, and pymgclient's
# cmake rejects them outright ("invalid integral value '1 -fno-omit-frame-
# pointer ...'", because -O1 and the rest arrive as one token). Nothing the
# harnesses import touches pymgclient -- it is the Memgraph driver -- and a
# fuzz target gets its instrumentation from atheris, not from these flags. So
# install dependencies with a clean environment.
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS \
  python3 -m pip install --break-system-packages ".[treesitter-full]"

# `evals` is NOT part of the installed wheel -- pyproject's package discovery
# includes only codebase_rag*, codec* and cgr* -- but fuzz_incremental_update
# imports evals.cgr_graph for the in-memory graph store it compares against.
# Without the repo root on PYTHONPATH, pyinstaller cannot resolve that import
# and the target builds into something that fails on first run, which reads as
# a fuzzing crash rather than a build mistake. Verified: importing evals with
# the repo root off sys.path raises ModuleNotFoundError.
export PYTHONPATH="$SRC/code-graph-rag${PYTHONPATH:+:$PYTHONPATH}"

# The grammars are loaded with `importlib.import_module(f"tree_sitter_{lang}")`
# on a name built at run time, so PyInstaller's static analysis cannot see a
# single one and bundles none. The frozen target then raises
# "No Tree-sitter languages available" on its first input -- it builds
# perfectly and fuzzes nothing. Name every grammar as a hidden import.
HIDDEN_IMPORTS=""
for grammar in $(python3 -c "
import importlib.util
from codebase_rag.constants.languages import SupportedLanguage
for language in SupportedLanguage:
    module = 'tree_sitter_' + str(language).replace('-', '_')
    if importlib.util.find_spec(module) is not None:
        print(module)
"); do
  HIDDEN_IMPORTS="$HIDDEN_IMPORTS --hidden-import=$grammar"
done
echo "Bundling grammars:$HIDDEN_IMPORTS"
if [ -z "$HIDDEN_IMPORTS" ]; then
  echo "ERROR: no tree_sitter_* grammars found; the parse target would build" >&2
  echo "and then fail on its first input." >&2
  exit 1
fi

# `shell_command` imports pydantic_ai, and that chain reads its own package
# metadata at import time (`importlib.metadata.version`), which PyInstaller
# does not bundle by default -- the target then dies with
# `PackageNotFoundError: No package metadata was found for genai_prices`
# before fuzzing anything. Ship the metadata for the packages in the chain.
METADATA=""
for dist in genai_prices pydantic_ai pydantic_ai_slim pydantic_graph logfire; do
  if python3 -c "import importlib.metadata as m; m.version('$dist')" 2>/dev/null; then
    METADATA="$METADATA --copy-metadata=$dist"
  fi
done
echo "Copying metadata:$METADATA"

for harness in "$SRC/code-graph-rag"/fuzz/fuzz_*.py; do
  # --paths makes the repo root importable to the frozen target too, so the
  # bundled analysis picks up `evals` rather than only the installed packages.
  compile_python_fuzzer "$harness" \
    --paths="$SRC/code-graph-rag" \
    $HIDDEN_IMPORTS \
    $METADATA
done

# Every target must survive being started, not merely built: both failure
# modes above produced binaries that compiled and then raised on the first
# input. ClusterFuzzLite's own build check catches this, but only after the
# whole job; failing here names the target and the reason.
for harness in "$SRC/code-graph-rag"/fuzz/fuzz_*.py; do
  target="$OUT/$(basename -s .py "$harness")"
  echo "Smoke-testing $(basename "$target")"
  if ! "$target" -runs=1 -rss_limit_mb=4096 > /tmp/smoke.log 2>&1; then
    echo "ERROR: $(basename "$target") failed to run a single input:" >&2
    tail -25 /tmp/smoke.log >&2
    exit 1
  fi
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
