---
description: "Python SDK overview for Code-Graph-RAG programmatic access."
---

# Python SDK Overview

The `cgr` package provides short imports for programmatic use of Code-Graph-RAG.

## Installation

```bash
pip install code-graph-rag
```

With semantic code search:

```bash
pip install 'code-graph-rag[semantic]'
```

## Quick Example

```python
from cgr import load_graph

graph = load_graph("graph.json")
print(graph.summary())

functions = graph.find_nodes_by_label("Function")
for fn in functions[:5]:
    rels = graph.get_relationships_for_node(fn.node_id)
    print(f"{fn.properties['name']}: {len(rels)} relationships")
```

## Available Modules

| Import | Purpose |
|--------|---------|
| `from cgr import load_graph` | Load and query exported graph data |
| `from cgr import MemgraphIngestor` | Query Memgraph with Cypher directly |
| `from cgr import CypherGenerator` | Generate Cypher from natural language |
| `from cgr import embed_code` | Semantic code search with UniXcoder |
| `from cgr import settings` | Configure providers programmatically |

## Configuration

```python
from cgr import settings

settings.set_orchestrator("openai", "gpt-4o", api_key="sk-...")
settings.set_cypher("google", "gemini-2.5-flash", api_key="your-key")
```

See individual pages for detailed API usage:

- [Graph Loader](graph-loader.md)
- [Cypher Generator](cypher-generator.md)
- [Semantic Search](semantic-search.md)
