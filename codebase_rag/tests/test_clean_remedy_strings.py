"""No user-facing remedy string tells the user to run a bare `cgr start --clean`.

`--clean` without `--update-graph` is destructive-only: `cli.py` deletes the
graph, clears the embeddings and the hash cache, then returns before indexing
anything (issues #1441, #1442). Advising it as a *fix* therefore hands the user
an empty graph, and in a shared database destroys every other project too.

The rule is narrower than "never mention --clean": a string may name the flag
while describing which operation is running, which is what
`MG_LIST_PROJECTS_FAILED` does. Only strings that *recommend running* it are
constrained, so the check looks for an imperative ("run '...'") wrapping the
flag rather than for the flag alone.
"""

import re

import pytest

from codebase_rag import logs

# "Run 'cgr start --clean'" and friends: an imperative followed by a command
# containing --clean, up to the closing quote. `--update-graph` appearing
# anywhere inside that command is what makes the advice correct.
_REMEDY = re.compile(r"run\s+'([^']*--clean[^']*)'", re.IGNORECASE)


def _string_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(logs).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def _remedies() -> list[tuple[str, str]]:
    found = []
    for name, value in _string_constants().items():
        for command in _REMEDY.findall(value):
            found.append((name, command))
    return found


class TestCleanRemedyStrings:
    def test_corpus_is_non_empty(self) -> None:
        """Guard the guard: an empty corpus would make every check below vacuous.

        This is the failure the rest of the class cannot detect on its own. If
        `vars(logs)` stopped yielding strings, `_remedies()` would return an
        empty list and the assertions would pass while inspecting nothing.
        """
        assert len(_string_constants()) > 100

    def test_clean_is_mentioned_somewhere(self) -> None:
        """The traversal reaches the strings this module is about."""
        mentioning = [n for n, v in _string_constants().items() if "--clean" in v]
        assert "PARSER_FINGERPRINT_MISMATCH" in mentioning

    def test_at_least_one_remedy_is_matched(self) -> None:
        """The regex matches real content, not just the empty set.

        Without this, weakening `_REMEDY` to something that never matches would
        leave `test_no_remedy_recommends_bare_clean` green.
        """
        assert _remedies()

    def test_no_remedy_recommends_bare_clean(self) -> None:
        bare = [
            (name, command)
            for name, command in _remedies()
            if "--update-graph" not in command
        ]
        assert not bare, (
            "These strings advise a bare --clean, which deletes the graph "
            f"without rebuilding it: {bare}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Run 'cgr start --clean' to rebuild it from scratch.",
            "run 'cgr start --clean --yes' to fix this",
        ],
    )
    def test_detector_catches_a_planted_bare_remedy(self, text: str) -> None:
        """Mutation check, inlined so it runs on every CI pass.

        A grep-style rule reports green both when nothing is wrong and when the
        matching is broken. Planting the exact phrasing the rule exists to
        forbid proves it is the former.
        """
        commands = _REMEDY.findall(text)
        assert commands
        assert all("--update-graph" not in c for c in commands)

    def test_detector_accepts_the_corrected_form(self) -> None:
        """The fixed wording must pass, or the rule would forbid the remedy too."""
        text = "Run 'cgr start --clean --update-graph' to rebuild it."
        commands = _REMEDY.findall(text)
        assert commands
        assert all("--update-graph" in c for c in commands)

    def test_operational_mentions_are_not_treated_as_remedies(self) -> None:
        """Naming the flag while describing an operation is not advice.

        `MG_LIST_PROJECTS_FAILED` says what failed *before* --clean ran. Pinning
        this keeps a future tightening of the rule from flagging it.
        """
        assert "--clean" in logs.MG_LIST_PROJECTS_FAILED
        assert not _REMEDY.findall(logs.MG_LIST_PROJECTS_FAILED)

    @pytest.mark.parametrize(
        "text",
        [
            "needs `--clean`:",
            "recovers without `--clean`.",
            "Reach for `--clean` only in that third case. It deletes every project.",
            "`--clean` on its own wipes without re-indexing the repository.",
        ],
    )
    def test_prose_about_clean_is_not_flagged(self, text: str) -> None:
        """Sentences that discuss the flag without recommending it must not match.

        A line-scoped "mentions --clean but not --update-graph" rule scores 100%
        false positives on correct documentation: measured against a known-good
        write-up of this flag, all four hits were noise. The last two are the
        sharpest, since a naive rule flags the very sentences warning against the
        bare form. These phrasings recur whenever someone documents the flag
        properly, so they are pinned rather than left to chance.
        """
        assert not _REMEDY.findall(text)
