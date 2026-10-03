"""Issue #2458: a Markdown link keeps what it says in the graph.

`LINKS_TO` was one `Module -> File` edge per distinct target file, with no
properties. Everything else a link states was dropped: the heading an anchor
names (`guide.md#setup`, or `#usage` in the same file), the section the link
sits in, where it is and what it reads, and the links whose file is missing.
List-valued front matter (`tags: [a, b]`) was dropped the same way.

These index real files, so the assertions are about what reaches the
ingestor, not about a helper's return value.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_audit as ga
from codebase_rag import logs as ls
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.document_tier import DocumentTier, parse_front_matter
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord, PropertyDict
from evals.cgr_graph import _StatefulIngestor

pytest.importorskip(
    "tree_sitter_markdown",
    reason="markdown grammar ships in the treesitter-full extra",
)

LINKS_TO = cs.RelationshipType.LINKS_TO.value
MODULE = cs.NodeLabel.MODULE.value
SECTION = cs.NodeLabel.SECTION.value
FILE = cs.NodeLabel.FILE.value

# The repository the issue's screenshot indexes, line for line.
README = (
    "# Project\n"
    "\n"
    "See [the guide](docs/guide.md) and [core code](pkg/core.py) and "
    "[run()](pkg/core.py#L1).\n"
    "Broken: [missing](docs/nope.md). Anchor: [setup section](docs/guide.md#setup).\n"
    "Self anchor: [below](#usage).\n"
    "\n"
    "## Usage\n"
    "\n"
    "Text.\n"
    "\n"
    "## Usage\n"
    "\n"
    "Duplicate heading.\n"
)
GUIDE = (
    "---\n"
    "title: Guide\n"
    "tags: [a, b]\n"
    "---\n"
    "# Guide\n"
    "\n"
    "## Setup\n"
    "\n"
    "Back to [readme](../README.md).\n"
)
CORE = "def run():\n    return 1\n"
ISSUE_REPO = {"README.md": README, "docs/guide.md": GUIDE, "pkg/core.py": CORE}

Endpoint = tuple[str, str]
Link = tuple[Endpoint, Endpoint, PropertyDict]


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


def _run(tmp_path: Path, files: dict[str, str]) -> MagicMock:
    _write(tmp_path, files)
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock, repo_path=tmp_path, parsers=parsers, queries=queries
    ).run()
    return mock


def _props_of(call) -> PropertyDict:
    if len(call.args) > 3 and call.args[3]:
        return dict(call.args[3])
    return dict(call.kwargs.get("properties") or {})


def _links(mock: MagicMock) -> list[Link]:
    """Every LINKS_TO the run emitted, endpoints as (label, key value)."""
    return [
        (
            (str(c.args[0][0]), str(c.args[0][2])),
            (str(c.args[2][0]), str(c.args[2][2])),
            _props_of(c),
        )
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == LINKS_TO
    ]


def _modules(mock: MagicMock) -> dict[str, PropertyDict]:
    return {
        str(c.args[1][cs.KEY_PATH]): c.args[1]
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == MODULE
    }


def _qn(tmp_path: Path, *parts: str) -> str:
    return ".".join([tmp_path.name, *parts])


def _abs(tmp_path: Path, rel: str) -> str:
    return (tmp_path / rel).resolve().as_posix()


def _by_text(links: list[Link], text: str) -> Link:
    found = [link for link in links if link[2].get(cs.KEY_LINK_TEXT) == text]
    assert len(found) == 1, (text, links)
    return found[0]


class TestAnchorsResolveToSections:
    def test_an_anchor_naming_a_heading_in_another_document_targets_that_section(
        self, tmp_path: Path
    ) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        _, target, props = _by_text(links, "setup section")
        assert target == (SECTION, _qn(tmp_path, "docs", "guide_md", "Guide", "Setup"))
        assert props[cs.KEY_ANCHOR] == "setup"

    def test_a_same_file_anchor_targets_the_section_it_names(
        self, tmp_path: Path
    ) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        _, target, props = _by_text(links, "below")
        assert target == (SECTION, _qn(tmp_path, "README_md", "Project", "Usage"))
        assert props[cs.KEY_ANCHOR] == "usage"

    def test_a_repeated_heading_is_reached_by_its_numbered_slug(
        self, tmp_path: Path
    ) -> None:
        # GitHub numbers a repeated heading's anchor `usage-1`; the section
        # carries the `@<line>` qn the tier gives a repeated heading.
        readme = README.replace("[below](#usage)", "[second](#usage-1)")
        links = _links(_run(tmp_path, {**ISSUE_REPO, "README.md": readme}))

        _, target, _ = _by_text(links, "second")
        assert target == (
            SECTION,
            _qn(tmp_path, "README_md", "Project", f"Usage{cs.DUP_QN_MARKER}11"),
        )

    def test_an_anchor_is_matched_against_the_slug_github_renders(
        self, tmp_path: Path
    ) -> None:
        doc = "# Guide\n\n## The `run()` Helper!\n\nSee [it](#the-run-helper).\n"
        links = _links(_run(tmp_path, {"guide.md": doc}))

        _, target, _ = _by_text(links, "it")
        assert target == (
            SECTION,
            _qn(tmp_path, "guide_md", "Guide", "The `run()` Helper!"),
        )

    def test_an_anchor_into_a_code_file_targets_the_file_and_keeps_the_anchor(
        self, tmp_path: Path
    ) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        _, target, props = _by_text(links, "run()")
        assert target == (FILE, _abs(tmp_path, "pkg/core.py"))
        assert props[cs.KEY_ANCHOR] == "L1"

    def test_an_anchor_no_heading_names_falls_back_to_the_file(
        self, tmp_path: Path
    ) -> None:
        readme = README.replace("docs/guide.md#setup", "docs/guide.md#nowhere")
        links = _links(_run(tmp_path, {**ISSUE_REPO, "README.md": readme}))

        _, target, props = _by_text(links, "setup section")
        assert target == (FILE, _abs(tmp_path, "docs/guide.md"))
        assert props[cs.KEY_ANCHOR] == "nowhere"

    def test_a_same_file_anchor_naming_no_heading_falls_back_to_its_file(
        self, tmp_path: Path
    ) -> None:
        # The edge keeps the dangling anchor findable, as for another file.
        readme = README.replace("[below](#usage)", "[below](#nowhere)")
        links = _links(_run(tmp_path, {**ISSUE_REPO, "README.md": readme}))

        _, target, props = _by_text(links, "below")
        assert target == (FILE, _abs(tmp_path, "README.md"))
        assert props[cs.KEY_ANCHOR] == "nowhere"


def _section_qn(mock: MagicMock, name: str) -> str:
    """The qn of the one Section emitted under this heading text."""
    found = [
        str(c.args[1][cs.KEY_QUALIFIED_NAME])
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == SECTION and c.args[1].get(cs.KEY_NAME) == name
    ]
    assert len(found) == 1, (name, found)
    return found[0]


def _jump_target(tmp_path: Path, heading: str, anchor: str) -> tuple[Endpoint, str]:
    """Where `[jump](#anchor)` lands in a document with one `## heading`."""
    doc = f"# Doc\n\n## {heading}\n\n[jump](#{anchor})\n\n[api]: api.md\n"
    mock = _run(tmp_path, {"doc.md": doc, "api.md": "# API\n", "guide.md": "# G\n"})
    return _by_text(_links(mock), "jump")[1], _section_qn(mock, heading)


class TestHeadingAnchorsUseTheRenderedText:
    """A heading's anchor is slugged from the text GitHub renders (PR #2830).

    Slugging the raw Markdown kept a link's destination, emphasis markers
    and HTML tags in the slug, so a valid anchor fell back to the File.
    """

    @pytest.mark.parametrize(
        ("heading", "anchor"),
        [
            ("See [the guide](guide.md) now", "see-the-guide-now"),
            ("Read [Guide](guide.md)", "read-guide"),
            ("Logo ![alt text](img.png)", "logo-alt-text"),
            ("Read [the API][api]", "read-the-api"),
            ("**Bold** and _em_", "bold-and-em"),
            ("Hello <code>world</code>", "hello-world"),
        ],
    )
    def test_markup_contributes_only_its_rendered_text(
        self, tmp_path: Path, heading: str, anchor: str
    ) -> None:
        target, section = _jump_target(tmp_path, heading, anchor)

        assert target == (SECTION, section)

    @pytest.mark.parametrize(
        ("heading", "anchor"),
        [
            # Negative: code keeps its content, `<div>` included; an autolink
            # keeps its URL; a plain heading keeps the slug it had.
            ("The `<div>` tag", "the-div-tag"),
            ("See <https://x.io>", "see-httpsxio"),
            ("Plain Heading", "plain-heading"),
        ],
    )
    def test_text_that_renders_as_written_keeps_its_slug(
        self, tmp_path: Path, heading: str, anchor: str
    ) -> None:
        target, section = _jump_target(tmp_path, heading, anchor)

        assert target == (SECTION, section)

    def test_a_repeat_of_a_rendered_heading_is_numbered(self, tmp_path: Path) -> None:
        # Negative: `## [A](x.md)` renders as `A`, so the plain `## A` after
        # it is the repeat and takes `a-1`, the duplicate rule as before.
        doc = "# Doc\n\n## [A](x.md)\n\n## A\n\n[first](#a) [second](#a-1)\n"
        mock = _run(tmp_path, {"doc.md": doc})
        links = _links(mock)

        assert _by_text(links, "first")[1] == (SECTION, _section_qn(mock, "[A](x.md)"))
        assert _by_text(links, "second")[1] == (SECTION, _section_qn(mock, "A"))


class TestLinksStartAtTheirSection:
    def test_a_link_starts_at_the_innermost_section_containing_it(
        self, tmp_path: Path
    ) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        source, _, _ = _by_text(links, "readme")
        assert source == (SECTION, _qn(tmp_path, "docs", "guide_md", "Guide", "Setup"))

    def test_a_link_after_a_subsection_closes_starts_at_the_parent_again(
        self, tmp_path: Path
    ) -> None:
        doc = "# Top\n\n## Inner\n\n[in](a.md)\n\n# Next\n\n[out](a.md)\n"
        links = _links(_run(tmp_path, {"d.md": doc, "a.md": "# A\n"}))

        assert _by_text(links, "in")[0] == (
            SECTION,
            _qn(tmp_path, "d_md", "Top", "Inner"),
        )
        assert _by_text(links, "out")[0] == (SECTION, _qn(tmp_path, "d_md", "Next"))

    def test_a_link_before_the_first_heading_starts_at_the_module(
        self, tmp_path: Path
    ) -> None:
        # Negative: a link no heading encloses still starts at the document.
        links = _links(_run(tmp_path, {"d.md": "[a](a.md)\n\n# Later\n", "a.md": ""}))

        assert [link[0] for link in links] == [(MODULE, _qn(tmp_path, "d_md"))]


class TestOneEdgePerLink:
    def test_each_link_records_its_line_column_and_text(self, tmp_path: Path) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        _, _, props = _by_text(links, "core code")
        assert props[cs.KEY_LINE] == 3
        assert props[cs.KEY_COL] == len("See [the guide](docs/guide.md) and ")
        assert props[cs.KEY_END_LINE] == 3
        assert props[cs.KEY_END_COL] == props[cs.KEY_COL] + len(
            "[core code](pkg/core.py)"
        )

    def test_two_links_to_one_file_are_two_edges(self, tmp_path: Path) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        to_core = [
            link for link in links if link[1][1] == _abs(tmp_path, "pkg/core.py")
        ]
        assert sorted(link[2][cs.KEY_LINK_TEXT] for link in to_core) == [
            "core code",
            "run()",
        ]

    def test_the_issue_repository_keeps_every_resolvable_link(
        self, tmp_path: Path
    ) -> None:
        links = _links(_run(tmp_path, ISSUE_REPO))

        assert sorted(str(link[2][cs.KEY_LINK_TEXT]) for link in links) == [
            "below",
            "core code",
            "readme",
            "run()",
            "setup section",
            "the guide",
        ]

    def test_a_reference_link_is_located_at_its_use_not_its_definition(
        self, tmp_path: Path
    ) -> None:
        doc = "# Guide\n\nSee [the API][api].\n\n## Refs\n\n[api]: api.md\n"
        links = _links(_run(tmp_path, {"guide.md": doc, "api.md": "# API\n"}))

        source, target, props = _by_text(links, "the API")
        assert source == (SECTION, _qn(tmp_path, "guide_md", "Guide"))
        assert target == (FILE, _abs(tmp_path, "api.md"))
        assert props[cs.KEY_LINE] == 3

    def test_a_link_without_an_anchor_carries_no_anchor(self, tmp_path: Path) -> None:
        # Negative: absent, not an empty string, so `r.anchor IS NULL` is the
        # test for "no fragment".
        links = _links(_run(tmp_path, ISSUE_REPO))

        assert cs.KEY_ANCHOR not in _by_text(links, "the guide")[2]

    def test_one_link_per_site_is_the_write_key(self) -> None:
        # The ingestor MERGEs on these, so two links to one target are two
        # edges in the store and not one edge rewritten twice.
        assert cs.MERGE_KEY_PROPS_BY_REL[LINKS_TO] == (cs.KEY_LINE, cs.KEY_COL)


def _capture_info() -> tuple[list[str], int]:
    """INFO-and-up log messages as written, without loguru's line ending."""
    messages: list[str] = []

    def record(message: str) -> None:
        messages.append(message.rstrip("\n"))

    sink = logger.add(record, level="INFO", format="{message}")
    return messages, sink


