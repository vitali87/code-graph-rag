---
description: "Complete CLI reference for Code-Graph-RAG commands and Makefile targets."
---

# CLI Reference

The `cgr` command is the main entry point for Code-Graph-RAG.

## Core Commands

### `cgr start`

Parse a repository and/or start the interactive query CLI.

```bash
cgr start --repo-path /path/to/repo [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--repo-path` | Path to repository (defaults to current directory) |
| `--update-graph` | Parse and ingest the repository into the knowledge graph |
| `--clean` | Clear existing data before ingesting |
| `--batch-size` | Override Memgraph flush batch size |
| `--orchestrator` | Specify provider:model for main operations (e.g., `google:gemini-2.5-pro`, `ollama:llama3.2`) |
| `--cypher` | Specify provider:model for graph queries (e.g., `google:gemini-2.5-flash`, `ollama:codellama`) |
| `-o` | Export graph to JSON file during update |

### `cgr export`

Export the knowledge graph to JSON.

```bash
cgr export -o my_graph.json [--batch-size 5000]
```

### `cgr optimize`

AI-powered codebase optimization.

```bash
cgr optimize <language> --repo-path /path/to/repo [OPTIONS]
```

| Option | Description |
|--------|-------------|
| `--repo-path` | Path to repository |
| `--orchestrator` | Specify provider:model for operations |
| `--batch-size` | Override Memgraph flush batch size |
| `--reference-document` | Path to reference documentation for guided optimization |

Supported languages: `python`, `javascript`, `typescript`, `rust`, `go`, `java`, `scala`, `cpp`

### `cgr mcp-server`

Start the MCP server for Claude Code integration.

```bash
cgr mcp-server
```

### `cgr index`

Index a repository to protobuf for offline use.

```bash
cgr index -o ./index-output --repo-path ./my-project
```

### `cgr doctor`

Check that all required dependencies and services are available.

```bash
cgr doctor
```

### `cgr language`

Manage language support.

```bash
cgr language add-grammar <language-name>
cgr language add-grammar --grammar-url <url>
cgr language list-languages
cgr language remove-language <language-name>
```

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make help` | Show help message |
| `make all` | Install everything for full development environment |
| `make install` | Install project dependencies with full language support |
| `make python` | Install project dependencies for Python only |
| `make dev` | Setup development environment (install deps + pre-commit hooks) |
| `make test` | Run unit tests only (fast, no Docker) |
| `make test-parallel` | Run unit tests in parallel (fast, no Docker) |
| `make test-integration` | Run integration tests (requires Docker) |
| `make test-all` | Run all tests including integration and e2e (requires Docker) |
| `make test-parallel-all` | Run all tests in parallel (requires Docker) |
| `make clean` | Clean up build artifacts and cache |
| `make build-grammars` | Build grammar submodules |
| `make watch` | Watch repository for changes and update graph in real-time |
| `make readme` | Regenerate README.md from codebase |
| `make lint` | Run ruff check |
| `make format` | Run ruff format |
| `make typecheck` | Run type checking with ty |
| `make check` | Run all checks: lint, typecheck, test |
| `make pre-commit` | Run all pre-commit checks locally |
