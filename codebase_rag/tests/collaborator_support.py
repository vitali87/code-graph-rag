"""Helpers for the tests that pin one language collaborator directly: a
snippet parsed with the real grammar, the nodes of a type in it, and a small
repository indexed through the real updater."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from evals.recorded_graph import RecordingIngestor


def parse(language: cs.SupportedLanguage, source: str | bytes) -> Node:
    """The root node of `source` parsed by `language`'s grammar."""
    parsers, _ = load_parsers()
    data = source.encode() if isinstance(source, str) else source
    return parsers[language].parse(data).root_node


def walk(root: Node) -> Iterator[Node]:
    """Every node under `root`, `root` first, in document order."""
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def nodes_of(root: Node, node_type: str) -> list[Node]:
    """The nodes of `node_type` under `root`, in document order."""
    return [node for node in walk(root) if node.type == node_type]


def first(root: Node, node_type: str) -> Node:
    """The first node of `node_type` under `root`."""
    found = nodes_of(root, node_type)
    assert found, f"no {node_type} in {root.text!r}"
    return found[0]


def index(
    repo: Path, ingestor: RecordingIngestor, files: Mapping[str, str]
) -> GraphUpdater:
    """`files` written under `repo` and indexed into `ingestor`."""
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return create_and_run_updater(repo, ingestor)


def edges(ingestor: RecordingIngestor, rel_type: str) -> set[tuple[str, str]]:
    """The (source, target) values of the `rel_type` edges recorded."""
    return {
        (str(rel.source), str(rel.target))
        for rel in get_relationships(ingestor, rel_type)
    }
