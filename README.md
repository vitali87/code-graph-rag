<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark-any.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/logo-light-any.png">
    <img src="assets/logo-light-any.png" alt="Code-Graph-RAG Logo" width="480">
  </picture>

  <p>
    <a href="https://trendshift.io/repositories/99619" target="_blank" rel="noopener noreferrer"><img src="https://trendshift.io/api/badge/repositories/99619" alt="vitali87/code-graph-rag | Trendshift" width="250" height="55"/></a>
  </p>

  <p>
  <a href="https://github.com/vitali87/code-graph-rag/actions/workflows/ci.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/vitali87/code-graph-rag/ci.yml?branch=main" alt="CI" />
  </a>
  <a href="https://codecov.io/gh/vitali87/code-graph-rag">
    <img src="https://codecov.io/gh/vitali87/code-graph-rag/graph/badge.svg" alt="Codecov" />
  </a>
  <a href="https://sonarcloud.io/summary/overall?id=vitali87_code-graph-rag">
    <img src="https://sonarcloud.io/api/project_badges/measure?project=vitali87_code-graph-rag&metric=alert_status" alt="Quality Gate Status" />
  </a>
  <a href="https://code-graph-rag.com">
    <img src="https://img.shields.io/badge/Enterprise-Support%20%26%20Services-6366f1" alt="Enterprise Support" />
  </a>
  <a href="https://pypi.org/project/code-graph-rag/">
    <img src="https://img.shields.io/pypi/v/code-graph-rag" alt="PyPI Version" />
  </a>
  <a href="https://pepy.tech/projects/code-graph-rag">
    <img src="https://static.pepy.tech/personalized-badge/code-graph-rag?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads" alt="PyPI Downloads" />
  </a>
  <a href="https://skillsllm.com/security-check/DHsMGRb1Ysys">
    <img src="https://skillsllm.com/security-check/badge.svg?owner=vitali87&repo=code-graph-rag" alt="SkillsLLM Security Check" />
  </a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/vitali87/code-graph-rag">
    <img src="https://api.scorecard.dev/projects/github.com/vitali87/code-graph-rag/badge" alt="OpenSSF Scorecard" />
  </a>
  <a href="https://www.bestpractices.dev/projects/13757">
    <img src="https://www.bestpractices.dev/projects/13757/badge" alt="OpenSSF Best Practices" />
  </a>
</p>
</div>

# Code-Graph-RAG

Code-Graph-RAG parses a multi-language codebase with Tree-sitter, builds a knowledge graph of its structure in Memgraph, and lets you query, edit, and optimise that code in plain English. It works across a monorepo of mixed languages under one unified graph schema.

<p align="center">
  <img src="./assets/demo.gif" alt="demo">
</p>

## Latest News 🔥

<!-- SECTION:latest_news -->
- **Transactional Multi-File Edits**: Multi-file edits now run as a transaction with staged verification and undo, alongside concrete-syntax-preserving patchers per language and import rewriting for renames and moves.
- **Scala Inheritance and Markdown Front-Matter**: Scala `INHERITS` edges are read from `extends`/`with` clauses, and declared Markdown front-matter is read onto the `Module` node.
- **Graph Retrieval**: A graph-proximity reranking prototype was added, MCP and CLI graph retrieval is scoped to one project, and call-site and import-site locations are now stored on graph edges.
- **Direct MCP Tools**: `find_duplicate_code` and `get_function_source` are exposed as direct MCP tools, and PHP `use function` imports resolve through declared namespaces.
- **Graph Indexing**: The name property is now indexed for improved graph read-path lookups. SQL routines are also indexed to resolve calls that name their target in a string.
- **Duplicate Code Detection**: An AST-based duplicate code detection system has been introduced, alongside fixes for duplicate detection across multiple repositories.
- **Agentic QA**: A new agentic QA benchmark harness and indexing-time benchmark have been added.
<!-- /SECTION:latest_news -->

See [NEWS.md](NEWS.md) for the full history.

## What It Does

Point Code-Graph-RAG at a repository and it reads every source file, extracts functions, classes, methods, modules, and the relationships between them, and stores the result as an interconnected graph. Once the graph exists you can:

- Ask questions about the codebase in natural language and get answers grounded in the real structure.
- Retrieve the actual source of any function, class, or method by name or by intent.
- Edit code through the agent with AST-based surgical patching and a diff preview before anything changes.
- Optimise code against language best practices or your own coding standards.
- Find dead code by walking call and reference edges from entry points.
- Search and rewrite structurally by AST pattern with ast-grep.
- Overlay runtime behaviour: trace a test run (or pull production eBPF profiles) with `cgr trace` and merge the calls that actually happened into the graph, exposing dispatch that static analysis cannot see.

## How It Works

The system has two components:

1. **Multi-language parser.** A Tree-sitter based parser reads the codebase and ingests functions, classes, methods, modules, and their relationships into Memgraph under a single language-agnostic schema.
2. **RAG system** (`codebase_rag/`). An interactive CLI that turns natural language into Cypher queries, retrieves matching code, and drives AI-powered editing and optimisation.

```
Source Code -> Tree-sitter Parser -> AST Analysis -> Memgraph Knowledge Graph
                                                             |
User Query -> AI Model (Cypher Gen) -> Cypher Query -> Graph Results -> Response
```

See the [Architecture Overview](docs/architecture/overview.md) and [Graph Schema](docs/architecture/graph-schema.md) for the full picture.

## Supported Languages

Python, TypeScript, TSX, JavaScript, Rust, Go, Java, C, C++, C#, PHP, Lua, and Dart are fully supported. Scala is in development, and Ruby, Kotlin, Swift, Elixir, Haskell, Solidity, Bash, and Nix have structural support (modules, functions, classes where the language has them, and imports) through the pluggable ast-grep tier. See the [Language Support](docs/architecture/language-support.md) matrix for per-language capabilities.

