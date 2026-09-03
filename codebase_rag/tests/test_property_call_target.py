# The property-access pass must skip the attribute that is a call's own
# function -- the call path has already resolved it -- while still resolving
# an ordinary read of the same property. The guard compared tree-sitter
# nodes with `is`, and py-tree-sitter builds a fresh wrapper per lookup, so
# it never fired: the pass resolved the call target instead and the plain
# read on the line above was left with no edge at all.
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.conftest import get_relationships, run_updater

# `value` is read on one line and called on the next, so the two sites are
# told apart by position rather than by a total that other passes also feed.
SOURCE = """
class Config:
    @property
    def value(self):
        return lambda: 1

    def go(self):
        x = self.value
        return self.value()
"""
READ_LINE = 8
CALL_LINE = 9


@pytest.fixture
def prop_project(temp_repo: Path) -> Path:
    project = temp_repo / "prop_call_target"
    project.mkdir()
    return project


def _getter_edge_lines(mock_ingestor: MagicMock) -> list[int]:
    return [
        call.kwargs["properties"]["line"]
        for call in get_relationships(mock_ingestor, "CALLS")
        if call.args[2][2].endswith("Config.value")
        and "properties" in call.kwargs
        and "line" in call.kwargs["properties"]
    ]


def test_property_read_beside_a_call_through_the_property(
    prop_project: Path, mock_ingestor: MagicMock
) -> None:
    (prop_project / "m.py").write_text(SOURCE, encoding="utf-8")

    run_updater(prop_project, mock_ingestor)

    lines = _getter_edge_lines(mock_ingestor)
    # The plain read resolves to the getter.
    assert READ_LINE in lines, lines
    # The call site is the call path's to resolve. With the identity
    # comparison the pass claimed it too, and every edge landed on the call
    # line while the read above got none.
    assert lines.count(CALL_LINE) < len(lines), lines