class TestBrokenLinks:
    def test_a_link_to_a_missing_file_is_listed_on_its_module(
        self, tmp_path: Path
    ) -> None:
        modules = _modules(_run(tmp_path, ISSUE_REPO))

        assert modules["README.md"][cs.KEY_BROKEN_LINKS] == ["docs/nope.md"]

    def test_a_sync_reports_how_many_links_are_broken(self, tmp_path: Path) -> None:
        messages, sink = _capture_info()
        try:
            _run(tmp_path, ISSUE_REPO)
        finally:
            logger.remove(sink)

        assert ls.MARKDOWN_BROKEN_LINKS.format(count=1, documents=1) in messages

    def test_a_document_whose_links_resolve_stores_an_empty_list(
        self, tmp_path: Path
    ) -> None:
        # An empty list overwrites a stale one on re-ingest; omission cannot.
        modules = _modules(_run(tmp_path, ISSUE_REPO))

        assert modules["docs/guide.md"][cs.KEY_BROKEN_LINKS] == []

    def test_external_outside_and_directory_links_are_not_broken(
        self, tmp_path: Path
    ) -> None:
        # Negative: only a repository path that does not exist is broken.
        repo = tmp_path / "repo"
        doc = (
            "# D\n\n[web](https://example.com/x.md) [mail](mailto:a@b.c) "
            "[out](../elsewhere/x.md) [dir](docs) [anchor](#d)\n"
        )
        modules = _modules(_run(repo, {"d.md": doc, "docs/x.md": "# X\n"}))

        assert modules["d.md"][cs.KEY_BROKEN_LINKS] == []

    def test_a_broken_link_still_emits_no_edge(self, tmp_path: Path) -> None:
        # Negative: reporting it must not invent a target.
        links = _links(_run(tmp_path, ISSUE_REPO))

        assert not [link for link in links if "nope" in link[1][1]]

    def test_no_summary_when_every_link_resolves(self, tmp_path: Path) -> None:
        messages, sink = _capture_info()
        try:
            _run(tmp_path, {"d.md": "# D\n\n[a](a.md)\n", "a.md": "# A\n"})
        finally:
            logger.remove(sink)

        summary = ls.MARKDOWN_BROKEN_LINKS.split("{", 1)[1].split("}", 1)[1]
        assert not [m for m in messages if summary in m]


