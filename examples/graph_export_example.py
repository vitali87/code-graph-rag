#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebase_rag.graph_loader import GraphLoader, load_graph
from codebase_rag.types_defs import GraphSummary

KEY_TOTAL_NODES = "total_nodes"
KEY_TOTAL_RELATIONSHIPS = "total_relationships"
KEY_METADATA = "metadata"
KEY_EXPORTED_AT = "exported_at"
KEY_NODE_LABELS = "node_labels"
KEY_RELATIONSHIP_TYPES = "relationship_types"
KEY_NAME = "name"
DEFAULT_NAME = "Unknown"
NODE_FUNCTION = "Function"
NODE_CLASS = "Class"


def log_summary(summary: GraphSummary) -> None:
    logger.info("Graph Summary:")
    logger.info(f"   Total nodes: {summary.get(KEY_TOTAL_NODES, 0):,}")
    logger.info(f"   Total relationships: {summary.get(KEY_TOTAL_RELATIONSHIPS, 0):,}")
    if KEY_METADATA in summary and KEY_EXPORTED_AT in summary[KEY_METADATA]:
        logger.info(f"   Exported at: {summary[KEY_METADATA][KEY_EXPORTED_AT]}")


def log_node_and_relationship_types(summary: GraphSummary) -> None:
    logger.info("Node Types:")
    for label, count in summary.get(KEY_NODE_LABELS, {}).items():
        logger.info(f"   {label}: {count:,} nodes")

    logger.info("Relationship Types:")
    for rel_type, count in summary.get(KEY_RELATIONSHIP_TYPES, {}).items():
        logger.info(f"   {rel_type}: {count:,} relationships")


def log_example_nodes(graph: GraphLoader, node_label: str, limit: int = 5) -> None:
    nodes = graph.find_nodes_by_label(node_label)
    logger.info(f"Found {len(nodes)} '{node_label}' nodes.")

    if nodes:
        logger.info(f"   Example {node_label} names:")
        for node in nodes[:limit]:
            name = node.properties.get(KEY_NAME, DEFAULT_NAME)
            logger.info(f"      - {name}")
        if len(nodes) > limit:
            logger.info(f"      ... and {len(nodes) - limit} more")


def analyze_graph(graph_file: str) -> None:
    logger.info(f"Analyzing graph from: {graph_file}")

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        log_summary(summary)
        log_node_and_relationship_types(summary)

        log_example_nodes(graph, NODE_FUNCTION)
        log_example_nodes(graph, NODE_CLASS)

        logger.success("Analysis complete!")

    except Exception as e:
        logger.error(f"Error analyzing graph: {e}")
        sys.exit(1)


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
