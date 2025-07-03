#!/usr/bin/env python3
"""
Example script demonstrating how to export and load graph data programmatically.

This script shows:
1. How to export a graph during repo analysis
2. How to load and work with exported graph data
3. How to perform basic queries on the exported data
"""

import sys
from pathlib import Path

# Add the parent directory to Python path so we can import codebase_rag
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebase_rag.graph_loader import load_graph


def analyze_graph(graph_file: str) -> None:
    """Analyze the exported graph and show useful information."""
    print(f"\n🔍 Analyzing graph from: {graph_file}")
    print("=" * 60)

    try:
        # Load the graph
        graph = load_graph(graph_file)

        # Get summary
        summary = graph.summary()
        print("\n📊 Graph Summary:")
        print(f"   • Total nodes: {summary['total_nodes']:,}")
        print(f"   • Total relationships: {summary['total_relationships']:,}")
        print(f"   • Exported at: {summary['metadata']['exported_at']}")

        # Show node types
        print("\n🏷️  Node Types:")
        for label, count in summary['node_labels'].items():
            print(f"   • {label}: {count:,} nodes")

        # Show relationship types
        print("\n🔗 Relationship Types:")
        for rel_type, count in summary['relationship_types'].items():
            print(f"   • {rel_type}: {count:,} relationships")

        # Find specific types of nodes
        print("\n🔍 Example Queries:")

        # Find all functions
        functions = graph.find_nodes_by_label("Function")
        print(f"   • Found {len(functions)} function nodes")

        # Find all classes
        classes = graph.find_nodes_by_label("Class")
        print(f"   • Found {len(classes)} class nodes")

        # Show some example function names (first 5)
        if functions:
            print("\n   📝 Example function names:")
            for func in functions[:5]:
                name = func.properties.get('name', 'Unknown')
                print(f"      - {name}")
            if len(functions) > 5:
                print(f"      ... and {len(functions) - 5} more")

        # Show some example class names (first 5)
        if classes:
            print("\n   📝 Example class names:")
            for cls in classes[:5]:
                name = cls.properties.get('name', 'Unknown')
                print(f"      - {name}")
            if len(classes) > 5:
                print(f"      ... and {len(classes) - 5} more")

        # Show relationship analysis for a random node
        if functions:
            example_func = functions[0]
            relationships = graph.get_relationships_for_node(example_func.node_id)
            print(f"\n🔗 Relationships for function '{example_func.properties.get('name', 'Unknown')}':")
            print(f"   • Total relationships: {len(relationships)}")

            outgoing = graph.get_outgoing_relationships(example_func.node_id)
            incoming = graph.get_incoming_relationships(example_func.node_id)
            print(f"   • Outgoing: {len(outgoing)}")
            print(f"   • Incoming: {len(incoming)}")

            if outgoing:
                print("   • Example outgoing relationship types:")
                rel_types = {rel.type for rel in outgoing[:3]}
                for rel_type in rel_types:
                    print(f"     - {rel_type}")

        print("\n✅ Analysis complete!")

    except Exception as e:
        print(f"❌ Error analyzing graph: {e}")
        sys.exit(1)


def main():
    """Main function to demonstrate graph analysis."""
    if len(sys.argv) != 2:
        print("Usage: python graph_export_example.py <exported_graph.json>")
        print("\nTo create an exported graph file, run:")
        print("python -m codebase_rag.main start --repo-path /path/to/repo --update-graph -o graph.json")
        print("\nOr export an existing graph:")
        print("python -m codebase_rag.main export -o graph.json")
        sys.exit(1)

    graph_file = sys.argv[1]

    if not Path(graph_file).exists():
        print(f"❌ Graph file not found: {graph_file}")
        sys.exit(1)

    analyze_graph(graph_file)


if __name__ == "__main__":
    main()