class TestListFrontMatter:
    def test_a_flow_list_is_stored_comma_joined(self, tmp_path: Path) -> None:
        modules = _modules(_run(tmp_path, ISSUE_REPO))

        assert modules["docs/guide.md"][cs.KEY_FRONT_MATTER] == [
            "tags=a,b",
            "title=Guide",
        ]

    def test_a_block_list_is_stored_comma_joined(self) -> None:
        text = "---\ntags:\n  - a\n  - b\ntitle: T\n---\n"

        assert parse_front_matter(text) == {"tags": ("a", "b"), "title": "T"}

    def test_a_zero_indented_block_list_is_a_list(self) -> None:
        assert parse_front_matter("---\ntags:\n- a\n- b\n---\n") == {"tags": ("a", "b")}

    def test_list_items_are_unquoted_and_lose_their_comments(self) -> None:
        text = "---\ntags: ['a b', \"c\"]\nmore:\n  - d # note\n  - 'e # f'\n---\n"

        assert parse_front_matter(text) == {
            "tags": ("a b", "c"),
            "more": ("d", "e # f"),
        }

    def test_a_flow_mapping_is_still_skipped(self) -> None:
        # Negative: a map is not a list, and has no `key=value` spelling.
        assert parse_front_matter("---\nmeta: {k: v}\n---\n") == {}

    def test_a_list_of_mappings_is_skipped(self) -> None:
        # Negative: an item that is itself a structure has no flat spelling.
        text = "---\nauthors:\n  - name: a\n  - name: b\nkeep: x\n---\n"

        assert parse_front_matter(text) == {"keep": "x"}

    def test_a_nested_list_is_skipped(self) -> None:
        assert parse_front_matter("---\ntags: [a, [b]]\nkeep: x\n---\n") == {
            "keep": "x"
        }

    def test_an_empty_list_declares_nothing(self) -> None:
        assert parse_front_matter("---\ntags: []\n---\n") == {}

    def test_a_nested_mapping_is_still_skipped(self) -> None:
        # Negative: `parent:` followed by an indented `child: v` is a map.
        text = "---\nparent:\n  child: v\nkeep: x\n---\n"

        assert parse_front_matter(text) == {"keep": "x"}


