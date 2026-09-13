"""The per-definition content hash a Gloss is graded against (issue #1808).

The issue's table is the contract: reformatting and comments leave the hash
alone; a local rename, a literal, a logic or a signature change flip it. The
existing `ast_fingerprint` is the clone-detection skeleton and cannot tell a
rename or a changed constant, which is why this hash exists beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.anchor_hash import anchor_hash, anchor_hash_props
from codebase_rag.parsers.ast_fingerprint import compute_ast_fingerprint
from evals.cgr_graph import _StatefulIngestor

BASE = '''def run(items, limit=10):
    """Return the first items."""
    total = 0
    for item in items:  # accumulate
        total += item
    return total
'''
REFORMATTED = '''def run(items,   limit = 10):
    """Return the first items."""
    total=0
    for item in items:
        total   +=   item

    return total
'''
COMMENT_CHANGED = BASE.replace("# accumulate", "# sum them up")
RENAMED_LOCAL = BASE.replace("total", "acc")
LITERAL_CHANGED = BASE.replace("total = 0", "total = 1")
LOGIC_CHANGED = BASE.replace("total += item", "total -= item")
SIGNATURE_CHANGED = BASE.replace("limit=10", "limit=20")
DOCSTRING_CHANGED = BASE.replace("first items", "first few items")
NESTING_CHANGED = BASE.replace(
    "        total += item\n    return total", "    total += item\n    return total"
)


def _python_definition(source: str):
    parsers, _queries = load_parsers()
    python = next((p for k, p in parsers.items() if str(k) == "python"), None)
    if python is None:
        pytest.skip("python parser not available")
    tree = python.parse(source.encode("utf-8"))
    node = tree.root_node.children[0]
    assert node.type == "function_definition", node.type
    return node


def _hash(source: str) -> str:
    return anchor_hash(_python_definition(source))


def test_the_hash_is_a_stable_hex_digest() -> None:
    first, second = _hash(BASE), _hash(BASE)
    assert first == second
    assert len(first) == 64
    int(first, 16)


@pytest.mark.parametrize(
    "variant", [REFORMATTED, COMMENT_CHANGED], ids=["reformatted", "comment"]
)
def test_formatting_and_comments_do_not_change_it(variant: str) -> None:
    assert _hash(variant) == _hash(BASE)


@pytest.mark.parametrize(
    "variant",
    [
        RENAMED_LOCAL,
        LITERAL_CHANGED,
        LOGIC_CHANGED,
        SIGNATURE_CHANGED,
        DOCSTRING_CHANGED,
        NESTING_CHANGED,
    ],
    ids=["local-rename", "literal", "logic", "signature", "docstring", "nesting"],
)
def test_meaningful_edits_change_it(variant: str) -> None:
    assert _hash(variant) != _hash(BASE)


INSIDE_BLOCK = "def f(a):\n    if a:\n        x()\n        y()\n"
AFTER_BLOCK = "def f(a):\n    if a:\n        x()\n    y()\n"


def test_moving_a_statement_across_a_block_boundary_changes_it() -> None:
    # Same nodes in the same pre-order (if, a, block, x(), y()); only where
    # the block CLOSES differs. A hash without nesting markers cannot see it.
    assert _hash(INSIDE_BLOCK) != _hash(AFTER_BLOCK)


def test_it_sees_what_the_clone_skeleton_cannot() -> None:
    # The control that says why this hash exists: the skeleton is identical
    # across a local rename and a changed constant, this hash is not.
    for variant in (RENAMED_LOCAL, LITERAL_CHANGED):
        base_fp = compute_ast_fingerprint(_python_definition(BASE))
        variant_fp = compute_ast_fingerprint(_python_definition(variant))
        assert base_fp is not None and variant_fp is not None
        assert base_fp.fingerprint == variant_fp.fingerprint
        assert _hash(variant) != _hash(BASE)


def test_props_carry_the_hash_under_the_shared_key() -> None:
    props = anchor_hash_props(_python_definition(BASE))
    assert set(props) == {cs.KEY_ANCHOR_HASH}
    assert props[cs.KEY_ANCHOR_HASH] == _hash(BASE)


def test_indexed_functions_and_methods_carry_the_hash(tmp_path: Path) -> None:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    (tmp_path / "mod.py").write_text(
        BASE + "\n\nclass Store:\n    def get(self, key):\n        return key\n",
        encoding="utf-8",
    )
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=True)
    store.flush_all()
    by_qn = {
        props.get(cs.KEY_QUALIFIED_NAME): (label, props)
        for (label, _uid), props in store.nodes.items()
    }
    label, run_props = by_qn["proj.mod.run"]
    assert label == "Function"
    assert run_props[cs.KEY_ANCHOR_HASH] == _hash(BASE)
    label, get_props = by_qn["proj.mod.Store.get"]
    assert label == "Method"
    assert len(get_props[cs.KEY_ANCHOR_HASH]) == 64
    # The class itself is not hashed in this stage: a note on it is not graded.
    _label, class_props = by_qn["proj.mod.Store"]
    assert cs.KEY_ANCHOR_HASH not in class_props
