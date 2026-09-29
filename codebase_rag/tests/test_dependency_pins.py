"""Pins that are supply-chain decisions rather than version preferences."""

import tomllib
from pathlib import Path

_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


def _treesitter_full() -> list[str]:
    config = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return config["project"]["optional-dependencies"]["treesitter-full"]


def test_tree_sitter_dart_is_pinned_exactly() -> None:
    # PyPI's `tree-sitter-dart` is one person's repackaging of a fork, not a
    # release from the grammar's maintainers. 0.1.0 matches upstream
    # UserNobody14/tree-sitter-dart at a9bdfa3 byte for byte; a range would
    # accept whatever that account publishes next on the following relock.
    assert "tree-sitter-dart==0.1.0" in _treesitter_full()