def _index(store: _StatefulIngestor, repo: Path, force: bool = False) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _stored_links(store: _StatefulIngestor) -> set[tuple]:
    """LINKS_TO edges whose two ends exist, as a MATCH-based write keeps."""
    found = set()
    for edges in store._out.values():
        for edge in edges:
            from_label, from_val, rel, to_label, to_val, _site = edge
            if rel != LINKS_TO:
                continue
            if (from_label, from_val) not in store.nodes:
                continue
            if (to_label, to_val) not in store.nodes:
                continue
            props = store.edge_props.get(edge, {})
            found.add(
                (from_label, from_val, to_label, to_val, tuple(sorted(props.items())))
            )
    return found


def _broken(store: _StatefulIngestor) -> dict[str, list]:
    return {
        str(props[cs.KEY_PATH]): list(props.get(cs.KEY_BROKEN_LINKS) or [])
        for (label, _), props in store.nodes.items()
        if label == MODULE and str(props.get(cs.KEY_PATH, "")).endswith(".md")
    }


def _touch(path: Path, cache: Path) -> None:
    future = cache.stat().st_mtime + 10
    os.utime(path, (future, future))
    os.utime(path.parent, (future, future))


def _clean(repo: Path) -> _StatefulIngestor:
    clean = _StatefulIngestor()
    _index(clean, repo, force=True)
    return clean


