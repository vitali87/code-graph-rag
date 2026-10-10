# Real-Memgraph check of issue #3230: a gloss's mentions were tracked by name
# alone. Renaming or moving a mentioned definition dropped its MENTIONS edge
# while the note stayed EXACT, and an unrelated definition that later took
# the old name inherited the mention. Mentions now carry the mentioned
# definition's content hash and text quote, the anchors the subject has.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import gloss
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

P = "gm"
HELPER = f"{P}.a.helper"
A = "def helper(x):\n    return x + 1\n"
B = "from a import helper\n\n\ndef run():\n    return helper(2)\n"
B_EMPTY = "from a import helper\n"
NEWCOMER = '\n\ndef run():\n    return "unrelated"\n'


class _Repo:
    def __init__(self, ingestor: MemgraphIngestor, root: Path) -> None:
        self.ingestor = ingestor
        self.root = root
        for rel, text in {"a.py": A, "b.py": B}.items():
            (root / rel).write_text(text, encoding="utf-8")
        parsers, queries = load_parsers()
        self.updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=P,
        )
        self.updater.run(force=True)

    def annotate(self, readable: bool = True) -> None:
        row = gloss.write_gloss(
            self.ingestor.fetch_all,
            self.ingestor.execute_write,
            P,
            HELPER,
            "run() relies on the +1",
            cs.GlossKind.INVARIANT,
            mentions=f"{P}.b.run",
            read_source=self.updater._read_project_source if readable else None,
        )
        assert row.get("mentions") == [f"{P}.b.run"], row

    def edit(self, files: dict[str, str]) -> None:
        for rel, text in files.items():
            (self.root / rel).write_text(text, encoding="utf-8")
        self.updater.reingest(sorted(files))

    def note(self) -> dict[str, object]:
        result = gloss.glosses_for(self.ingestor.fetch_all, P, HELPER)
        (row,) = result["annotating"]
        return {
            "anchor_state": row["anchor_state"],
            "mentions": row["mentions"],
            "mentions_lost": row.get("mentions_lost"),
        }

    def mentioning(self, target: str) -> list[str]:
        result = gloss.glosses_for(self.ingestor.fetch_all, P, target)
        return [row["body"] for row in result.get("mentioning", [])]


