# (H) Indexing the same repo twice must produce identical batch sequences.
# (H) Replaces the digest-baseline CI artifacts proposed in #522/#646: rather
# (H) than comparing a stored hash across runs, index the same fixture twice
# (H) in-process and require byte-identical node and relationship output.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

PY_MAIN = """
from helpers import shout

class Greeter:
    def greet(self, name):
        return shout(name)

def main():
    return Greeter().greet("world")
"""

PY_HELPERS = """
def shout(text):
    return text.upper()

HANDLERS = {"shout": shout}
"""

JS_INDEX = """
import { render } from "./render.js";

export class App {
    start() {
        return render("app");
    }
}

export function boot() {
    return new App().start();
}
"""

JS_RENDER = """
export function render(target) {
    return target.length;
}
"""

RS_LIB = """
pub struct Engine {
    pub name: String,
}

impl Engine {
    pub fn run(&self) -> usize {
        helper(&self.name)
    }
}

pub fn helper(name: &str) -> usize {
    name.len()
}
"""

JAVA_MAIN = """
public class Main {
    public static void main(String[] args) {
        Worker worker = new Worker();
        worker.work();
    }
}

class Worker {
    void work() {
    }
}
"""


def _write_fixture(repo: Path) -> None:
    (repo / "main.py").write_text(PY_MAIN)
    (repo / "helpers.py").write_text(PY_HELPERS)
    (repo / "index.js").write_text(JS_INDEX)
    (repo / "render.js").write_text(JS_RENDER)
    (repo / "lib.rs").write_text(RS_LIB)
    (repo / "Main.java").write_text(JAVA_MAIN)


def _index_from_scratch(repo_path: Path, ingestor: MagicMock) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run(force=True)


def _recorded_batches(ingestor: MagicMock) -> tuple[list, list]:
    nodes = [
        (str(c.args[0]), c.args[1]) for c in ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        (c.args[0], str(c.args[1]), c.args[2], *c.args[3:])
        for c in ingestor.ensure_relationship_batch.call_args_list
    ]
    return nodes, rels


@pytest.fixture
def second_ingestor() -> _MockIngestor:
    return _MockIngestor()


def test_indexing_twice_is_deterministic(
    temp_repo: Path, mock_ingestor: MagicMock, second_ingestor: MagicMock
) -> None:
    _write_fixture(temp_repo)

    _index_from_scratch(temp_repo, mock_ingestor)
    _index_from_scratch(temp_repo, second_ingestor)

    first_nodes, first_rels = _recorded_batches(mock_ingestor)
    second_nodes, second_rels = _recorded_batches(second_ingestor)

    assert first_nodes, "fixture produced no nodes"
    assert first_rels, "fixture produced no relationships"
    assert first_nodes == second_nodes
    assert first_rels == second_rels
