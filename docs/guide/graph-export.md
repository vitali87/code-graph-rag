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

## Canonical index and provenance manifest

`cgr index` writes a canonical artifact: nodes and relationships are sorted
(node id; then source id, relationship type, target id) and serialized with
deterministic protobuf encoding, and every File/Folder identity is
repo-relative. Two exports of the same source state with the same analyzer
version and capture configuration are byte-identical, wherever the repo is
checked out.

Alongside the artifacts, `manifest.json` records the provenance: the source
commit and a dirty-tree flag (null when the target is not a git repository),
the analyzer version, the sha256 of the codec schema, the capture
configuration, a sha256 per artifact, and a per-language coverage summary
(module counts and `flow_covered` totals) computed from the artifact itself,
so the claims can never drift from the content. The `created_at` timestamp is
metadata only and participates in no hash.

Verify an index against its manifest:

```bash
cgr verify-index -i ./index-dir
```

Verification fails when an artifact is missing or its hash mismatches, when an
artifact is not covered by the manifest, or when the manifest's coverage
summary disagrees with the graph content. In CI, attesting `manifest.json`
(GitHub artifact attestation) extends the chain to a signer identity: the
attestation proves who produced the manifest, and the manifest proves which
artifact bytes and source state it belongs to.
