#!/usr/bin/env python3

import sys
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebase_rag import cli_help as ch
from codebase_rag import logs
from codebase_rag.constants import (
    DEFAULT_NAME,
    KEY_EXPORTED_AT,
    KEY_METADATA,
    KEY_NAME,
    KEY_NODE_LABELS,
    KEY_RELATIONSHIP_TYPES,
    KEY_TOTAL_NODES,
    KEY_TOTAL_RELATIONSHIPS,
    NodeLabel,
)
from codebase_rag.graph_loader import GraphLoader, load_graph
from codebase_rag.types_defs import GraphSummary


def log_summary(summary: GraphSummary) -> None:
    logger.info(logs.GRAPH_SUMMARY)
    logger.info(logs.GRAPH_TOTAL_NODES.format(count=summary.get(KEY_TOTAL_NODES, 0)))
    logger.info(
        logs.GRAPH_TOTAL_RELS.format(count=summary.get(KEY_TOTAL_RELATIONSHIPS, 0))
    )
    if KEY_METADATA in summary and KEY_EXPORTED_AT in summary[KEY_METADATA]:
        logger.info(
            logs.GRAPH_EXPORTED_AT.format(
                timestamp=summary[KEY_METADATA][KEY_EXPORTED_AT]
            )
        )


def log_node_and_relationship_types(summary: GraphSummary) -> None:
    logger.info(logs.GRAPH_NODE_TYPES)
    for label, count in summary.get(KEY_NODE_LABELS, {}).items():
        logger.info(logs.GRAPH_NODE_COUNT.format(label=label, count=count))

    logger.info(logs.GRAPH_REL_TYPES)
    for rel_type, count in summary.get(KEY_RELATIONSHIP_TYPES, {}).items():
        logger.info(logs.GRAPH_REL_COUNT.format(rel_type=rel_type, count=count))


def log_example_nodes(graph: GraphLoader, node_label: str, limit: int = 5) -> None:
    nodes = graph.find_nodes_by_label(node_label)
    logger.info(logs.GRAPH_FOUND_NODES.format(count=len(nodes), label=node_label))

    if nodes:
        logger.info(logs.GRAPH_EXAMPLE_NAMES.format(label=node_label))
        for node in nodes[:limit]:
            name = node.properties.get(KEY_NAME, DEFAULT_NAME)
            logger.info(logs.GRAPH_EXAMPLE_NAME.format(name=name))
        if len(nodes) > limit:
            logger.info(logs.GRAPH_MORE_NODES.format(count=len(nodes) - limit))


def analyze_graph(graph_file: str) -> None:
    logger.info(logs.GRAPH_ANALYZING.format(path=graph_file))

    try:
        _perform_graph_analysis(graph_file)
    except Exception as e:
        logger.error(logs.GRAPH_ANALYSIS_ERROR.format(error=e))
        sys.exit(1)


def _perform_graph_analysis(graph_file: str) -> None:
    graph = load_graph(graph_file)
    summary = graph.summary()

    log_summary(summary)
    log_node_and_relationship_types(summary)

    log_example_nodes(graph, NodeLabel.FUNCTION)
    log_example_nodes(graph, NodeLabel.CLASS)

    logger.success(logs.GRAPH_ANALYSIS_COMPLETE)


def main(
    graph_file: Annotated[Path, typer.Argument(help=ch.HELP_EXPORTED_GRAPH_FILE)],
) -> None:
    if not graph_file.exists():
        logger.error(logs.GRAPH_FILE_NOT_FOUND.format(path=graph_file))
        raise typer.Exit(1)

    analyze_graph(str(graph_file))


if __name__ == "__main__":
    typer.run(main)
