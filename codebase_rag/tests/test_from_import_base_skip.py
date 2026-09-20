# `from <module> import <name>` must not report the module itself as one of
# the imported names. The base is skipped by comparing it against each
# named child, and that comparison used `is`: py-tree-sitter builds a fresh
# wrapper per lookup, so the node from child_by_field_name is never the
# same object as the one in named_children and the skip never happened.
#
# A dotted base (`pkg.sub`) hid the bug -- the dot filter below the skip
# dropped it anyway -- so it only shows on a dotless module name.
from __future__ import annotations

import pytest

from codebase_rag.parsers.endpoint_prefixes import _from_import_targets

tree_sitter = pytest.importorskip("tree_sitter")
tree_sitter_python = pytest.importorskip("tree_sitter_python")


def _import_node(source: str):
    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))
    return parser.parse(source.encode("utf-8")).root_node.children[0]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from routers import alpha\n", {"alpha": "routers.alpha"}),
        (
            "from routers import alpha, beta\n",
            {"alpha": "routers.alpha", "beta": "routers.beta"},
        ),
        ("from routers import alpha as a\n", {"a": "routers.alpha"}),
        # A dotted base was already excluded by the dot filter.
        ("from pkg.sub import alpha\n", {"alpha": "pkg.sub.alpha"}),
    ],
)
def test_the_import_base_is_not_reported_as_an_imported_name(
    source: str, expected: dict[str, str]
) -> None:
    targets = _from_import_targets("proj.m", _import_node(source))

    # With the identity comparison the dotless cases also carried
    # {"routers": "routers.routers"} -- the base mapped to itself.
    assert targets == expected, targets
