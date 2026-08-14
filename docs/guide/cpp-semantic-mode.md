---
description: "Enable compiler-backed C and C++ facts with libclang and a compilation database."
---

# C/C++ Semantic Mode

Code-Graph-RAG defaults to the `hybrid` C/C++ frontend. Tree-sitter remains the structural backbone while libclang adds compiler-derived macro functions, macro-expansion calls, and `#include` relationships. If libclang or a compilation database is unavailable, indexing continues with Tree-sitter and logs the exact setup step that is missing.

## Install libclang

Install the optional C/C++ semantic dependency:

```bash
pip install "code-graph-rag[cpp]"
```

For full Tree-sitter language coverage and C/C++ semantic facts together:

```bash
pip install "code-graph-rag[treesitter-full,cpp]"
```

From a source checkout, use:

```bash
uv sync --extra treesitter-full --extra cpp
```

## Choose a Frontend

Set `CPP_FRONTEND` to one of these modes:

| Mode | Behaviour | Requirements |
|------|-----------|--------------|
| `treesitter` | Uses only Tree-sitter syntax facts. | The C/C++ Tree-sitter grammars, available through `treesitter-full`. |
| `libclang` | Requests libclang as the C/C++ frontend and falls back to Tree-sitter with a diagnostic when a requirement is unavailable. | The `cpp` extra and `compile_commands.json`. |
| `hybrid` | Keeps Tree-sitter as the backbone and layers compiler-derived facts on top. This is the default. | The `cpp` extra and `compile_commands.json` for the semantic layer; otherwise it falls back to Tree-sitter. |

For example:

```bash
export CPP_FRONTEND=hybrid
```

Changing the effective frontend or adding, removing, relocating, or materially changing the discovered `compile_commands.json` invalidates the parser fingerprint, so the next sync warns when an existing graph needs a clean rebuild.

## Generate `compile_commands.json`

For CMake projects, configure a build directory with compilation database export enabled:

```bash
cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
```

For Make-based projects, generate the database with Bear:

```bash
bear -- make
```

Code-Graph-RAG searches the indexed directory and its parents for `compile_commands.json`, including a conventional `build/` directory at each level.
