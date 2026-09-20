"""Enum variants become `EnumVariant` nodes under their Enum through
`HAS_VARIANT`, with position, index, the written discriminant value and the
doc comment where the language has one (issue #1807). Opt-in through the
`enum_variants` capture group, as parameters and fields are."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SOURCES: dict[str, str] = {
    "shape.rs": (
        "/// A shape.\npub enum Shape {\n    /// Unit.\n    Circle,\n"
        "    Rect(u32, u32),\n    Named { w: u32 },\n    Fixed = 3,\n}\n"
    ),
    "Colour.java": (
        "enum Colour {\n    /** Red. */\n    RED,\n    GREEN(2);\n"
        "    Colour() {}\n    Colour(int v) {}\n"
        "    enum Inner { X }\n}\n"
    ),
    "color.c": "enum color { RED, GREEN = 2, BLUE };\n",
    "mode.cpp": "enum class Mode : int { Fast, Slow = 5 };\n",
    "Color.cs": "namespace App;\n\npublic enum Color { Red, Green = 2 }\n",
    "Suit.php": "<?php\nenum Suit: string {\n    case Hearts = 'H';\n    case Spades;\n}\n",
}

LABEL = "EnumVariant"
REL = "HAS_VARIANT"


def _index(tmp_path: Path, tokens: list[str]) -> _StatefulIngestor:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    for name, src in SOURCES.items():
        (repo / name).write_text(src, encoding="utf-8")
    parsers, queries = load_parsers()
    missing = [
        lang
        for lang in (
            cs.SupportedLanguage.RUST,
            cs.SupportedLanguage.JAVA,
            cs.SupportedLanguage.C,
            cs.SupportedLanguage.CPP,
            cs.SupportedLanguage.CSHARP,
            cs.SupportedLanguage.PHP,
        )
        if lang not in parsers
    ]
    if missing:
        pytest.skip(f"{missing[0]} parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run(force=True)
    return store


def _variants(store: _StatefulIngestor, enum_name: str) -> dict[str, dict]:
    return {
        str(props[cs.KEY_NAME]): props
        for (label, _uid), props in store.nodes.items()
        if label == LABEL
        and str(props[cs.KEY_QUALIFIED_NAME])
        .rsplit(".", 1)[0]
        .endswith(f".{enum_name}")
    }


def _owner_edges(store: _StatefulIngestor, enum_name: str) -> set[tuple[str, str]]:
    return {
        (str(src), str(tgt))
        for sl, src, rel, _tl, tgt in store.edges
        if rel == REL and str(src).endswith(f".{enum_name}")
    }


def test_the_default_index_emits_no_variant_and_no_edge(tmp_path: Path) -> None:
    store = _index(tmp_path, [])
    assert not [1 for (label, _uid) in store.nodes if label == LABEL]
    assert not [1 for e in store.edges if e[2] == REL]


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("enums"), ["+enum_variants"])


@pytest.mark.parametrize(
    ("enum_name", "names", "values"),
    [
        ("Shape", ["Circle", "Rect", "Named", "Fixed"], {"Fixed": "3"}),
        ("Colour", ["RED", "GREEN"], {}),
        ("color", ["RED", "GREEN", "BLUE"], {"GREEN": "2"}),
        ("Mode", ["Fast", "Slow"], {"Slow": "5"}),
        ("Color", ["Red", "Green"], {"Green": "2"}),
        ("Suit", ["Hearts", "Spades"], {"Hearts": "'H'"}),
    ],
    ids=["rust", "java", "c", "cpp", "csharp", "php"],
)
def test_every_variant_becomes_a_node_in_declaration_order(
    indexed: _StatefulIngestor, enum_name: str, names: list[str], values: dict[str, str]
) -> None:
    variants = _variants(indexed, enum_name)
    assert list(variants) == names, sorted(variants)
    for index, name in enumerate(names):
        props = variants[name]
        assert props[cs.KEY_INDEX] == index
        assert props[cs.KEY_START_LINE] >= 1
        assert props[cs.KEY_PATH]
        assert Path(props[cs.KEY_ABSOLUTE_PATH]).is_absolute()
        if name in values:
            assert props["value"] == values[name], props
        else:
            assert "value" not in props, props
    edges = _owner_edges(indexed, enum_name)
    assert {tgt.rsplit(".", 1)[-1] for _s, tgt in edges} == set(names), edges


def test_a_variant_carries_its_doc_comment(indexed: _StatefulIngestor) -> None:
    assert _variants(indexed, "Shape")["Circle"][cs.KEY_DOCSTRING] == "Unit."
    assert _variants(indexed, "Colour")["RED"][cs.KEY_DOCSTRING] == "Red."
    assert cs.KEY_DOCSTRING not in _variants(indexed, "Shape")["Rect"]


def test_java_enum_body_members_are_not_variants(indexed: _StatefulIngestor) -> None:
    # Constructors are not variants, and a nested enum's variants are its
    # own: only a direct child of the body is a constant of THIS enum.
    assert "Colour" not in _variants(indexed, "Colour")
    assert "X" not in _variants(indexed, "Colour")
    assert list(_variants(indexed, "Inner")) == ["X"]


def test_the_emitter_itself_declines_when_the_relationship_is_off() -> None:
    # The default-index test above passes even with the early return in
    # `emit_declared_variants` deleted, because `filtering.py` drops the
    # nodes and edges independently. That makes it blind to the in-function
    # gate, so assert the gate directly: a disabled HAS_VARIANT must make the
    # emitter return 0 without asking the ingestor to write anything.
    from codebase_rag.parsers.enum_variants import (
        declared_variants,
        emit_declared_variants,
    )

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def rel_enabled(self, _rel: cs.RelationshipType) -> bool:
            return False

        def ensure_node_batch(self, *a: object, **k: object) -> None:
            self.calls.append("node")

        def ensure_relationship_batch(self, *a: object, **k: object) -> None:
            self.calls.append("rel")

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    tree = parsers[cs.SupportedLanguage.RUST].parse(
        b"pub enum Shape { Circle, Fixed = 3 }\n"
    )
    enum_node = next(
        n for n in tree.root_node.named_children if n.type == cs.TS_RS_ENUM_ITEM
    )
    # A real enum with real variants: the ONLY reason to emit nothing is the
    # gate. Proven by the control below, which finds two variants.
    assert len(declared_variants(enum_node, cs.SupportedLanguage.RUST)) == 2

    recorder = _Recorder()
    written = emit_declared_variants(
        recorder,  # type: ignore[arg-type]
        cs.NodeLabel.ENUM,
        "proj.shape.Shape",
        enum_node,
        cs.SupportedLanguage.RUST,
        {},
    )
    assert written == 0
    assert recorder.calls == [], recorder.calls
