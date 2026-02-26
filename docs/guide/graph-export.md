---
description: "Export the Code-Graph-RAG knowledge graph to JSON for programmatic analysis and integration."
---

# Graph Export

Export the entire knowledge graph to JSON for programmatic access and integration with other tools.

## Export Commands

**Export during graph update:**

```bash
cgr start --repo-path /path/to/repo --update-graph --clean -o my_graph.json
```

**Export existing graph without updating:**

```bash
cgr export -o my_graph.json
```

**Adjust Memgraph batching during export:**

```bash
cgr export -o my_graph.json --batch-size 5000
```

## Working with Exported Data

```python
from codebase_rag.graph_loader import load_graph

graph = load_graph("my_graph.json")

summary = graph.summary()
print(f"Total nodes: {summary['total_nodes']}")
print(f"Total relationships: {summary['total_relationships']}")

functions = graph.find_nodes_by_label("Function")
classes = graph.find_nodes_by_label("Class")

for func in functions[:5]:
    relationships = graph.get_relationships_for_node(func.node_id)
    print(f"Function {func.properties['name']} has {len(relationships)} relationships")
```

## Example Analysis Script

```bash
python examples/graph_export_example.py my_graph.json
```

## Use Cases

Exported graph data is useful for:

- Integration with other tools
- Custom analysis scripts
- Building documentation generators
- Creating code metrics dashboards

See the [Python SDK](../sdk/overview.md) for more programmatic access patterns.
