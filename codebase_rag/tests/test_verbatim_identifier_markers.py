"""A C# verbatim identifier is not a duplicate marker (issue #2017).

`function_registry` appends `@<line>` (and `_<col>`) to disambiguate
definitions that share a qualified name. C# spells a keyword-colliding name
with a leading `@` -- `@event`, `@lock` -- so a reader that cuts at the first
`@` destroys the name instead of stripping a marker.

The tests prefer driving a SHIPPED reader over driving the helper, because a
helper that is correct in isolation says nothing about a caller that never
invokes it -- the gap that let this bug survive #2014's partial fix.

Three of the ten routed readers are reachable without graph fixtures and are
tested behaviourally (`graph_updater._natural_qn`,
`call_processor._scope_qn_candidates`, `dead_code._is_root`). The remaining
five are methods on parser classes whose fixtures would cost far more than
the assertion is worth; `TestEveryRoutedReaderUsesTheHelper` is a structural
backstop for those, and is deliberately the weakest thing here.
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

    def test_the_reader_delegates_to_the_shared_helper_by_result(self) -> None:
        """Pins the PROPERTY (same answer as the helper), not the call shape.

        A reader that reimplemented the grammar correctly would pass; one
        that drifted from it would not. That is the thing worth protecting --
        issue #2017 is a family of readers that each spelled it themselves.
        """
        from codebase_rag.graph_updater import _natural_qn

        for qn in (MARKED, VERBATIM, VERBATIM_MARKED, "pkg.@event", "pkg.T.M@3"):
            assert _natural_qn(qn) == natural_qn(qn), qn


class TestScopeCandidateReaders:
    """`call_processor._scope_qn_candidates`, driven directly.

    The early-exit guard these sites used (`MARKER not in last`) is TRUE for a
    verbatim identifier, so before the fix a verbatim scope produced a second
    candidate that was the corrupted name. Observable in the return value.
    """

    def test_a_verbatim_scope_yields_no_second_candidate(self) -> None:
        """Was `['pkg.@event', 'pkg.']`: the extra candidate was corrupt."""
        from codebase_rag.parsers.call_processor import _scope_qn_candidates

        assert _scope_qn_candidates("pkg.@event") == ["pkg.@event"]

    def test_a_marked_scope_still_yields_its_natural_form(self) -> None:
        """The behaviour the guard exists for must survive the fix."""
        from codebase_rag.parsers.call_processor import _scope_qn_candidates

        assert _scope_qn_candidates("pkg.useStore@7") == [
            "pkg.useStore@7",
            "pkg.useStore",
        ]

    def test_a_marked_verbatim_scope_yields_the_written_name(self) -> None:
        """Both at once: the marker goes, the verbatim name stays."""
        from codebase_rag.parsers.call_processor import _scope_qn_candidates

        assert _scope_qn_candidates("pkg.@event@12") == [
            "pkg.@event@12",
            "pkg.@event",
        ]

    def test_an_unmarked_scope_is_unchanged(self) -> None:
        from codebase_rag.parsers.call_processor import _scope_qn_candidates

        assert _scope_qn_candidates("pkg.plain") == ["pkg.plain"]


class TestDeadCodeRootLeaf:
    """`dead_code._is_root`, whose name-scoped rules read the stripped leaf.

    The issue's own provenance case: a SECOND Go `init()` in one file
    registers as `init@51` and was reported dead because the leaf no longer
    matched `GO_ROOT_FUNCTION_NAMES`.
    """

    @staticmethod
    def _is_root(qn: str) -> bool:
        from codebase_rag import constants as cs
        from codebase_rag.dead_code import DeadCodeConfig, _is_root

        config = DeadCodeConfig(
            include_tests=False,
            include_classes=False,
            root_decorators=frozenset(),
            entry_points=(),
            test_patterns=(),
            exclude_patterns=(),
            min_resolution=None,
            endpoint_roots=False,
        )
        return _is_root(
            qn,
            {cs.KEY_PATH: "pkg/register.go"},
            config,
            set(),
            set(),
            {},
            {},
            set(),
            set(),
            set(),
            {},
            "proj",
            None,
        )

    def test_a_duplicate_go_init_is_still_a_root(self) -> None:
        """The pre-existing behaviour, pinned so the fix cannot regress it."""
        assert self._is_root("pkg.init@51") is True

    def test_an_unduplicated_go_init_is_a_root(self) -> None:
        assert self._is_root("pkg.init") is True

    def test_an_ordinary_function_is_not_a_root(self) -> None:
        """The control: without it, a rule that returned True for everything
        would satisfy both assertions above."""
        assert self._is_root("pkg.other") is False


class TestEveryRoutedReaderUsesTheHelper:
    """A structural backstop for the readers that need real graph state.

    Five of the routed sites are methods on parser classes whose fixtures
    cost far more than the assertion is worth. This pins the property that
    matters for those: the module reaches the shared grammar and no longer
    carries the broken spelling. It is deliberately weaker than the
    behavioural tests above and does not replace them.
    """

    MODULES = (
        "codebase_rag.graph_updater",
        "codebase_rag.dead_code",
        "codebase_rag.parsers.call_processor",
        "codebase_rag.parsers.call_resolver",
        "codebase_rag.parsers.class_ingest.method_override",
        "codebase_rag.parsers.class_ingest.mixin",
        "codebase_rag.parsers.function_ingest",
        "evals.cgr_graph",
    )

    @pytest.mark.parametrize("module_name", MODULES)
    def test_no_module_still_cuts_at_the_first_marker(self, module_name: str) -> None:
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(module_name))
        # Written without the string itself appearing as code, so this test
        # cannot match its own assertion text.
        broken = f"split(cs.{'DUP_QN_MARKER'}, 1)[0]"
        assert broken not in source, (
            f"{module_name} still cuts at the first `@`, which empties a C# "
            "verbatim identifier (issue #2017)"
        )

    @pytest.mark.parametrize("module_name", MODULES)
    def test_every_module_reaches_the_shared_grammar(self, module_name: str) -> None:
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(module_name))
        assert "qn_markers." in source, (
            f"{module_name} does not route through the shared helper, so its "
            "marker grammar can drift from the producer's"
        )
