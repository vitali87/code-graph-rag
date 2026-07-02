from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _exported_by_qn(mock: MagicMock) -> dict[str, bool]:
    # (H) qualified_name -> is_exported for every Function/Method node ingested.
    out: dict[str, bool] = {}
    for c in mock.ensure_node_batch.call_args_list:
        label, props = c.args[0], c.args[1]
        if label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            out[props[cs.KEY_QUALIFIED_NAME]] = props.get(cs.KEY_IS_EXPORTED, False)
    return out


def _run(tmp_path: Path, files: dict[str, str]) -> dict[str, bool]:
    parsers, queries = load_parsers()
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    mock = MagicMock()
    updater = GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    return _exported_by_qn(mock)


def _one(exported: dict[str, bool], suffix: str) -> bool:
    matches = [qn for qn in exported if qn.endswith(suffix)]
    assert matches, f"no node ending with {suffix!r}; have {sorted(exported)}"
    return exported[matches[0]]


PY_SRC = """\
class SRGExporter:
    def __init__(self):
        self.x = 1

    def to_srg(self):
        return self._predicates()

    def _predicates(self):
        return 1


def parse(s):
    return _extract(s)


def _extract(s):
    return s
"""

GO_SRC = """\
package main

type Msg struct{}

func (m *Msg) Reset() {}

func (m *Msg) internal() {}

func ExportedFunc() int { return unexportedFunc() }

func unexportedFunc() int { return 1 }
"""

TS_SRC = """\
export function useThing() {
    return 1;
}

function localHelper() {
    return 2;
}

export const Widget = () => 3;
"""


def test_python_public_symbols_are_exported(tmp_path: Path) -> None:
    if "python" not in load_parsers()[0]:
        pytest.skip("python parser not available")
    exported = _run(tmp_path, {"srg.py": PY_SRC})
    assert _one(exported, ".to_srg") is True
    assert _one(exported, ".parse") is True
    assert _one(exported, ".__init__") is True  # dunder: runtime-invoked
    assert _one(exported, "._predicates") is False
    assert _one(exported, "._extract") is False


def test_nested_python_function_is_not_a_root(tmp_path: Path) -> None:
    # (H) A function nested inside another function is a local closure, never public
    # (H) API, so it must not be seeded as a reachability root even if its name is
    # (H) public -- otherwise an unreachable outer function's helpers look live.
    src = (
        "def outer():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper()\n"
    )
    exported = _run(tmp_path, {"m.py": src})
    assert _one(exported, ".outer") is True
    assert _one(exported, ".outer.helper") is False


def test_go_capitalized_symbols_are_exported(tmp_path: Path) -> None:
    if "go" not in load_parsers()[0]:
        pytest.skip("go parser not available")
    exported = _run(tmp_path, {"msg.go": GO_SRC})
    assert _one(exported, ".ExportedFunc") is True
    assert _one(exported, ".Reset") is True
    assert _one(exported, ".unexportedFunc") is False
    assert _one(exported, ".internal") is False


def test_ts_export_keyword_marks_exported(tmp_path: Path) -> None:
    if "typescript" not in load_parsers()[0]:
        pytest.skip("typescript parser not available")
    exported = _run(tmp_path, {"widget.ts": TS_SRC})
    assert _one(exported, ".useThing") is True
    assert _one(exported, ".Widget") is True
    assert _one(exported, ".localHelper") is False
