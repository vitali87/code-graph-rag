# Code-Graph-RAG

Code-Graph-RAG parses a multi-language codebase with Tree-sitter, builds a knowledge graph of its structure in Memgraph, and lets you query, edit, and optimise that code in plain English. It works across a monorepo of mixed languages under one unified graph schema.

## What It Does

Point it at a repository and it reads every source file, extracts functions, classes, methods, and modules along with the relationships between them, and stores the result as an interconnected graph. Once the graph exists you can:

- Ask questions about the codebase in natural language and get answers grounded in the real structure.
- Retrieve the actual source of any function, class, or method by name or by intent.
- Edit code through the agent with AST-based surgical patching and a diff preview before anything changes.
- Search and rewrite code structurally by AST pattern with ast-grep, instead of text or regex.
- Trace data flow through assignments, calls, and I/O sinks via `FLOWS_TO` taint edges.
- Optimise code against language best practices or your own coding standards.
- Find dead code by walking call and reference edges from entry points.
- Group several repositories into a named workspace and query them as one graph.
- Trace calls between microservices: route decorators become endpoint templates, and HTTP client URLs resolve to the handlers that serve them, linking services across project boundaries.

## Supported Languages

Python, TypeScript, TSX, JavaScript, Rust, Go, Java, C, C++, C#, PHP, Lua, and Dart are fully supported. Scala is in development, and Ruby has structural support (modules, functions, classes, and imports) through the pluggable ast-grep tier.

## Install

```bash
pip install code-graph-rag
```

With all Tree-sitter grammars (Python, JS, TS, Rust, Go, Java, Scala, C, C++, C#, PHP, Lua, Dart):

```bash
pip install 'code-graph-rag[treesitter-full]'
```

With semantic code search (UniXcoder embeddings):

```bash
pip install 'code-graph-rag[semantic]'
```

Qdrant is the default vector store for semantic search. To use Milvus Lite,
install `code-graph-rag[semantic,milvus]`, then set
`CGR_VECTOR_STORE_BACKEND=milvus` and `MILVUS_URI=./.milvus_code_embeddings.db`
before indexing.

To compute embeddings on an OpenAI-compatible endpoint (OpenAI, Ollama, vLLM)
instead of locally, set `CGR_EMBEDDING_PROVIDER=openai` with
`OPENAI_EMBEDDING_BASE_URL` and `OPENAI_EMBEDDING_MODEL`; torch and
transformers are then not required locally.

### Prerequisites

- Python 3.12+
- Docker (for Memgraph)
- `cmake` (for building pymgclient)
- `ripgrep` (`rg`) (for shell command text searching)

## CLI Quick Start

The package installs a `cgr` command.

**Start Memgraph, parse a repo, and query it:**

```bash
cgr daemon up                              # start Memgraph + Qdrant
cgr start --repo-path ./my-project \
          --update-graph --clean           # parse & launch interactive chat
```

**Index to protobuf for offline use:**

```bash
cgr index -o ./index-output --repo-path ./my-project
```

**Export knowledge graph to JSON:**

```bash
cgr export -o graph.json
```

**AI-guided optimisation:**

```bash
cgr optimize python --repo-path ./my-project
```

**Find dead code (functions unreachable from any entry point):**

```bash
cgr dead-code                                   # scan the indexed project
cgr dead-code -e main --exclude '*.gen.*'       # add roots, skip generated code
cgr dead-code --format json --fail-on-found     # CI-friendly report
```

Results are candidates for review, not a guaranteed delete list. See the
[Dead Code Detection guide](https://docs.code-graph-rag.com/guide/dead-code/).

**Group repositories into a workspace and query them together:**

```bash
cgr workspace create my-platform
cgr workspace add-repo my-platform ./service-a
cgr workspace add-repo my-platform ./service-b
cgr start --workspace my-platform
```

**Inspect the graph and the stack:**

```bash
cgr stats                                  # node and relationship counts
cgr status                                 # stack state and last sync per project
cgr doctor                                 # check dependencies and configuration
```

## MCP Server

Run `cgr mcp-server` to serve the tools over stdio or HTTP for Claude Code and other MCP clients. The server exposes the full toolbox:

- **Ask and retrieve:** `ask_agent`, `query_code_graph`, `semantic_search`, `get_code_snippet`
- **Structural editing:** `structural_search`, `structural_replace`, `surgical_replace_code`
- **Files and projects:** `read_file`, `write_file`, `list_directory`, `list_projects`, `index_repository`, `update_repository`, `delete_project`

## Python SDK

The `cgr` package provides short imports for programmatic use.

### Load and query an exported graph

```python
from cgr import load_graph

graph = load_graph("graph.json")
print(graph.summary())

functions = graph.find_nodes_by_label("Function")
for fn in functions[:5]:
    rels = graph.get_relationships_for_node(fn.node_id)
    print(f"{fn.properties['name']}: {len(rels)} relationships")
```

### Query Memgraph with Cypher

```python
from cgr import MemgraphIngestor

with MemgraphIngestor(host="localhost", port=7687) as db:
    rows = db.fetch_all("MATCH (f:Function) RETURN f.name LIMIT 10")
    for row in rows:
        print(row)
```

### Generate Cypher from natural language

```python
import asyncio
from cgr import CypherGenerator

async def main():
    gen = CypherGenerator()
    cypher = await gen.generate("Find all classes that inherit from BaseModel")
    print(cypher)

asyncio.run(main())
```

### Semantic code search

Requires the `semantic` extra.

```python
from cgr import embed_code

embedding = embed_code("def authenticate(user, password): ...")
print(f"Embedding dimension: {len(embedding)}")
```

### Configuration

```python
from cgr import settings

settings.set_orchestrator("openai", "gpt-4o", api_key="sk-...")
settings.set_cypher("google", "gemini-2.5-flash", api_key="your-key")
```

## Environment Variables

Configure via `.env` or environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMGRAPH_HOST` | `localhost` | Memgraph hostname |
| `MEMGRAPH_PORT` | `7687` | Memgraph port |
| `ORCHESTRATOR_PROVIDER` | | Provider: `google`, `openai`, `anthropic`, `azure`, `ollama`, `minimax`, `litellm_proxy` |
| `ORCHESTRATOR_MODEL` | | Model ID (e.g. `gpt-4o`, `gemini-2.5-pro`) |
| `ORCHESTRATOR_API_KEY` | | API key for the provider (not needed for `ollama`) |
| `CYPHER_PROVIDER` | | Provider for Cypher generation |
| `CYPHER_MODEL` | | Model ID for Cypher generation (e.g. `codellama`, `gpt-4o-mini`) |
| `CYPHER_API_KEY` | | API key for Cypher provider (not needed for `ollama`) |
| `TARGET_REPO_PATH` | `.` | Default repository path |

## Documentation

Full documentation, architecture details, and contribution guide:
[docs.code-graph-rag.com](https://docs.code-graph-rag.com)

## License

MIT

<!-- mcp-name: io.github.vitali87/code-graph-rag -->
