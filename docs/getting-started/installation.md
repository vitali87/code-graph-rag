---
description: "Install Code-Graph-RAG and set up Memgraph for multi-language codebase analysis."
---

# Installation

## Prerequisites

- Python 3.12+ (the wheel is pure Python, but the interpreter floor is strict; Debian Bookworm ships Python 3.11, which is why the [piwheels](https://www.piwheels.org/project/code-graph-rag/) Bookworm build shows as failed. On Raspberry Pi OS Bookworm, run the install commands below inside a Python 3.12 environment, for example `uv venv --python 3.12 --seed && source .venv/bin/activate`, so pip exists in the environment and actually uses 3.12.)
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

    If `pymgclient` has no prebuilt wheel for your platform (for example older
    macOS x86_64 setups) and pip falls back to building it from source, the
    build also needs Homebrew's OpenSSL and, if CMake cannot find it,
    `pkg-config`:

    ```bash
    brew install pkg-config openssl
    export OPENSSL_ROOT_DIR="$(brew --prefix openssl)"
    export PKG_CONFIG_PATH="$(brew --prefix openssl)/lib/pkgconfig:$PKG_CONFIG_PATH"
    ```

    Run the install command again in the same shell so the variables are
    picked up. See [Troubleshooting](../advanced/troubleshooting.md) for the
    symptoms this fixes.

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

With all Tree-sitter grammars (Python, JS, TS, Rust, Go, Java, Scala, C, C++, Lua, PHP, C#, Dart):

```bash
pip install 'code-graph-rag[treesitter-full]'
```

With semantic code search (UniXcoder embeddings):

```bash
pip install 'code-graph-rag[semantic]'
```

With both full language support and semantic search:

```bash
pip install 'code-graph-rag[treesitter-full,semantic]'
```

With compiler-backed C/C++ facts:

```bash
pip install 'code-graph-rag[treesitter-full,cpp]'
```

The `cpp` extra installs libclang. Semantic C/C++ indexing also needs a `compile_commands.json`; see [C/C++ Semantic Mode](../guide/cpp-semantic-mode.md) for frontend modes and setup commands.

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

For full multi-language support with compiler-backed C/C++ facts:

```bash
uv sync --extra treesitter-full --extra cpp
```

For development (including tests and pre-commit hooks):

```bash
make dev
```

This installs all dependencies and sets up pre-commit hooks automatically.

## Verify Release Artifacts

Each [GitHub release](https://github.com/vitali87/code-graph-rag/releases) ships prebuilt binaries together with Sigstore signatures (`*.sigstore.json`); releases from v0.0.484 onwards also carry a SLSA build provenance attestation (`multiple.intoto.jsonl`). Both are produced by the `build-binaries.yml` GitHub Actions workflow using keyless signing, so there is no maintainer-held key to obtain: verification checks that the artifact was built by this repository's release workflow.

To verify provenance with the [GitHub CLI](https://cli.github.com/):

```bash
gh attestation verify code-graph-rag-linux-amd64 \
  --repo vitali87/code-graph-rag \
  --signer-workflow vitali87/code-graph-rag/.github/workflows/build-binaries.yml
```

The `--signer-workflow` flag pins the attestation to the release workflow itself; `--repo` alone accepts an attestation signed by any workflow in the repository.

To verify a signature with [cosign](https://docs.sigstore.dev/cosign/system_config/installation/):

```bash
cosign verify-blob \
  --bundle code-graph-rag-linux-amd64.sigstore.json \
  --certificate-identity-regexp 'https://github\.com/vitali87/code-graph-rag/\.github/workflows/build-binaries\.yml@.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  code-graph-rag-linux-amd64
```

Substitute the binary name for your platform. Packages installed from PyPI are protected differently: `pip` and `uv` verify package hashes, which proves integrity in transit, and releases after v0.0.187 additionally carry a [PEP 740](https://peps.python.org/pep-0740/) attestation. That is a publish attestation — proof that the file was uploaded by this project's trusted publisher — rather than build provenance, and it can be queried through PyPI's [Integrity API](https://docs.pypi.org/api/integrity/).

## Start Memgraph

```bash
cgr daemon up
```

This starts the packaged Memgraph + Qdrant stack and waits until it is healthy. It works the same whether you installed from PyPI or from source, since the compose file ships inside the package. Memgraph listens on port 7687 and Memgraph Lab on port 3000.

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

## Key Dependencies

<!-- SECTION:dependencies -->
- **loguru**: Python logging made (stupidly) simple
- **mcp**: Model Context Protocol SDK
- **pydantic-ai**: AI Agent Framework, the Pydantic way
- **pydantic-settings**: Settings management using Pydantic
- **pymgclient**: Memgraph database adapter for Python language
- **python-dotenv**: Read key-value pairs from a .env file and set them as environment variables
- **tiktoken**: tiktoken is a fast BPE tokeniser for use with OpenAI's models
- **toml**: Python Library for Tom's Obvious, Minimal Language
- **tree-sitter-python**: Python grammar for tree-sitter
- **tree-sitter**: Python bindings to the Tree-sitter parsing library
- **watchdog**: Filesystem events monitoring
- **typer**: Typer, build great CLIs. Easy to code. Based on Python type hints.
- **rich**: Render rich text, tables, progress bars, syntax highlighting, markdown and more to the terminal
- **prompt-toolkit**: Library for building powerful interactive command lines in Python
- **diff-match-patch**: Repackaging of Google's Diff Match and Patch libraries.
- **click**: Composable command line interface toolkit
- **protobuf**
- **defusedxml**: XML bomb protection for Python stdlib modules
- **huggingface-hub**: Client library to download and publish models, datasets and other repos on the huggingface.co hub
- **pathspec**: Utility library for gitignore style pattern matching of file paths.
- **pygments**: Pygments is a syntax highlighting package written in Python.
<!-- /SECTION:dependencies -->
