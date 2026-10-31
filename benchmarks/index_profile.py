"""Where indexing spends its time, from a py-spy recording.

Record an index run with py-spy's raw (collapsed) format, then summarise it:

    py-spy record --rate 200 --format raw -o run.txt -- \\
        python -c "from codebase_rag.cli import app; app()" \\
        start --repo-path REPO --update-graph --clean --yes --no-embeddings
    python benchmarks/index_profile.py run.txt

Sampling leaves the interpreter untouched, so unlike cProfile it does not
inflate pure-Python code against C. The stages are the ones
docs/reports/INDEXER_RUST_PLAN.md sizes a Rust port by.
"""

import sys
from collections import Counter

# The first rule matching any frame of a stack, from the root, names its stage.
STAGES = (
    (
        "embeddings: model inference",
        ("torch/", "transformers/", "sentence_transformers/", "embedder"),
    ),
    ("embeddings: batch flush", ("_flush_embedding_batch",)),
    ("embeddings: source extraction", ("_embedding_candidate",)),
    ("calls pass", ("_process_function_calls",)),
    ("definitions pass", ("_process_files",)),
    ("graph store (ingest + SQLite)", ("graph_service.py", "embedded_graph/")),
    ("file scan + hashing", ("_scan_eligible_file", "_hash_file")),
    ("imports / startup", ("_find_and_load",)),
)
OWN = "codebase_rag/"
SITE = "site-packages/"
MODULE = "<module> ("
IMPORT = "_find_and_load "
TOP = 20


def _samples(path: str):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            stack, _, count = line.rstrip("\n").rpartition(" ")
            # The first frame names the process.
            yield stack.split(";")[1:], int(count)


def _stage(frames: list[str]) -> str:
    for name, keys in STAGES:
        if any(key in frame for frame in frames for key in keys):
            return name
    return "other"


def _own_function(frames: list[str]) -> str | None:
    own = [frame for frame in frames if OWN in frame]
    if not own:
        return None
    name, _, location = own[-1].partition(" (")
    return f"{location.split(':')[0].split(OWN)[-1]}:{name}"


def _imported_package(frames: list[str]) -> str | None:
    if not any(frame.startswith(IMPORT) for frame in frames):
        return None
    # py-spy prints a module's path relative to the sys.path entry holding it,
    # so its first component is the package. The outermost one not cgr's own
    # is what cgr chose to import.
    modules = [f.split("(")[1].split(":")[0] for f in frames if f.startswith(MODULE)]
    foreign = [m for m in modules[1:] if not m.startswith(OWN)]
    if foreign:
        return foreign[0].split(SITE)[-1].split("/")[0]
    return modules[-1] if modules else None


def main(path: str) -> None:
    stages: Counter[str] = Counter()
    functions: Counter[str] = Counter()
    imports: Counter[str] = Counter()
    total = 0
    for frames, count in _samples(path):
        total += count
        stage = _stage(frames)
        stages[stage] += count
        if function := _own_function(frames):
            functions[function] += count
        if stage == "imports / startup" and (package := _imported_package(frames)):
            imports[package] += count

    def show(title: str, counts: Counter[str]) -> None:
        print(f"-- {title}")
        for name, count in counts.most_common(TOP):
            print(f"{100 * count / total:5.1f}%  {name}")

    print(f"{total} samples")
    show("stages", stages)
    show("innermost cgr function", functions)
    show("imports by package", imports)


if __name__ == "__main__":
    main(sys.argv[1])
