# C/C++ Semantic Mode

The C/C++ frontend has three modes, selected with the `CPP_FRONTEND` environment variable. The default is `hybrid`.

| Mode | What runs | What you get |
|---|---|---|
| `treesitter` | Tree-sitter only | Definitions, calls, classes for every `.c`/`.cpp`/`.h` file. No preprocessor awareness. |
| `hybrid` (default) | Tree-sitter backbone plus libclang | Everything above, plus macro `Function` nodes, macro-expansion `CALLS` edges, `#include` `IMPORTS` edges, and `using`/`typedef` alias `Type` nodes. Nothing is skipped when libclang cannot parse a file; tree-sitter still covers it. |
| `libclang` | libclang only | Compiler-accurate parsing of the translation units listed in the compile database; files outside it are not covered. |

## Requirements for `hybrid` and `libclang`

Both semantic modes need two things:

1. **The libclang bindings.** They ship as an optional extra:

    ```bash
    pip install "code-graph-rag[cpp]"
    ```

    Without them the frontend automatically falls back to tree-sitter and logs a warning naming this extra.

2. **A `compile_commands.json`.** libclang parses translation units with the exact flags your build uses, discovered from a compile database in the indexed directory, any ancestor, or a conventional `build/` subdirectory beside either. Generate one with:

    ```bash
    cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build
    # or, for non-CMake builds:
    bear -- make
    ```

    Without it the frontend falls back to tree-sitter and logs a warning with these commands.

A repository with no C/C++ files skips all of this silently; the warnings only fire when there is C/C++ source to lose fidelity on.

## Staleness

The parser fingerprint records the resolved mode and whether a compile database is discoverable, not just the configured setting: a graph indexed while libclang was missing reads as stale after you install the `cpp` extra, and one indexed before you generated `compile_commands.json` reads as stale after you do, so the next `--update-graph` rebuilds with the hybrid facts included.
