"""A C# verbatim identifier is not a duplicate marker (issue #2017).

`function_registry` appends `@<line>` (and `_<col>`) to disambiguate
definitions that share a qualified name. C# spells a keyword-colliding name
with a leading `@` -- `@event`, `@lock` -- so a reader that cuts at the first
`@` destroys the name instead of stripping a marker.

Every test here drives a SHIPPED reader rather than the helper, because a
helper that is correct in isolation says nothing about a caller that never
invokes it. The helper's own behaviour is pinned once, in
`TestHelperGrammar`, and everything else goes through production entry points.
"""

from __future__ import annotations

import pytest

from codebase_rag.constants import core as cs
from codebase_rag.utils.qn_markers import natural_qn, strip_dup_marker

# `@` + line, optionally `_` + column: what `function_registry` appends. Kept
# as data so a reader test names the same shape the producer writes.
MARKED = "Box@12"
MARKED_WITH_COLUMN = "Box@12_5"
# C# verbatim identifiers: the language's way of using a keyword as a name.
VERBATIM = "@event"
VERBATIM_LOCK = "@lock"
# The case that separates a correct fix from a plausible one: a DUPLICATE
# verbatim type carries both, and only the trailing numeric run is a marker.
VERBATIM_MARKED = "@event@12"


class TestHelperGrammar:
    """The helper itself. Pinned once; the rest of the file tests callers."""

    @pytest.mark.parametrize(
        ("qn", "expected"),
        [
            (MARKED, "Box"),
            (MARKED_WITH_COLUMN, "Box"),
            ("Box@7", "Box"),
            (VERBATIM, VERBATIM),
            (VERBATIM_LOCK, VERBATIM_LOCK),
            (VERBATIM_MARKED, VERBATIM),
            ("@event@12_4", VERBATIM),
            ("plain", "plain"),
        ],
    )
    def test_only_a_trailing_numeric_run_is_a_marker(
        self, qn: str, expected: str
    ) -> None:
        assert strip_dup_marker(qn) == expected

    def test_the_marker_grammar_matches_what_the_registry_writes(self) -> None:
        """Built from the producer's constants, so the two cannot drift.

        If `function_registry` ever changed its separator, this would fail
        rather than silently stop stripping.
        """
        produced = f"Box{cs.DUP_QN_MARKER}12{cs.DUP_QN_COLUMN_MARKER}5"
        assert produced == MARKED_WITH_COLUMN
        assert strip_dup_marker(produced) == "Box"

    def test_a_digit_only_name_is_not_rescued(self) -> None:
        """`@1` is stripped, and that is correct: no language here permits an
        identifier starting with a digit, so it cannot be a verbatim name."""
        assert strip_dup_marker("@1") == ""


class TestGraphUpdaterNaturalQn:
    """`graph_updater._natural_qn`, the reader the issue names first."""

    def test_a_marked_qn_still_loses_its_marker(self) -> None:
        from codebase_rag.graph_updater import _natural_qn

        assert _natural_qn("pkg.T.M@3") == "pkg.T.M"

    def test_a_verbatim_leaf_survives(self) -> None:
        """Was `pkg.` before the fix: the leaf was destroyed entirely."""
        from codebase_rag.graph_updater import _natural_qn

        assert _natural_qn("pkg.@event") == "pkg.@event"

    def test_a_bare_verbatim_name_survives(self) -> None:
        """Was `''` before the fix -- an empty qualified name."""
        from codebase_rag.graph_updater import _natural_qn

        assert _natural_qn(VERBATIM) == VERBATIM

    def test_a_duplicate_verbatim_type_keeps_its_name_and_loses_its_marker(
        self,
    ) -> None:
        """The case a first-`@` reader and a last-`@` reader both get wrong.

        Cutting at the first `@` yields `pkg.`; cutting at the last without
        checking for digits yields `pkg.@event` only by luck here, and
        `pkg.` again for `pkg.@event@12`.
        """
        from codebase_rag.graph_updater import _natural_qn

        assert _natural_qn("pkg.@event@12") == "pkg.@event"

    def test_the_reader_delegates_to_the_shared_helper(self) -> None:
        """Pins the PROPERTY (same answer as the helper), not the call shape.

        A reader that reimplemented the grammar correctly would pass; one
        that drifted from it would not. That is the thing worth protecting --
        issue #2017 is a family of readers that each spelled it themselves.
        """
        from codebase_rag.graph_updater import _natural_qn

        for qn in (MARKED, VERBATIM, VERBATIM_MARKED, "pkg.@event", "pkg.T.M@3"):
            assert _natural_qn(qn) == natural_qn(qn), qn
