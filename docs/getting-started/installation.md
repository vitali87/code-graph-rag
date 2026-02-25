---
description: "Install Code-Graph-RAG and set up Memgraph for multi-language codebase analysis."
---

# Installation

## Prerequisites

- Python 3.12+
- Docker & Docker Compose (for Memgraph)
- **cmake** (required for building pymgclient dependency)
- **ripgrep** (`rg`) (required for shell command text searching)
- **For cloud models**: Google Gemini API key, OpenAI API key, or both
- **For local models**: Ollama installed and running
- `uv` package manager (recommended) or `pip`

### Installing cmake and ripgrep

=== "macOS"

    ```bash
    brew install cmake ripgrep
    ```

=== "Ubuntu/Debian"

    ```bash
    sudo apt-get update
    sudo apt-get install cmake ripgrep
    ```

=== "CentOS/RHEL"

    ```bash
    sudo yum install cmake
    sudo dnf install ripgrep
    ```

    ripgrep may need to be installed from EPEL or via `cargo install ripgrep`.

## Install from PyPI

```bash
pip install code-graph-rag
```

With all Tree-sitter grammars (Python, JS, TS, Rust, Go, Java, Scala, C++, Lua):

```bash
pip install 'code-graph-rag[treesitter-full]'
```

With semantic code search (UniXcoder embeddings):

```bash
pip install 'code-graph-rag[semantic]'
```

## Install from Source

```bash
git clone https://github.com/vitali87/code-graph-rag.git
cd code-graph-rag
```

For basic Python support:

```bash
uv sync
```

For full multi-language support:

```bash
uv sync --extra treesitter-full
```

For development (including tests and pre-commit hooks):

```bash
make dev
```

This installs all dependencies and sets up pre-commit hooks automatically.

## Start Memgraph

```bash
docker compose up -d
```

This starts the Memgraph database on port 7687 and Memgraph Lab on port 3000.

## Set Up Environment Variables

```bash
cp .env.example .env
# Edit .env with your configuration
```

See the [Configuration](configuration.md) guide for all available options.

## Verify Your Setup

```bash
cgr doctor
```

This checks that all required dependencies and services are available.
