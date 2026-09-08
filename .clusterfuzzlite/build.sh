#!/bin/bash -eu
# Builds one fuzz target per harness in fuzz/.
#
# The harnesses import the package under test, so the project and its parsing
# extras must be installed into the image's interpreter before compile_python_fuzzer
# freezes each target. `treesitter-full` is required: fuzz_parse_source refuses
# to start with no grammars, which would otherwise show up as a target that
# builds and then exits immediately.

python3 -m pip install --upgrade pip
python3 -m pip install ".[treesitter-full]"

for harness in "$SRC/code-graph-rag"/fuzz/fuzz_*.py; do
  compile_python_fuzzer "$harness"
done

# Ship the seed corpora alongside their targets. ClusterFuzzLite picks up
# <target_name>_seed_corpus.zip automatically.
for corpus in "$SRC/code-graph-rag"/fuzz/corpus/*/; do
  name="$(basename "$corpus")"
  if [ -d "$corpus" ]; then
    zip -j "$OUT/${name}_seed_corpus.zip" "$corpus"* || true
  fi
done
