# Document tier (issue #1426). Markdown files are parsed for heading
# structure, emitting a Section per heading nested by heading level via
# CONTAINS_SECTION, plus the Module node the other tiers emit. These tests
# index real .md files end to end and assert those nodes/edges land.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

pytest.importorskip(
    "tree_sitter_markdown",
    reason="markdown grammar ships in the treesitter-full extra",
)

SECTION = cs.NodeLabel.SECTION.value
MODULE = cs.NodeLabel.MODULE.value
FILE = cs.NodeLabel.FILE.value
CONTAINS_SECTION = cs.RelationshipType.CONTAINS_SECTION.value

NESTED = (
    "# Project Plan\n"
    "\n"
    "Intro prose.\n"
    "\n"
    "## Phase One\n"
    "\n"
    "Body.\n"
    "\n"
    "### Subtask A\n"
    "\n"
    "Detail.\n"
    "\n"
    "## Phase Two\n"
    "\n"
    "More.\n"
)


def _run(tmp_path: Path, files: dict[str, str]) -> MagicMock:
    parsers, queries = load_parsers()
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return mock


def _nodes(mock: MagicMock, label: str) -> list[dict]:
    return [
        c.args[1]
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == label
    ]


def _prop_values(mock: MagicMock, label: str, key: str) -> set[str]:
    """Every value of one property across the emitted nodes of a label.

    A node missing the key would silently become None in the set, which reads
    as a passing assertion about a node that was never emitted properly.
    """
    values = set()
    for props in _nodes(mock, label):
        assert key in props, f"{label} node missing {key!r}: {props}"
        values.add(str(props[key]))
    return values


def _node_names(mock: MagicMock, label: str) -> set[str]:
    return _prop_values(mock, label, cs.KEY_NAME)


def _qns(mock: MagicMock, label: str) -> set[str]:
    return _prop_values(mock, label, cs.KEY_QUALIFIED_NAME)


def _rels(mock: MagicMock, rel_type: str) -> set[tuple[str, str]]:
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == rel_type
    }


def _module_qn(tmp_path: Path, rel: str) -> str:
    """The Module qn for a document, whose suffix is part of its identity."""
    stem, _, suffix = rel.rpartition(".")
    parts = f"{stem}_{suffix}".split("/")
    return ".".join([tmp_path.name, *parts])


def _section_by_name(mock: MagicMock, name: str) -> dict:
    matches = [p for p in _nodes(mock, SECTION) if p.get(cs.KEY_NAME) == name]
    assert matches, f"no Section named {name!r}"
    return matches[0]


