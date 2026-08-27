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
from pathlib import Path

import pytest

from codebase_rag import logs

_LANGUAGE_SUPPORT_DOC = (
    Path(__file__).resolve().parents[2] / "docs/architecture/language-support.md"
)

# "Run 'cgr start --clean'" and friends: an imperative followed by a command
# containing --clean. `--update-graph` appearing anywhere inside that command is
# what makes the advice correct.
#
# Three delimiter forms are recognised, because a remedy is equally dangerous
# however it happens to be punctuated: single-quoted, backticked, and bare
# (running to sentence punctuation, a command connector, or end of line). The
# `run` imperative stays mandatory -- it is what separates advice from prose
# that merely discusses the flag, which `test_prose_about_clean_is_not_flagged`
# pins.
#
# The bare form must also stop at a connector such as "then" or "followed by".
# Without that it swallows the *next* invocation too, and a trailing safe
# command launders a destructive leading one: "Run cgr start --clean then run
# cgr start --update-graph" captured as a single string, whose
# `--update-graph` satisfied the check for a command that does not have it.
#
# `and` is a connector on its own, not merely an optional prefix to `then`. The
# first version of this fix only ever matched `and` as part of `and then`, so
# "Run cgr start --clean and run cgr start --update-graph" still captured as one
# string and laundered exactly as before -- the bug the connector list exists to
# prevent, surviving in the conjunction nobody enumerated (Greptile P1, #1444).
_CONNECTOR = (
    r"(?:\s+and\s+then\b|\s+and\b|\s+then\b|\s+followed\s+by\b|\s+before\b|\s+&&)"
)

_REMEDY = re.compile(
    r"run\s+(?:'([^'\n]*--clean[^'\n]*)'"
    r"|`([^`\n]*--clean[^`\n]*)`"
    r"|((?:(?!" + _CONNECTOR + r")[^'\"`\n])*?--clean"
    r"(?:(?!" + _CONNECTOR + r")[^'\"`\n,.;:!?])*))",
    re.IGNORECASE,
)


def _commands(text: str) -> list[str]:
    """Every remedy command in `text`, whichever delimiter form it used.

    `_REMEDY` has one capture group per delimiter form, so each match yields a
    tuple with exactly one non-empty element.
    """
    return [
        group.strip() for match in _REMEDY.findall(text) for group in match if group
    ]


def _string_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(logs).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def _remedies() -> list[tuple[str, str]]:
    found = []
    for name, value in _string_constants().items():
        for command in _commands(value):
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
            "Run `cgr start --clean` to rebuild it from scratch.",
            "run `cgr start --clean --yes` to fix this",
            "Run cgr start --clean to rebuild it from scratch.",
            "run cgr start --clean --yes, then re-index",
        ],
    )
    def test_detector_catches_unquoted_and_backticked_remedies(self, text: str) -> None:
        """A remedy is dangerous however it is punctuated (CodeRabbit, #1444).

        The original matcher only inspected single-quoted commands, so a string
        saying "Run `cgr start --clean`" recommended destructive cleanup while
        `test_no_remedy_recommends_bare_clean` stayed green -- a hole in the very
        guard that exists to have none.
        """
        commands = _commands(text)
        assert commands
        assert all("--update-graph" not in c for c in commands)

    @pytest.mark.parametrize(
        "text",
        [
            "Run cgr start --clean then run cgr start --update-graph",
            "Run cgr start --clean and then run cgr start --update-graph",
            "Run cgr start --clean followed by cgr start --update-graph",
            "Run cgr start --clean && cgr start --update-graph",
            "Run cgr start --clean before cgr start --update-graph",
            # Standalone "and", with no "then" after it, was the hole the first
            # connector fix left: `and` was only ever optional *before* `then`,
            # never a boundary on its own (Greptile P1, #1444).
            "Run cgr start --clean and run cgr start --update-graph",
            "Run cgr start --clean, and run cgr start --update-graph",
            "Run cgr start --clean and cgr start --update-graph",
        ],
    )
    def test_bare_match_stops_at_a_command_connector(self, text: str) -> None:
        """A later safe command must not launder an earlier destructive one.

        The bare alternative ran to sentence punctuation, so two commands joined
        by "then" captured as a single string. `--update-graph` from the *second*
        command then satisfied the acceptance check for the *first*, and a string
        opening with a destructive bare `--clean` passed (CodeRabbit, #1444).

        Each invocation must be judged on its own, so the destructive one is
        still reported however safe its successor is.
        """
        commands = _commands(text)
        assert commands
        assert any("--update-graph" not in c for c in commands), (
            f"the destructive first command was laundered by a later one: {commands}"
        )

    @pytest.mark.parametrize(
        "text",
        [
            "Run `cgr start --clean --update-graph` to rebuild it.",
            "Run cgr start --clean --update-graph to rebuild it.",
        ],
    )
    def test_detector_accepts_corrected_unquoted_forms(self, text: str) -> None:
        """The corrected wording must pass in every delimiter form too."""
        commands = _commands(text)
        assert commands
        assert all("--update-graph" in c for c in commands)

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
        commands = _commands(text)
        assert commands
        assert all("--update-graph" not in c for c in commands)

    def test_detector_accepts_the_corrected_form(self) -> None:
        """The fixed wording must pass, or the rule would forbid the remedy too."""
        text = "Run 'cgr start --clean --update-graph' to rebuild it."
        commands = _commands(text)
        assert commands
        assert all("--update-graph" in c for c in commands)

    def test_operational_mentions_are_not_treated_as_remedies(self) -> None:
        """Naming the flag while describing an operation is not advice.

        `MG_LIST_PROJECTS_FAILED` says what failed *before* --clean ran. Pinning
        this keeps a future tightening of the rule from flagging it.
        """
        assert "--clean" in logs.MG_LIST_PROJECTS_FAILED
        assert not _commands(logs.MG_LIST_PROJECTS_FAILED)

    def test_docs_name_the_mtime_mechanism(self) -> None:
        """The skip rule is an mtime comparison, not content hashing (#1444).

        `graph_updater.py` skips a file when `filepath.stat().st_mtime <=
        cache_mtime`. Saying only "files not modified since the last sync"
        reads as content-based detection, which would make the parser-migration
        warning sound avoidable: touching a file forces a re-parse even when its
        content is identical, and that is precisely the distinction a user
        forcing a migration needs.
        """
        doc = _LANGUAGE_SUPPORT_DOC.read_text(encoding="utf-8")
        assert "mtime" in doc, (
            "language-support.md must name the mtime comparison explicitly "
            "rather than implying content-based change detection"
        )

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
        assert not _commands(text)
