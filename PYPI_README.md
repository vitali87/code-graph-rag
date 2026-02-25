# Code-Graph-RAG

A graph-based RAG system that parses multi-language codebases with Tree-sitter, builds knowledge graphs in Memgraph, and enables natural language querying, editing, and optimization.

## Install

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

### Prerequisites

- Python 3.12+
- Docker (for Memgraph)
- `cmake` (for building pymgclient)
- `ripgrep` (`rg`) (for shell command text searching)

## CLI Quick Start

The package installs a `cgr` command.

**Start Memgraph, parse a repo, and query it:**

```bash
docker-compose up -d                       # start Memgraph
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

**AI-guided optimization:**

```bash
cgr optimize python --repo-path ./my-project
```

**Run as an MCP server (for Claude Code):**

```bash
cgr mcp-server
```

**Check your setup:**

```bash
cgr doctor
```

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
| `ORCHESTRATOR_PROVIDER` | | Provider: `google`, `openai`, `ollama` |
| `ORCHESTRATOR_MODEL` | | Model ID (e.g. `gpt-4o`, `gemini-2.5-pro`) |
| `ORCHESTRATOR_API_KEY` | | API key for the provider (not needed for `ollama`) |
| `CYPHER_PROVIDER` | | Provider for Cypher generation |
| `CYPHER_MODEL` | | Model ID for Cypher generation |
| `CYPHER_API_KEY` | | API key for Cypher provider (not needed for `ollama`) |
| `TARGET_REPO_PATH` | `.` | Default repository path |

## Documentation

Full documentation, architecture details, and contribution guide:
[GitHub Repository](https://github.com/vitali87/code-graph-rag)

## License

MIT
