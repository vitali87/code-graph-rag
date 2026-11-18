"""Issue #2846: a `pub trait`'s methods are exported, so dead-code keeps them.

A trait's methods take no `pub` of their own: they are as visible as the
trait. A method counted as exported only with its own bare `pub`, so a
`pub trait`'s required and default methods, and through them the impls of
that trait, were never dead-code roots and a library's whole trait API was
reported dead (byteorder: 161 methods).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.tests.test_is_exported_roots import _one, _run
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

LIB = """\
pub trait Greeter {
    fn greet(&self) -> String;
    fn greet_loudly(&self) -> String {
        self.greet().to_uppercase()
    }
}

pub struct English;

impl Greeter for English {
    fn greet(&self) -> String {
        "hi".to_string()
    }
}

pub(crate) trait Internal {
    fn tick(&self);
}

trait Hidden {
    fn peek(&self) -> u8 {
        0
    }
}

impl English {
    pub fn name(&self) -> &str {
        "english"
    }

    fn secret(&self) -> u8 {
        1
    }
}

pub fn public_free_fn() -> i32 {
    42
}

fn private_helper() -> i32 {
    7
}
"""


@pytest.fixture
def exported(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"lib.rs": LIB})


@pytest.mark.parametrize("method", [".Greeter.greet", ".Greeter.greet_loudly"])
def test_a_pub_traits_methods_are_exported(
    exported: dict[str, bool], method: str
) -> None:
    assert _one(exported, method) is True


class _Graph:
    """The dead-code queries answered from what the indexer emitted."""

    def __init__(self, graph: RecordedGraph) -> None:
        self._nodes = [
            {
                "label": props[cs.KEY_LABEL],
                "qualified_name": qn,
                "name": props.get(cs.KEY_NAME),
                "path": props.get(cs.KEY_PATH),
                "start_line": props.get(cs.KEY_START_LINE),
                "end_line": props.get(cs.KEY_END_LINE),
                "decorators": props.get(cs.KEY_DECORATORS, []),
                "is_exported": props.get(cs.KEY_IS_EXPORTED, False),
                "overrides_external": props.get(cs.KEY_OVERRIDES_EXTERNAL, False),
            }
            for qn, props in graph.nodes.items()
            if props[cs.KEY_LABEL]
            in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
        ]
        labels = {qn: props[cs.KEY_LABEL] for qn, props in graph.nodes.items()}
        self._rels = [
            {
                "from_label": labels.get(src),
                "from_qn": src,
                "rel_type": rel,
                "to_label": labels.get(dst),
                "to_qn": dst,
            }
            for src, rel, dst, _props in graph.edges
        ]

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._nodes if query == cq.CYPHER_DEAD_CODE_NODES else self._rels


def test_dead_code_reports_only_what_nothing_can_reach(tmp_path: Path) -> None:
    root = tmp_path / "greet"
    _write(root, "lib.rs", LIB)
    graph = _index(root, MagicMock())
    config = default_dead_code_config(include_tests=True, include_classes=False)

    reported = {
        str(row["qualified_name"]).removeprefix(f"{graph.project}.")
        for row in collect_dead_code(_Graph(graph), graph.project, config)
    }

    # The trait's methods are roots and the impl is reached through them;
    # what stays is what no caller outside the crate can name.
    assert not reported & {
        "lib.Greeter.greet",
        "lib.Greeter.greet_loudly",
        "lib.English.greet",
    }
    assert {"lib.private_helper", "lib.English.secret"} <= reported


# Negative: what must not change.


@pytest.mark.parametrize(
    ("method", "is_exported"),
    [
        (".Internal.tick", False),
        (".Hidden.peek", False),
        (".English.name", True),
        (".English.secret", False),
        (".public_free_fn", True),
        (".private_helper", False),
    ],
)
def test_other_rust_visibility_is_unchanged(
    exported: dict[str, bool], method: str, is_exported: bool
) -> None:
    assert _one(exported, method) is is_exported
