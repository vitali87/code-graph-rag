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
    assert first.startswith(cs.ANCHOR_HASH_VERSION)
    digest = first.removeprefix(cs.ANCHOR_HASH_VERSION)
    assert len(digest) == 64
    int(digest, 16)


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
        assert base_fp is not None
        assert variant_fp is not None
        assert base_fp.fingerprint == variant_fp.fingerprint
        assert _hash(variant) != _hash(BASE)


def test_removing_a_decorator_changes_it() -> None:
    # A Python decorator is the PARENT of the definition node, so the tree
    # walk alone cannot see it; the extracted names are folded in instead.
    node = _python_definition(BASE)
    decorated = anchor_hash(node, ["property"])
    decorated_again = anchor_hash(node, ["property"])
    plain = anchor_hash(node, [])
    reordered = anchor_hash(node, ["cache", "property"])
    reordered_back = anchor_hash(node, ["property", "cache"])
    assert decorated != plain
    assert decorated == decorated_again
    assert reordered != reordered_back


DART_ONE = "int f() {\n  return 1;\n}\n"
DART_TWO = "int f() {\n  return 2;\n}\n"
DART_ONE_REFORMATTED = "int f() { return 1; }\n"


def _dart_signature(source: str):
    parsers, _queries = load_parsers()
    dart = next((p for k, p in parsers.items() if str(k) == "dart"), None)
    if dart is None:
        pytest.skip("dart parser not available")
    tree = dart.parse(source.encode("utf-8"))
    stack = list(tree.root_node.children)
    while stack:
        node = stack.pop(0)
        if node.type in cs.DART_SIGNATURE_TYPES:
            return node
        stack.extend(node.children)
    raise AssertionError("no Dart signature node found")


def test_a_dart_body_change_is_seen_through_the_sibling_body_node() -> None:
    # Dart's captured definition node is the signature; its body is the next
    # sibling, so walking the node alone would miss every body edit.
    assert anchor_hash(_dart_signature(DART_ONE)) != anchor_hash(
        _dart_signature(DART_TWO)
    )
    assert anchor_hash(_dart_signature(DART_ONE)) == anchor_hash(
        _dart_signature(DART_ONE_REFORMATTED)
    )


def test_props_carry_the_hash_under_the_shared_key() -> None:
    props = anchor_hash_props(_python_definition(BASE))
    assert set(props) == {cs.KEY_ANCHOR_HASH}
    assert props[cs.KEY_ANCHOR_HASH] == _hash(BASE)


def test_an_indexed_decorated_method_hashes_its_decorator(tmp_path: Path) -> None:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    plain = "class Store:\n    def size(self):\n        return 1\n"
    decorated = "class Store:\n    @property\n    def size(self):\n        return 1\n"
    hashes = []
    for source in (plain, decorated):
        root = tmp_path / ("decorated" if source is decorated else "plain")
        root.mkdir()
        (root / "mod.py").write_text(source, encoding="utf-8")
        store = _StatefulIngestor()
        GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        ).run(force=True)
        store.flush_all()
        props = next(
            p
            for (_l, _u), p in store.nodes.items()
            if p.get(cs.KEY_QUALIFIED_NAME) == "proj.mod.Store.size"
        )
        hashes.append(props[cs.KEY_ANCHOR_HASH])
    assert hashes[0] != hashes[1]


class _BufferingStore(_StatefulIngestor):
    """The emulator, but nodes land only at flush (as the real ingestor's
    batching does below its batch size), and the grade statement records
    what it could see when it ran."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: list[tuple[str, dict]] = []
        self.grade_saw: list[tuple[int, str | None]] = []

    def ensure_node_batch(self, label: str, properties: dict) -> None:  # type: ignore[override]
        self.pending.append((label, dict(properties)))

    def flush_all(self) -> None:
        for label, properties in self.pending:
            super().ensure_node_batch(label, properties)
        self.pending.clear()
        super().flush_all()

    def execute_write(self, query: str, params: dict | None = None) -> None:  # type: ignore[override]
        from codebase_rag import cypher_queries as cq

        if query == cq.CYPHER_GRADE_GLOSS_ANCHORS:
            run = next(
                (
                    p
                    for (_l, _u), p in self.nodes.items()
                    if p.get(cs.KEY_QUALIFIED_NAME) == "proj.mod.run"
                ),
                None,
            )
            self.grade_saw.append(
                (len(self.pending), run.get(cs.KEY_ANCHOR_HASH) if run else None)
            )
            return
        super().execute_write(query, params)


def test_a_scoped_reingest_grades_after_the_new_nodes_are_flushed(
    tmp_path: Path,
) -> None:
    # The grade compares a note with its subject's CURRENT hash in the store.
    # Run before the flush, the edited function is still in the buffer and the
    # note on the very file just edited stays EXACT until the following sync.
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    (tmp_path / "mod.py").write_text(BASE, encoding="utf-8")
    store = _BufferingStore()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    store.flush_all()
    before = _hash(BASE)
    (tmp_path / "mod.py").write_text(LOGIC_CHANGED, encoding="utf-8")
    store.grade_saw.clear()
    updater.reingest((tmp_path / "mod.py",))
    assert store.grade_saw, "the scoped reingest must grade the notes"
    pending_at_grade, hash_at_grade = store.grade_saw[-1]
    assert pending_at_grade == 0, "graded while re-parsed nodes were still buffered"
    assert hash_at_grade == _hash(LOGIC_CHANGED)
    assert hash_at_grade != before


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
    assert get_props[cs.KEY_ANCHOR_HASH].startswith(cs.ANCHOR_HASH_VERSION)
    # The class itself is not hashed in this stage: a note on it is not graded.
    _label, class_props = by_qn["proj.mod.Store"]
    assert cs.KEY_ANCHOR_HASH not in class_props