@pytest.fixture
def repo(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> _Repo:
    repo = _Repo(memgraph_ingestor, tmp_path)
    repo.annotate()
    return repo


def test_a_renamed_mention_is_followed(repo: _Repo) -> None:
    repo.edit({"b.py": B.replace("def run", "def run_job")})

    assert repo.note() == {
        "anchor_state": "EXACT",
        "mentions": [f"{P}.b.run_job"],
        "mentions_lost": [],
    }
    assert repo.mentioning(f"{P}.b.run_job") == ["run() relies on the +1"]


def test_a_mention_moved_unchanged_to_another_file_is_followed(repo: _Repo) -> None:
    repo.edit({"b.py": B_EMPTY, "c.py": B})

    assert repo.note()["mentions"] == [f"{P}.c.run"]
    assert repo.note()["mentions_lost"] == []


def test_a_newcomer_reusing_the_name_does_not_take_the_mention(repo: _Repo) -> None:
    # One edit renames `run` and adds an unrelated `run`: the mention follows
    # the renamed body, and the newcomer gets nothing.
    repo.edit({"b.py": B.replace("def run", "def run_job") + NEWCOMER})

    assert repo.note()["mentions"] == [f"{P}.b.run_job"]
    assert repo.mentioning(f"{P}.b.run") == []


def test_a_mention_that_cannot_be_placed_is_reported(repo: _Repo) -> None:
    repo.edit({"b.py": B_EMPTY})

    assert repo.note() == {
        "anchor_state": "EXACT",
        "mentions": [],
        "mentions_lost": [f"{P}.b.run"],
    }


def test_a_lost_mention_is_not_given_to_a_later_newcomer(repo: _Repo) -> None:
    repo.edit({"b.py": B_EMPTY})
    repo.edit({"b.py": B_EMPTY + NEWCOMER})

    assert repo.note()["mentions"] == []
    assert repo.note()["mentions_lost"] == [f"{P}.b.run"]
    assert repo.mentioning(f"{P}.b.run") == []


def test_a_lost_mention_whose_code_comes_back_is_restored(repo: _Repo) -> None:
    # Negative: the same body under the same name is the same definition.
    repo.edit({"b.py": B_EMPTY})
    repo.edit({"b.py": B})

    assert repo.note()["mentions"] == [f"{P}.b.run"]
    assert repo.note()["mentions_lost"] == []


def test_a_mention_whose_code_went_to_two_places_is_not_guessed(
    repo: _Repo,
) -> None:
    # Negative: two identical copies and no `run` left; neither is the one.
    repo.edit({"b.py": B_EMPTY, "c.py": B, "d.py": B})

    assert repo.note()["mentions"] == []
    assert repo.note()["mentions_lost"] == [f"{P}.b.run"]


def test_a_mention_edited_in_place_keeps_its_edge(repo: _Repo) -> None:
    # Negative: a changed body under the same name, with nowhere else the old
    # body went, is the same definition edited.
    repo.edit({"b.py": B.replace("helper(2)", "helper(3)")})

    assert repo.note()["mentions"] == [f"{P}.b.run"]
    assert repo.note()["mentions_lost"] == []
    # And it is followed from its edited body on, by a later rename.
    repo.edit({"b.py": B.replace("helper(2)", "helper(3)").replace("run", "go")})

    assert repo.note()["mentions"] == [f"{P}.b.go"]


def test_an_untouched_mention_stays_attached(repo: _Repo) -> None:
    # Negative: a sync that re-parses the mentioned file unchanged.
    repo.edit({"b.py": B})

    assert repo.note() == {
        "anchor_state": "EXACT",
        "mentions": [f"{P}.b.run"],
        "mentions_lost": [],
    }


def test_a_note_from_before_mention_anchors_still_attaches_by_name(
    repo: _Repo,
) -> None:
    # A note written before mentions recorded anchors has only the name to go
    # on: it re-attaches by name, as it did, and a gone name is reported.
    repo.ingestor.execute_write(
        "MATCH (g:Gloss) REMOVE g.mention_hashes, g.mention_quotes", None
    )
    repo.edit({"b.py": B})

    assert repo.note()["mentions"] == [f"{P}.b.run"]
    repo.edit({"b.py": B_EMPTY})

    assert repo.note()["mentions"] == []
    assert repo.note()["mentions_lost"] == [f"{P}.b.run"]
    # Its first pass recorded the code under the name, so the same code
    # coming back is the same definition.
    repo.edit({"b.py": B})

    assert repo.note()["mentions"] == [f"{P}.b.run"]
    assert repo.note()["mentions_lost"] == []


def test_a_note_from_before_mention_anchors_gains_them(repo: _Repo) -> None:
    # Its first pass records the mentioned definition's hash, so a later
    # move is followed.
    repo.ingestor.execute_write(
        "MATCH (g:Gloss) REMOVE g.mention_hashes, g.mention_quotes", None
    )
    repo.edit({"b.py": B})
    repo.edit({"b.py": B_EMPTY, "c.py": B})

    assert repo.note()["mentions"] == [f"{P}.c.run"]


@pytest.fixture
def unread(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> _Repo:
    # A note written away from the checkout records hashes but no quotes.
    repo = _Repo(memgraph_ingestor, tmp_path)
    repo.annotate(readable=False)
    return repo


def test_a_moved_mention_is_followed_by_its_hash_alone(unread: _Repo) -> None:
    unread.edit({"b.py": B_EMPTY, "c.py": B})

    assert unread.note()["mentions"] == [f"{P}.c.run"]


def test_an_edited_mention_is_followed_from_its_renewed_anchors(
    unread: _Repo,
) -> None:
    edited = B.replace("helper(2)", "helper(3)")
    unread.edit({"b.py": edited})
    unread.edit({"b.py": B_EMPTY, "c.py": edited})

    assert unread.note()["mentions"] == [f"{P}.c.run"]
