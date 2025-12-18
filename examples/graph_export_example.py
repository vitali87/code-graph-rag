#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebase_rag.graph_loader import GraphLoader, load_graph


def print_summary(summary: dict) -> None:
    logger.info("Graph Summary:")
    logger.info(f"   Total nodes: {summary.get('total_nodes', 0):,}")
    logger.info(f"   Total relationships: {summary.get('total_relationships', 0):,}")
    if "metadata" in summary and "exported_at" in summary["metadata"]:
        logger.info(f"   Exported at: {summary['metadata']['exported_at']}")


def print_node_and_relationship_types(summary: dict) -> None:
    logger.info("Node Types:")
    for label, count in summary.get("node_labels", {}).items():
        logger.info(f"   {label}: {count:,} nodes")

    logger.info("Relationship Types:")
    for rel_type, count in summary.get("relationship_types", {}).items():
        logger.info(f"   {rel_type}: {count:,} relationships")


def print_example_nodes(graph: GraphLoader, node_label: str, limit: int = 5) -> None:
    nodes = graph.find_nodes_by_label(node_label)
    logger.info(f"Found {len(nodes)} '{node_label}' nodes.")

    if nodes:
        logger.info(f"   Example {node_label} names:")
        for node in nodes[:limit]:
            name = node.properties.get("name", "Unknown")
            logger.info(f"      - {name}")
        if len(nodes) > limit:
            logger.info(f"      ... and {len(nodes) - limit} more")


def analyze_graph(graph_file: str) -> None:
    logger.info(f"Analyzing graph from: {graph_file}")

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        print_summary(summary)
        print_node_and_relationship_types(summary)

        print_example_nodes(graph, "Function")
        print_example_nodes(graph, "Class")

        logger.success("Analysis complete!")

    except Exception as e:
        logger.error(f"Error analyzing graph: {e}")
        sys.exit(1)


EPILOG = """
To create an exported graph file, run:
  cgr start --repo-path /path/to/repo --update-graph -o graph.json
Or to export an existing graph:
  cgr export -o graph.json
"""


def main(
    graph_file: Annotated[
        Path, typer.Argument(help="Path to the exported_graph.json file.")
    ],
) -> None:
    if not graph_file.exists():
        logger.error(f"Graph file not found: {graph_file}")
        raise typer.Exit(1)

    analyze_graph(str(graph_file))


if __name__ == "__main__":
    typer.run(main)
