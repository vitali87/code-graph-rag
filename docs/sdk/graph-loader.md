---
description: "Load and query exported Code-Graph-RAG knowledge graphs with the Python SDK."
---

# Graph Loader

The `load_graph` function loads exported JSON graph data for programmatic analysis.

## Export a Graph

First, export the knowledge graph to JSON:

```bash
cgr export -o my_graph.json
```

Or export one repository's graph while syncing it:

```bash
cgr start --repo-path /path/to/repo --update-graph -o my_graph.json
```

## Load and Query

```python
from cgr import load_graph

graph = load_graph("my_graph.json")
```

The file must be a JSON object with a `nodes` array (each node has `node_id`,
`labels` and `properties`) and a `relationships` array (each has `from_id`,
`to_id`, `type` and `properties`), the shape `cgr export` writes. `metadata` is
optional: a hand-built or filtered graph without it loads, and its summary
reports `exported_at` as `unknown`. A file of any other shape raises
`GraphFileFormatError` (a `ValueError`) naming what is missing.

### Summary Statistics

```python
summary = graph.summary()
print(f"Total nodes: {summary['total_nodes']}")
print(f"Total relationships: {summary['total_relationships']}")
```

### Find Nodes by Label

```python
functions = graph.find_nodes_by_label("Function")
classes = graph.find_nodes_by_label("Class")
modules = graph.find_nodes_by_label("Module")
```

### Analyse Relationships

```python
for func in functions[:5]:
    relationships = graph.get_relationships_for_node(func.node_id)
    print(f"Function {func.properties['name']} has {len(relationships)} relationships")
```

## Query Memgraph Directly

For live queries against a running Memgraph instance:

```python
from cgr import MemgraphIngestor

with MemgraphIngestor(host="localhost", port=7687) as db:
    rows = db.fetch_all("MATCH (f:Function) RETURN f.name LIMIT 10")
    for row in rows:
        print(row)
```

## Use Cases

- Integration with other tools
- Custom analysis scripts
- Building documentation generators
- Creating code metrics dashboards