## Installation

`cgr` is published to PyPI. Install it system-wide with the `treesitter-full` (all languages) and `semantic` (vector search) extras:

```bash
# with uv (recommended)
uv tool install "code-graph-rag[treesitter-full,semantic]"

# or with pipx
pipx install "code-graph-rag[treesitter-full,semantic]"
```

### Which version am I getting?

Three version lines exist and they intentionally differ:

| where | what it tracks |
|---|---|
| git tags | every version, one per merge |
| GitHub Releases (binaries, signatures) | every 50th version, plus any security fix |
| PyPI | every 50th version, plus any security fix |

So the newest tag on `main` usually runs ahead of the newest release, often by
tens of patch versions; they coincide only just after a release. Nothing is
stuck, the cadences differ by design. A security fix does NOT wait for the
cadence: it ships a release and a PyPI upload immediately.

`uv tool install` and `pipx install` give you the newest PyPI version, which is the
newest RELEASE, not the newest tag. Interim tags exist so every merge is
addressable; binaries and PyPI uploads follow the cadence above.

To run code newer than the latest release, install from git:

```bash
uv tool install "code-graph-rag[treesitter-full,semantic] @ git+https://github.com/vitali87/code-graph-rag@main"
```

You also need Python 3.12+, Docker (for Memgraph), `cmake`, and `ripgrep`. Full prerequisites, source installs, and environment setup are in the [Installation](docs/getting-started/installation.md) guide.

> [!NOTE]
> The wheel is pure Python (`py3-none-any`), so the package itself installs on any platform with Python 3.12 or newer (dependencies may still need platform wheels or build tools, such as `cmake` for `pymgclient`). The [piwheels](https://www.piwheels.org/project/code-graph-rag/) build for Debian Bookworm shows as failed because Bookworm's system Python is 3.11, which is below our floor. On Raspberry Pi OS Bookworm, pin the interpreter explicitly, for example `uv tool install --python 3.12 "code-graph-rag[treesitter-full,semantic]"`; uv downloads Python 3.12 automatically and the PyPI wheel installs normally.

## Quick Start

```bash
# Start the packaged Memgraph + Qdrant stack (no compose file needed)
cgr daemon up

# Parse a repository into the graph, then query it
cgr start --repo-path /path/to/repo --update-graph
cgr start --repo-path /path/to/repo
```

Repeat the first command for each repository you want indexed; the graph is
shared, and syncing one project leaves the others alone. To start over from an
empty graph, add `--clean` — it deletes **every** project in the shared graph,
not just this one, and asks for confirmation first when other
projects would be destroyed.

The [Quick Start](docs/getting-started/quickstart.md) guide walks through parsing, querying, and exporting in five minutes.

## MCP Server

Code-Graph-RAG runs as an [MCP](https://modelcontextprotocol.io) server so Claude Code and other MCP clients can query and edit your codebase directly. See the [MCP Server](docs/guide/mcp-server.md) guide for setup.

## Documentation

**Getting Started**
- [Installation](docs/getting-started/installation.md)
- [Quick Start](docs/getting-started/quickstart.md)
- [Configuration](docs/getting-started/configuration.md)

**User Guide**
- [CLI Reference](docs/guide/cli-reference.md)
- [Interactive Querying](docs/guide/interactive-querying.md)
- [Code Optimisation](docs/guide/code-optimization.md)
- [Dead Code Detection](docs/guide/dead-code.md)
- [Dynamic Call Tracing](docs/guide/dynamic-tracing.md)
- [Graph Export](docs/guide/graph-export.md)
- [Real-Time Updates](docs/guide/realtime-updates.md)
- [C/C++ Semantic Mode](docs/guide/cpp-semantic-mode.md)
- [MCP Server](docs/guide/mcp-server.md)

**Architecture**
- [Overview](docs/architecture/overview.md)
- [Graph Schema](docs/architecture/graph-schema.md)
- [Language Support](docs/architecture/language-support.md)
- [Data-Flow Edges](docs/architecture/data-flow-edges.md)

**Python SDK**
- [Overview](docs/sdk/overview.md)
- [Graph Loader](docs/sdk/graph-loader.md)
- [Cypher Generator](docs/sdk/cypher-generator.md)
- [Semantic Search](docs/sdk/semantic-search.md)

**Advanced**
- [Adding Languages](docs/advanced/adding-languages.md)
- [Ignore Patterns](docs/advanced/ignore-patterns.md)
- [Building Binaries](docs/advanced/building-binaries.md)
- [Troubleshooting](docs/advanced/troubleshooting.md)

## Enterprise Services

Code-Graph-RAG is open source and free to use. For organisations that need more, we offer **fully managed cloud-hosted solutions** and **on-premise deployments**:

- **Cloud-Hosted Deployment**: Managed cloud infrastructure for both the graph database and the AI agent connection. Zero infrastructure overhead, so we handle scaling, updates, and availability while your team focuses on building.
- **On-Premise & Air-Gapped Deployment**: Deploy Code-Graph-RAG entirely within your own environment, including air-gapped networks. Full data sovereignty for regulated industries and security-sensitive organisations.

We also offer custom development, integration consulting, technical support contracts, and team training.

**[View plans & pricing at code-graph-rag.com](https://code-graph-rag.com/enterprise)**

## Contributing

Please see [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. Good first PRs come from the TODO issues.

## Support

For issues or questions, check the [Troubleshooting](docs/advanced/troubleshooting.md) guide first, then open an issue.

## License

MIT. See [LICENSE](LICENSE).
