# Markdown front-matter as declared metadata (issue #1448, metadata bullet).
#
# #1448 lists six remaining criteria, five of which need design decisions that
# have not been made -- where an inferred purpose comes from, what "related"
# means for synchronisation, whether the agent may edit files unprompted.
#
# Metadata tagging is the exception, and the reason it is separable is worth
# stating: front-matter is DECLARED, not inferred, so "inference can be
# silently wrong" does not apply; and reading it writes nothing, so the
# unprompted-edit question does not arise.
#
# The grammar already exposes it. `tree-sitter-markdown` parses
#
#     ---
#     purpose: planning
#     ---
#
# to a `minus_metadata` node, which `document_tier` currently ignores.
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.parsers.document_tier import parse_front_matter


class TestParsing:
    """What the parser accepts and, more importantly, what it refuses."""

    def test_simple_scalar_keys(self) -> None:
        assert parse_front_matter(
            "---\npurpose: planning\nscope: service-X\n---\n"
        ) == {"purpose": "planning", "scope": "service-X"}

    def test_values_keep_internal_colons(self) -> None:
        """`url: https://x/y` must not split at the second colon.

        Splitting on every colon would truncate the value to `https`, which is
        wrong in a way that looks plausible -- the key is right and the value
        is a real string, so nothing downstream reports it.
        """
        assert parse_front_matter("---\nurl: https://example.com/a\n---\n") == {
            "url": "https://example.com/a"
        }

    def test_quotes_are_stripped_from_values(self) -> None:
        assert parse_front_matter("---\ntitle: \"My Plan\"\nk: 'v'\n---\n") == {
            "title": "My Plan",
            "k": "v",
        }

    def test_a_document_without_front_matter_yields_nothing(self) -> None:
        """The common case: most Markdown has no front-matter at all.

        Returning an empty mapping rather than raising, because absence is
        normal here and an exception would make every ordinary document a
        parse failure.
        """
        assert parse_front_matter("# Title\n\nBody.\n") == {}

    def test_a_delimiter_line_alone_is_not_front_matter(self) -> None:
        """`---` as a horizontal rule or setext underline must not open a block.

        A setext heading underlines with `---`, so a document beginning with a
        title in that style would otherwise have its body swallowed as
        metadata.
        """
        assert parse_front_matter("Title\n---\n\nBody.\n") == {}

    def test_a_fenced_block_below_the_first_line_is_not_front_matter(self) -> None:
        """Front-matter must open on line 1; a later fenced pair is prose.

        Found by mutation, in two rounds. Removing the `lines[0]` check left
        all tests passing; my first replacement fixture ALSO left it passing,
        because its pairs sat after the first fence found, so a position-blind
        scan read a blank line and returned nothing either way. The fixture
        was written to describe the defect rather than to separate the two
        implementations.

        The discriminating shape needs a `key: value` on line 2 and a fence on
        line 3. This is a setext heading -- `Title` underlined with `---` --
        which is exactly the realistic case: a position-blind parser reads the
        line between as metadata and swallows body text into node properties.
        """
        assert parse_front_matter("Title\nkey: swallowed\n---\n\nBody.\n") == {}

    def test_a_reserved_key_cannot_overwrite_an_identity_property(self) -> None:
        """A document may not declare `path:` or `qualified_name:`.

        Found by mutation: dropping the reserved-key check changed nothing,
        because no fixture declared one. Without it a document could rename
        its own node or point it at another file, and every consumer that
        resolves a Module back to a file would follow the document's claim
        rather than the filesystem.

        The surrounding valid pairs still survive, so one hostile or mistaken
        key does not discard the rest.
        """
        assert parse_front_matter(
            "---\npath: /etc/passwd\nqualified_name: other.module\npurpose: p\n---\n"
        ) == {"purpose": "p"}

    def test_an_unterminated_block_yields_nothing(self) -> None:
        """No closing delimiter means the block never ends.

        Treating the rest of the file as metadata would put arbitrary prose
        into node properties. Refusing is the conservative reading, and it is
        also what a YAML parser would do.
        """
        assert parse_front_matter("---\npurpose: planning\n\n# Title\n") == {}

    def test_lines_without_a_colon_are_skipped_not_fatal(self) -> None:
        """One malformed line must not discard the whole block.

        Front-matter is hand-written, so a stray line is likelier than a
        wholly invalid block. The valid pairs around it still carry meaning.
        """
        assert parse_front_matter(
            "---\npurpose: planning\nnot a pair\nscope: x\n---\n"
        ) == {"purpose": "planning", "scope": "x"}

    def test_a_nested_key_is_not_hoisted_to_the_top_level(self) -> None:
        """`child:` under `parent:` must not become a top-level declaration.

        The worst of the three: hoisting produced `{"parent": "", "child":
        "v"}`, so a nested key became indistinguishable from one the author
        declared at the top level. The docstring said "top-level scalars
        only" while the code took every line with a colon -- the contract and
        the implementation disagreed (reported on #1488).
        """
        assert parse_front_matter("---\nparent:\n  child: v\npurpose: p\n---\n") == {
            "purpose": "p"
        }

    def test_a_comment_line_declares_nothing(self) -> None:
        """`# note: x` would otherwise become the key `# note`."""
        assert parse_front_matter("---\n# note: x\npurpose: p\n---\n") == {
            "purpose": "p"
        }

    def test_a_key_opening_a_structure_is_skipped(self) -> None:
        """`tags:` with no value opens a list, and is not an empty scalar.

        Recording `{"tags": ""}` asserts the author declared it empty, which
        is a different claim from declaring a structure this parser does not
        represent. The distinction matters because a consumer cannot tell the
        two apart after the fact.
        """
        assert parse_front_matter("---\ntags:\n  - a\n  - b\n---\n") == {}

    def test_a_flow_collection_is_not_stored_as_a_scalar(self) -> None:
        """`[a, b]` and `{k: v}` are structures written on one line.

        Storing the source text makes a list indistinguishable from a string
        that happens to look like one, and no consumer can recover which was
        meant. Reported on #1488.
        """
        assert parse_front_matter("---\ntags: [a, b]\n---\n") == {}
        assert parse_front_matter("---\nmeta: {k: v}\n---\n") == {}

    def test_a_block_scalar_marker_is_not_the_value(self) -> None:
        """`key: |` stores the MARKER, not the text beneath it.

        The worst of the collection cases: the value became the literal string
        `"|"` -- punctuation mistaken for content -- while the actual text was
        silently dropped, because this parser skips the indented lines that
        carry it.

        All six markers are covered: `|` and `>` with the chomping variants
        `-` and `+`, since a parser matching only the bare forms leaves four
        spellings storing punctuation.
        """
        for marker in ("|", ">", "|-", ">-", "|+", ">+"):
            assert parse_front_matter(
                f"---\nnote: {marker}\n  text\n---\n"
            ) == {}, marker

    def test_a_value_that_merely_contains_a_bracket_is_kept(self) -> None:
        """The control: only a LEADING bracket opens a collection.

        Without this the guard could reject any value containing punctuation,
        which would silently drop ordinary metadata -- a fix worse than the
        defect.
        """
        assert parse_front_matter("---\ntitle: Draft [v2]\n---\n") == {
            "title": "Draft [v2]"
        }

    def test_an_empty_key_is_refused(self) -> None:
        """`: value` names nothing and must not become a property.

        An empty-string key would collide with any other empty-string key and
        is not addressable by a consumer.
        """
        assert parse_front_matter("---\n: orphan\npurpose: p\n---\n") == {
            "purpose": "p"
        }