class TestHeadingExtraction:
    def test_every_heading_becomes_a_section(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        assert _node_names(mock, SECTION) == {
            "Project Plan",
            "Phase One",
            "Subtask A",
            "Phase Two",
        }

    def test_heading_level_recorded(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        levels = {
            p[cs.KEY_NAME]: p[cs.KEY_HEADING_LEVEL] for p in _nodes(mock, SECTION)
        }
        assert levels == {
            "Project Plan": 1,
            "Phase One": 2,
            "Subtask A": 3,
            "Phase Two": 2,
        }

    def test_line_span_is_one_based(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        # "# Project Plan" is the first line of the file.
        assert _section_by_name(mock, "Project Plan")[cs.KEY_START_LINE] == 1
        # "## Phase One" is the fifth.
        assert _section_by_name(mock, "Phase One")[cs.KEY_START_LINE] == 5

    def test_section_span_covers_the_prose_beneath_the_heading(
        self, tmp_path: Path
    ) -> None:
        # The heading node's own end line would report a 1-2 line span for
        # every section, losing the body the section actually owns.
        doc = "# Top\n\nline3\nline4\n\n## Child\n\nline8\nline9\n"
        mock = _run(tmp_path, {"span.md": doc})
        child = _section_by_name(mock, "Child")
        assert (child[cs.KEY_START_LINE], child[cs.KEY_END_LINE]) == (6, 9)

    def test_section_closes_before_the_next_sibling(self, tmp_path: Path) -> None:
        doc = "# Top\n\n## A\n\na-body\n\n## B\n\nb-body\n"
        mock = _run(tmp_path, {"sib.md": doc})
        first = _section_by_name(mock, "A")
        # "## B" starts on line 7, so A owns through line 6.
        assert (first[cs.KEY_START_LINE], first[cs.KEY_END_LINE]) == (3, 6)

    def test_parent_span_contains_its_subsections(self, tmp_path: Path) -> None:
        # A deeper heading is a child, so it stays inside the parent's span
        # rather than closing it.
        doc = "# Top\n\n## A\n\na-body\n\n## B\n\nb-body\n"
        mock = _run(tmp_path, {"sib.md": doc})
        top = _section_by_name(mock, "Top")
        assert (top[cs.KEY_START_LINE], top[cs.KEY_END_LINE]) == (1, 9)

    def test_final_section_runs_to_end_of_file(self, tmp_path: Path) -> None:
        # A trailing newline ends the last line; it must not invent one more.
        mock = _run(tmp_path, {"eof.md": "# Only\n\nbody\n"})
        only = _section_by_name(mock, "Only")
        assert only[cs.KEY_END_LINE] == 3

    def test_prose_before_any_heading_emits_no_section(self, tmp_path: Path) -> None:
        # The grammar wraps a preamble in a headingless `section`; emitting it
        # would put a nameless node in the graph.
        mock = _run(tmp_path, {"doc.md": "Loose intro text.\n\n# Real Heading\n"})
        assert _node_names(mock, SECTION) == {"Real Heading"}

    def test_document_with_no_headings_emits_no_sections(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"notes.md": "Just prose.\n\nAnd more prose.\n"})
        assert _nodes(mock, SECTION) == []


class TestNesting:
    def test_subsection_is_contained_by_its_parent_heading(
        self, tmp_path: Path
    ) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        edges = _rels(mock, CONTAINS_SECTION)
        top = f"{_module_qn(tmp_path, 'plan.md')}.Project Plan"
        assert (top, f"{top}.Phase One") in edges
        assert (f"{top}.Phase One", f"{top}.Phase One.Subtask A") in edges
        assert (top, f"{top}.Phase Two") in edges

    def test_top_level_heading_hangs_off_the_module(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        module_qn = _module_qn(tmp_path, "plan.md")
        assert (module_qn, f"{module_qn}.Project Plan") in _rels(mock, CONTAINS_SECTION)

    def test_sibling_closes_the_deeper_heading(self, tmp_path: Path) -> None:
        # "## Phase Two" must not land under "### Subtask A" that precedes it.
        mock = _run(tmp_path, {"plan.md": NESTED})
        top = f"{_module_qn(tmp_path, 'plan.md')}.Project Plan"
        assert f"{top}.Phase Two" in _qns(mock, SECTION)
        assert f"{top}.Phase One.Subtask A.Phase Two" not in _qns(mock, SECTION)

    def test_skipped_level_nests_under_the_open_heading(self, tmp_path: Path) -> None:
        # h1 -> h3 with no h2: the h3 is a child of the h1, not an orphan.
        mock = _run(tmp_path, {"skip.md": "# One\n\n### Three\n"})
        base = _module_qn(tmp_path, "skip.md")
        assert (f"{base}.One", f"{base}.One.Three") in _rels(mock, CONTAINS_SECTION)


class TestSetextHeadings:
    # Setext headings ("Title" over "====") do NOT produce nested `section`
    # nodes the way ATX headings do, and their text sits one level deeper, in
    # a paragraph. Both traps are silent: naive readers drop these entirely.
    SETEXT = "Title Here\n==========\n\nBody.\n\nSub Here\n--------\n\nMore.\n"

    def test_setext_headings_are_extracted(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"setext.md": self.SETEXT})
        assert _node_names(mock, SECTION) == {"Title Here", "Sub Here"}

    def test_setext_levels(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"setext.md": self.SETEXT})
        assert _section_by_name(mock, "Title Here")[cs.KEY_HEADING_LEVEL] == 1
        assert _section_by_name(mock, "Sub Here")[cs.KEY_HEADING_LEVEL] == 2

    def test_setext_h2_nests_under_h1(self, tmp_path: Path) -> None:
        # The grammar makes these flat siblings; nesting comes from levels.
        mock = _run(tmp_path, {"setext.md": self.SETEXT})
        base = _module_qn(tmp_path, "setext.md")
        assert (f"{base}.Title Here", f"{base}.Title Here.Sub Here") in _rels(
            mock, CONTAINS_SECTION
        )

    def test_mixed_atx_and_setext(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"mixed.md": "Setext Top\n==========\n\n## Atx Child\n"})
        base = _module_qn(tmp_path, "mixed.md")
        assert (f"{base}.Setext Top", f"{base}.Setext Top.Atx Child") in _rels(
            mock, CONTAINS_SECTION
        )