class TestIncrementalSyncMatchesACleanIndex:
    """A sync that re-parses only some files ends where a clean index does."""

    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "proj"
        _write(repo, ISSUE_REPO)
        return repo

    def _sync(self, store: _StatefulIngestor, repo: Path) -> None:
        _index(store, repo)

    def test_editing_the_linked_document_keeps_the_anchor_edge(
        self, repo: Path
    ) -> None:
        store = _StatefulIngestor()
        _index(store, repo)
        guide = repo / "docs" / "guide.md"
        guide.write_text(GUIDE + "\nMore text.\n", encoding="utf-8")
        _touch(guide, repo / cs.HASH_CACHE_FILENAME)

        self._sync(store, repo)

        assert (
            SECTION,
            "proj.docs.guide_md.Guide.Setup",
        ) in {(link[2], link[3]) for link in _stored_links(store)}
        assert _stored_links(store) == _stored_links(_clean(repo))

    def test_renaming_the_linked_heading_falls_back_to_the_file(
        self, repo: Path
    ) -> None:
        store = _StatefulIngestor()
        _index(store, repo)
        guide = repo / "docs" / "guide.md"
        guide.write_text(GUIDE.replace("## Setup", "## Install"), encoding="utf-8")
        _touch(guide, repo / cs.HASH_CACHE_FILENAME)

        self._sync(store, repo)

        assert _stored_links(store) == _stored_links(_clean(repo))
        assert not [link for link in _stored_links(store) if link[3].endswith("Setup")]

    def test_a_document_linking_into_a_re_parsed_dependent_keeps_its_edge(
        self, repo: Path
    ) -> None:
        # notes.md links into README's Usage section. Editing guide.md
        # re-parses README (it links into guide's Setup section), which
        # recreates README's sections; notes.md's edge must come back.
        _write(repo, {"notes.md": "# Notes\n\nSee [usage](README.md#usage).\n"})
        store = _StatefulIngestor()
        _index(store, repo)
        guide = repo / "docs" / "guide.md"
        guide.write_text(GUIDE + "\nMore text.\n", encoding="utf-8")
        _touch(guide, repo / cs.HASH_CACHE_FILENAME)

        self._sync(store, repo)

        assert (
            SECTION,
            "proj.README_md.Project.Usage",
        ) in {(link[2], link[3]) for link in _stored_links(store)}
        assert _stored_links(store) == _stored_links(_clean(repo))

    def test_deleting_a_linked_file_lists_it_as_broken(self, repo: Path) -> None:
        store = _StatefulIngestor()
        _index(store, repo)
        (repo / "pkg" / "core.py").unlink()
        _touch(repo / "pkg", repo / cs.HASH_CACHE_FILENAME)

        self._sync(store, repo)

        clean = _clean(repo)
        assert _broken(store)["README.md"] == ["pkg/core.py", "docs/nope.md"]
        assert _broken(store) == _broken(clean)
        assert _stored_links(store) == _stored_links(clean)

    def test_creating_a_missing_target_adds_its_edge(self, repo: Path) -> None:
        store = _StatefulIngestor()
        _index(store, repo)
        nope = repo / "docs" / "nope.md"
        nope.write_text("# Nope\n", encoding="utf-8")
        _touch(nope, repo / cs.HASH_CACHE_FILENAME)

        self._sync(store, repo)

        clean = _clean(repo)
        assert _broken(store)["README.md"] == []
        assert _broken(store) == _broken(clean)
        assert _stored_links(store) == _stored_links(clean)

    def test_editing_a_linked_code_file_does_not_re_parse_the_document(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Negative: a File target outlives its file's re-parse, so the
        # document linking it has nothing to recompute.
        store = _StatefulIngestor()
        _index(store, repo)
        core = repo / "pkg" / "core.py"
        core.write_text(CORE + "\n\ndef other():\n    return 2\n", encoding="utf-8")
        _touch(core, repo / cs.HASH_CACHE_FILENAME)
        parsed: list[str] = []
        original = DocumentTier.process_file

        def record(tier: DocumentTier, file_path: Path, elements: dict) -> None:
            parsed.append(file_path.name)
            original(tier, file_path, elements)

        monkeypatch.setattr(DocumentTier, "process_file", record)

        self._sync(store, repo)

        assert parsed == []
        assert _stored_links(store) == _stored_links(_clean(repo))


class TestSchemaAndDocs:
    def test_section_ends_are_documented_link_shapes(self) -> None:
        documented = ga.documented_relationship_triples()

        for source in (MODULE, SECTION):
            for target in (FILE, SECTION):
                assert (source, LINKS_TO, target) in documented

    def test_the_issue_repository_passes_the_audit(self, tmp_path: Path) -> None:
        mock = _run(tmp_path, ISSUE_REPO)
        nodes = [
            GraphNodeRecord(str(c.args[0]), c.args[1])
            for c in mock.ensure_node_batch.call_args_list
        ]
        rels = [
            GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
            for c in mock.ensure_relationship_batch.call_args_list
        ]

        assert ga.collect_violations(nodes, rels) == []

    def test_a_link_into_a_function_is_still_undocumented(self) -> None:
        # Negative: widening the ends must not document LINKS_TO wholesale.
        violations = ga.find_relationship_violations(
            [
                GraphRelRecord(
                    (SECTION, cs.KEY_QUALIFIED_NAME, "proj.d_md.H"),
                    cs.RelationshipType.LINKS_TO,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, "proj.app.main"),
                )
            ]
        )

        assert [v.check for v in violations] == [
            cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP
        ]

    @pytest.mark.parametrize(
        "doc",
        ["docs/architecture/graph-schema.md", "docs/architecture/language-support.md"],
    )
    def test_the_docs_describe_the_link_edge(self, doc: str) -> None:
        text = (Path(__file__).resolve().parents[2] / doc).read_text(encoding="utf-8")

        for word in ("LINKS_TO", "`anchor`", "`text`", "`broken_links`"):
            assert word in text, (doc, word)


def test_the_context_slice_finds_a_document_that_links_from_a_section(
    tmp_path: Path,
) -> None:
    # The doc-sections read walked `(Module)-[:LINKS_TO]->(File)`; a link now
    # starts at its section, and the read must still find the document.
    repo = tmp_path / "proj"
    _write(repo, ISSUE_REPO)
    store = _StatefulIngestor()
    _index(store, repo, force=True)

    rows = store.fetch_all(
        cq.CYPHER_CONTEXT_DOC_SECTIONS,
        {
            cs.KEY_ABSOLUTE_PATH: _abs(repo, "pkg/core.py"),
            cs.KEY_PROJECT_PREFIX: "proj.",
        },
    )

    assert {(r[cs.KEY_FROM_QN], r[cs.KEY_QUALIFIED_NAME]) for r in rows} == {
        ("proj.README_md", "proj.README_md.Project")
    }
