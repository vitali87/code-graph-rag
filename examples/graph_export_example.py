#!/usr/bin/env python3
"""
Example script demonstrating how to export and load graph data programmatically.

This script shows:
1. How to export a graph during repo analysis
2. How to load and work with exported graph data
3. How to perform basic queries on the exported data
"""

import argparse
import sys
from pathlib import Path

# Add the parent directory to Python path so we can import codebase_rag
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebase_rag.graph_loader import GraphLoader, load_graph


def print_summary(summary: dict) -> None:
    """Prints the high-level summary of the graph."""
    print("\n📊 Graph Summary:")
    print(f"   • Total nodes: {summary.get('total_nodes', 0):,}")
    print(f"   • Total relationships: {summary.get('total_relationships', 0):,}")
    if "metadata" in summary and "exported_at" in summary["metadata"]:
        print(f"   • Exported at: {summary['metadata']['exported_at']}")


def print_node_and_relationship_types(summary: dict) -> None:
    """Prints the breakdown of node and relationship labels."""
    print("\n🏷️  Node Types:")
    for label, count in summary.get("node_labels", {}).items():
        print(f"   • {label}: {count:,} nodes")

    print("\n🔗 Relationship Types:")
    for rel_type, count in summary.get("relationship_types", {}).items():
        print(f"   • {rel_type}: {count:,} relationships")


def print_example_nodes(graph: GraphLoader, node_label: str, limit: int = 5) -> None:
    """Finds and prints a sample of nodes for a given label."""
    nodes = graph.find_nodes_by_label(node_label)
    print(f"\n🔍 Found {len(nodes)} '{node_label}' nodes.")

    if nodes:
        print(f"   📝 Example {node_label} names:")
        for node in nodes[:limit]:
            name = node.properties.get("name", "Unknown")
            print(f"      - {name}")
        if len(nodes) > limit:
            print(f"      ... and {len(nodes) - limit} more")


def analyze_graph(graph_file: str) -> None:
    """Analyze the exported graph and show useful information."""
    print(f"\n🔍 Analyzing graph from: {graph_file}")
    print("=" * 60)

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        print_summary(summary)
        print_node_and_relationship_types(summary)

        print_example_nodes(graph, "Function")
        print_example_nodes(graph, "Class")

        print("\n✅ Analysis complete!")

    except Exception as e:
        print(f"❌ Error analyzing graph: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Main function to demonstrate graph analysis."""
    parser = argparse.ArgumentParser(
        description="Analyze an exported codebase graph.",
        epilog="""
To create an exported graph file, run:
  python -m codebase_rag.main start --repo-path /path/to/repo --update-graph -o graph.json
Or to export an existing graph:
  python -m codebase_rag.main export -o graph.json
""",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "graph_file", type=str, help="Path to the exported_graph.json file."
    )

    args = parser.parse_args()

    graph_path = Path(args.graph_file)
    if not graph_path.exists():
        print(f"❌ Graph file not found: {graph_path}", file=sys.stderr)
        sys.exit(1)

    analyze_graph(str(graph_path))


if __name__ == "__main__":
    main()
