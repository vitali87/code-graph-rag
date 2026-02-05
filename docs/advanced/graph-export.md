# Graph Export and Analysis

Export your knowledge graph data for programmatic analysis, integration with other tools, or custom processing.

## Quick Export

### Export During Graph Update

```bash
graph-code start --repo-path /path/to/repo --update-graph --clean -o my_graph.json
```

### Export Existing Graph

```bash
graph-code export -o my_graph.json
```

### With Custom Batch Size

```bash
graph-code export -o my_graph.json --batch-size 5000
```

---

## Working with Exported Data

### Load and Analyze

```python
from codebase_rag.graph_loader import load_graph

# Load the exported graph
graph = load_graph("my_graph.json")

# Get summary statistics
summary = graph.summary()
print(f"Total nodes: {summary['total_nodes']}")
print(f"Total relationships: {summary['total_relationships']}")

# Find specific node types
functions = graph.find_nodes_by_label("Function")
classes = graph.find_nodes_by_label("Class")

# Analyze relationships
for func in functions[:5]:
    relationships = graph.get_relationships_for_node(func.node_id)
    print(f"Function {func.properties['name']} has {len(relationships)} relationships")
```

### Example Analysis Script

```bash
python examples/graph_export_example.py my_graph.json
```

---

## Use Cases

### Integration with Other Tools
- Custom visualization tools
- Documentation generators
- Code metrics dashboards
- Static analysis tools

### Custom Analysis
- Dependency analysis
- Code complexity metrics
- Architecture validation
- Technical debt assessment

### CI/CD Integration
- Track codebase evolution over time
- Generate architecture diagrams
- Validate architectural constraints
- Monitor code quality metrics

---

## Export Format

The exported JSON contains:
- All nodes with properties (functions, classes, modules, files)
- All relationships (calls, imports, contains, inherits)
- Metadata (timestamps, labels, properties)

```json
{
  "nodes": [
    {
      "id": 123,
      "labels": ["Function"],
      "properties": {
        "name": "process_payment",
        "file_path": "src/payment.py",
        "line_number": 42
      }
    }
  ],
  "relationships": [
    {
      "start_node": 123,
      "end_node": 456,
      "type": "CALLS",
      "properties": {}
    }
  ]
}
```

---

## Related Documentation

- **[Basic Usage](../usage/basic-usage.md)** - Getting started
- **[Real-Time Updates](./real-time-updates.md)** - Auto-sync graph
- **[Graph Schema](../architecture/graph-schema.md)** - Understanding the graph structure