class TestQualifiedNames:
    def test_repeated_sibling_headings_stay_distinct(self, tmp_path: Path) -> None:
        # Two "## Notes" under one parent share a qn, which would merge them
        # into a single graph node.
        mock = _run(
            tmp_path, {"dup.md": "# Top\n\n## Notes\n\ntext\n\n## Notes\n\nmore\n"}
        )
        notes = [p for p in _nodes(mock, SECTION) if p[cs.KEY_NAME] == "Notes"]
        assert len(notes) == 2
        assert len({p[cs.KEY_QUALIFIED_NAME] for p in notes}) == 2

    def test_duplicate_suffix_follows_the_shared_marker_convention(
        self, tmp_path: Path
    ) -> None:
        mock = _run(
            tmp_path, {"dup.md": "# Top\n\n## Notes\n\ntext\n\n## Notes\n\nmore\n"}
        )
        qns = {
            p[cs.KEY_QUALIFIED_NAME]
            for p in _nodes(mock, SECTION)
            if p[cs.KEY_NAME] == "Notes"
        }
        suffixed = [q for q in qns if cs.DUP_QN_MARKER in q]
        assert len(suffixed) == 1
        # Consumers split on the marker and keep the base.
        base, _, line = suffixed[0].partition(cs.DUP_QN_MARKER)
        assert base.endswith("Notes")
        assert line.isdigit()

    def test_dots_in_a_heading_do_not_create_phantom_levels(
        self, tmp_path: Path
    ) -> None:
        mock = _run(tmp_path, {"v.md": "# Release 1.2.3\n"})
        qn = next(iter(_qns(mock, SECTION)))
        assert qn == f"{_module_qn(tmp_path, 'v.md')}.Release 1_2_3"
        # The display name keeps the dots.
        assert _node_names(mock, SECTION) == {"Release 1.2.3"}

    def test_runs_of_whitespace_collapse_in_the_qualified_name(
        self, tmp_path: Path
    ) -> None:
        # Repeated spaces within one heading line. The display name keeps the
        # original spacing; only the qualified name collapses.
        mock = _run(tmp_path, {"w.md": "# Alpha   Beta\n"})
        assert _qns(mock, SECTION) == {f"{_module_qn(tmp_path, 'w.md')}.Alpha Beta"}
        assert _node_names(mock, SECTION) == {"Alpha   Beta"}

    def test_heading_text_spanning_lines_keeps_one_identity(
        self, tmp_path: Path
    ) -> None:
        # A setext heading's text really can span physical lines, which the
        # single-line fixture above does not exercise. The newline must not
        # reach the qualified name, or reflowing a heading would rename it.
        mock = _run(tmp_path, {"r.md": "Alpha\nBeta\n=====\n"})
        assert _qns(mock, SECTION) == {f"{_module_qn(tmp_path, 'r.md')}.Alpha Beta"}
        assert _node_names(mock, SECTION) == {"Alpha\nBeta"}

    def test_heading_with_no_text_gets_a_placeholder_name(self, tmp_path: Path) -> None:
        # A bare "##" has nothing to name a node after; an empty name would
        # produce a qualified name ending in a bare separator.
        mock = _run(tmp_path, {"e.md": "# Top\n\n##\n"})
        assert "(untitled)" in _node_names(mock, SECTION)
        assert f"{_module_qn(tmp_path, 'e.md')}.Top.(untitled)" in _qns(mock, SECTION)

    def test_heading_literally_containing_the_marker_stays_distinct(
        self, tmp_path: Path
    ) -> None:
        # "Notes" is claimed first, then a LITERAL "Notes@9" heading takes
        # the name the third heading would generate: it repeats "Notes" and
        # starts on line 9, so one suffix pass yields the already-owned
        # "Notes@9" and the two sections would merge into one node.
        doc = "# Top\n\n## Notes\n\n## Notes@9\n\nx\n\n## Notes\n"
        mock = _run(tmp_path, {"marker.md": doc})
        qns = _qns(mock, SECTION)
        assert len(qns) == len(_nodes(mock, SECTION)), (
            f"qualified names collided, sections merged: {sorted(qns)}"
        )

    def test_same_name_at_different_depths_does_not_collide(
        self, tmp_path: Path
    ) -> None:
        mock = _run(tmp_path, {"n.md": "# Same\n\n## Same\n"})
        assert len(_qns(mock, SECTION)) == 2