class TestIngestion:
    """The declared metadata must reach the Module node.

    Guarded on the grammar, unlike `TestParsing` above. These two index a
    fixture through the real updater, so without `tree_sitter_markdown` no
    Module node is emitted at all and both fail with "no Module node emitted"
    -- a missing optional dependency reported as a product defect.

    The parser tests are deliberately NOT guarded: they are pure string
    handling and must run on the base install, which is where the contract
    they pin is easiest to break unnoticed.
    """

    pytestmark = pytest.mark.skipif(
        importlib.util.find_spec("tree_sitter_markdown") is None,
        reason="markdown grammar ships in the treesitter-full extra",
    )

    def test_front_matter_becomes_module_properties(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        """A Module node carries its declared front-matter.

        Asserted on the node the graph actually emits rather than on the
        parser alone: a correct parser whose output is never attached would
        satisfy every test in the class above and change nothing observable.
        """
        from codebase_rag.tests.conftest import run_updater
        from codebase_rag.types_defs import NodeType

        project = temp_repo / "md_fm"
        project.mkdir()
        (project / "plan.md").write_text(
            "---\npurpose: planning\nscope: service-X\n---\n\n# Plan\n\nBody.\n",
            encoding="utf-8",
        )

        run_updater(project, mock_ingestor)

        modules = [
            call.args[1]
            for call in mock_ingestor.ensure_node_batch.call_args_list
            if call.args[0] == NodeType.MODULE
            and str(call.args[1].get("qualified_name", "")).endswith("plan_md")
        ]

        assert modules, "no Module node emitted for plan.md"
        props = modules[0]

        # ONE declared property holding sorted "key=value" entries, not a
        # property per key. The graph's node schema is a fixed property list
        # audited on every ingest -- arbitrary keys would be undocumented
        # properties, and a document could otherwise define any node property
        # it liked. The audit caught this design when it was wrong.
        assert props.get("front_matter") == ["purpose=planning", "scope=service-X"], (
            props
        )

    def test_a_document_without_front_matter_gains_no_properties(
        self, temp_repo: Path, mock_ingestor: MagicMock
    ) -> None:
        """The control: absence must stay absent.

        Emitting empty-string properties for every ordinary document would
        make "has no declared purpose" indistinguishable from "declares an
        empty purpose", and would write a property onto every Markdown node
        in every repository.
        """
        from codebase_rag.tests.conftest import run_updater
        from codebase_rag.types_defs import NodeType

        project = temp_repo / "md_plain"
        project.mkdir()
        (project / "notes.md").write_text("# Notes\n\nBody.\n", encoding="utf-8")

        run_updater(project, mock_ingestor)

        modules = [
            call.args[1]
            for call in mock_ingestor.ensure_node_batch.call_args_list
            if call.args[0] == NodeType.MODULE
            and str(call.args[1].get("qualified_name", "")).endswith("notes_md")
        ]

        assert modules, "no Module node emitted for notes.md"
        props = modules[0]
        assert "front_matter" not in props, props
