---
description: "Parse, query, and export your codebase in 5 minutes with Code-Graph-RAG."
---

# Quick Start

Get from zero to querying your codebase in 5 minutes.

## Step 1: Parse a Repository

Parse and ingest a multi-language repository into the knowledge graph.

**For the first repository (clean start):**

```bash
cgr start --repo-path /path/to/repo1 --update-graph --clean
```

**For additional repositories (preserve existing data):**

```bash
cgr start --repo-path /path/to/repo2 --update-graph
cgr start --repo-path /path/to/repo3 --update-graph
```

**Control Memgraph batch flushing:**

```bash
cgr start --repo-path /path/to/repo --update-graph --batch-size 5000
```

The system automatically detects and processes files for all supported languages.

## Step 2: Query the Codebase

Start the interactive RAG CLI:

```bash
cgr start --repo-path /path/to/your/repo
```

**Specify custom models:**

```bash
cgr start --repo-path /path/to/your/repo \
  --orchestrator ollama:llama3.2 \
  --cypher ollama:codellama
```

```bash
cgr start --repo-path /path/to/your/repo \
  --orchestrator google:gemini-2.0-flash-thinking-exp-01-21 \
  --cypher google:gemini-2.5-flash-lite-preview-06-17
```

**Example queries:**

- "Show me all classes that contain 'user' in their name"
- "Find functions related to database operations"
- "What methods does the User class have?"
- "Show me functions that handle authentication"
- "List all TypeScript components"
- "Find Rust structs and their methods"
- "Add logging to all database connection functions"
- "Refactor the User class to use dependency injection"

## Step 3: Export Graph Data

**Export during graph update:**

```bash
cgr start --repo-path /path/to/repo --update-graph --clean -o my_graph.json
```

**Export existing graph without updating:**

```bash
cgr export -o my_graph.json
```

**Work with exported data in Python:**

```python
from codebase_rag.graph_loader import load_graph

graph = load_graph("my_graph.json")
summary = graph.summary()
print(f"Total nodes: {summary['total_nodes']}")
print(f"Total relationships: {summary['total_relationships']}")

functions = graph.find_nodes_by_label("Function")
for func in functions[:5]:
    relationships = graph.get_relationships_for_node(func.node_id)
    print(f"Function {func.properties['name']} has {len(relationships)} relationships")
```

## What Next?

- [CLI Reference](../guide/cli-reference.md) for all available commands
- [Interactive Querying](../guide/interactive-querying.md) for query examples
- [Code Optimization](../guide/code-optimization.md) for AI-powered improvements
- [MCP Server](../guide/mcp-server.md) for Claude Code integration
- [Python SDK](../sdk/overview.md) for programmatic access