class TestFileHandling:
    def test_markdown_still_gets_its_file_node(self, tmp_path: Path) -> None:
        # Folder containment and orphan pruning rely on the File node, so the
        # document tier must not replace it.
        mock = _run(tmp_path, {"plan.md": NESTED})
        assert "plan.md" in _node_names(mock, FILE)

    def test_module_node_emitted_for_the_document(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"plan.md": NESTED})
        assert _module_qn(tmp_path, "plan.md") in _qns(mock, MODULE)

    def test_markdown_extension_variant_handled(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"doc.markdown": "# Heading\n"})
        assert _node_names(mock, SECTION) == {"Heading"}

    def test_same_stem_with_different_suffixes_stays_separate(
        self, tmp_path: Path
    ) -> None:
        # Both suffixes are handled by this tier, so dropping the extension
        # from the module qn would merge the two files onto one Module node
        # and merge their identically-named sections with it.
        mock = _run(
            tmp_path,
            {"docs/guide.md": "# Alpha\n", "docs/guide.markdown": "# Alpha\n"},
        )
        # Asserts the qualified names each file actually gets, not merely
        # that they differ: a rule that mangled the stem would still produce
        # two distinct names and satisfy a uniqueness-only check.
        by_path = {
            str(p[cs.KEY_PATH]): p[cs.KEY_QUALIFIED_NAME]
            for p in _nodes(mock, MODULE)
            if str(p.get(cs.KEY_PATH, "")).startswith("docs/guide")
        }
        assert by_path == {
            "docs/guide.md": f"{tmp_path.name}.docs.guide_md",
            "docs/guide.markdown": f"{tmp_path.name}.docs.guide_markdown",
        }
        assert _qns(mock, SECTION) == {
            f"{tmp_path.name}.docs.guide_md.Alpha",
            f"{tmp_path.name}.docs.guide_markdown.Alpha",
        }

    def test_nested_directory_paths_recorded(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"docs/guide/plan.md": "# Deep\n"})
        section = _section_by_name(mock, "Deep")
        assert section[cs.KEY_PATH] == "docs/guide/plan.md"

    def test_non_markdown_files_produce_no_sections(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, {"mod.py": "def f():\n    return 1\n"})
        assert _nodes(mock, SECTION) == []


def _export_index(tmp_path: Path, document: str = NESTED):
    """Index a document through the protobuf sink and read the artifact back."""
    import codec.schema_pb2 as pb
    from codebase_rag.services.protobuf_service import ProtobufFileIngestor

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "d.md").write_text(document, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    parsers, queries = load_parsers()
    GraphUpdater(
        ProtobufFileIngestor(str(out), split_index=False),
        project_dir,
        parsers,
        queries,
    ).run()

    index = pb.GraphCodeIndex()
    index.ParseFromString((out / "index.bin").read_bytes())
    return index


def _exported_sections(index) -> dict:
    return {
        node.section.name: node.section
        for node in index.nodes
        if node.WhichOneof("payload") == "section"
    }


class TestProtobufExport:
    # `cgr index --output` serialises through ProtobufFileIngestor, which
    # DROPS any label with no oneof mapping (logging a warning) and writes
    # RELATIONSHIP_TYPE_UNSPECIFIED for an unmapped edge. Without the schema
    # entries the exported index would silently lose the whole hierarchy.
    def test_sections_survive_protobuf_export(self, tmp_path: Path) -> None:
        sections = _exported_sections(_export_index(tmp_path))
        assert set(sections) == {
            "Project Plan",
            "Phase One",
            "Subtask A",
            "Phase Two",
        }

    def test_section_payload_keeps_its_properties(self, tmp_path: Path) -> None:
        sections = _exported_sections(_export_index(tmp_path))
        assert sections["Subtask A"].heading_level == 3
        assert sections["Project Plan"].start_line == 1
        assert sections["Subtask A"].path == "d.md"

    def test_contains_section_edges_are_typed(self, tmp_path: Path) -> None:
        import codec.schema_pb2 as pb

        index = _export_index(tmp_path)
        section_rels = [r for r in index.relationships if r.target_label == SECTION]
        assert section_rels, "no relationships target a Section"
        contains_section = pb.Relationship.RelationshipType.CONTAINS_SECTION
        for rel in section_rels:
            assert rel.type == contains_section, (
                f"expected CONTAINS_SECTION, got {rel.type}: {rel}"
            )


class TestDegradedGrammar:
    def test_handles_is_false_without_the_grammar(self, tmp_path: Path) -> None:
        # A base install has no markdown grammar; indexing must fall through
        # to the generic File node rather than losing the file.
        from codebase_rag.parsers.document_tier import DocumentTier

        tier = DocumentTier(MagicMock(), tmp_path, "proj")
        object.__setattr__(tier, "_parser", None)
        assert tier.handles(".md") is False

    def test_process_file_is_a_noop_without_the_grammar(self, tmp_path: Path) -> None:
        from codebase_rag.parsers.document_tier import DocumentTier

        ingestor = MagicMock()
        tier = DocumentTier(ingestor, tmp_path, "proj")
        object.__setattr__(tier, "_parser", None)
        (tmp_path / "d.md").write_text("# H\n", encoding="utf-8")
        tier.process_file(tmp_path / "d.md", {})
        ingestor.ensure_node_batch.assert_not_called()

    def test_unreadable_file_is_skipped(self, tmp_path: Path) -> None:
        from codebase_rag.parsers.document_tier import DocumentTier

        ingestor = MagicMock()
        tier = DocumentTier(ingestor, tmp_path, "proj")
        tier.process_file(tmp_path / "missing.md", {})
        ingestor.ensure_node_batch.assert_not_called()
