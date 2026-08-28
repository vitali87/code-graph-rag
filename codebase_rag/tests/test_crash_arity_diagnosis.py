# Arity TypeError diagnosis (issue #227, Phase 1, one bullet).
#
# `rank_root_causes` ranks candidates by graph proximity. For an arity
# TypeError it does not need to rank at all: the message names the callee and
# both counts, and Python ingestion records each function's declared
# POSITIONAL parameters, so the mismatch is mechanically decidable.
#
# That last clause was false when this file was written -- it claimed the
# graph already knew every function's parameters, when no frontend stored any.
# The `positional_params` property was added to make it true. Other languages
# still store nothing and are answered with "cannot corroborate".
#
# The parser is tested against messages CPython actually emits, captured by
# running the failing calls rather than transcribed from memory:
#
#     take_two() takes 2 positional arguments but 3 were given
#     take_two() missing 1 required positional argument: 'b'
#     C.m() takes 2 positional arguments but 3 were given
#     only_kw() takes 0 positional arguments but 1 was given
#
# Note the third: a bound method reports 2 for ONE declared parameter,
# because `self` counts. Any rule comparing the message's number against a
# stored parameter count has to know that, and a fixture using only free
# functions would never reveal it.
from __future__ import annotations

import pytest

from codebase_rag.crash_correlation import diagnose_arity, parse_arity_error


class TestParsingRealMessages:
    """The two shapes CPython emits, and what must NOT parse."""

    def test_too_many_positional_arguments(self) -> None:
        parsed = parse_arity_error(
            "take_two() takes 2 positional arguments but 3 were given"
        )

        assert parsed is not None
        assert parsed.callee == "take_two"
        assert parsed.expected == 2
        assert parsed.actual == 3

    def test_a_single_extra_argument_uses_singular_was(self) -> None:
        """`1 was given`, not `1 were given` -- a real message CPython emits.

        Pinned because a regex written from the plural example alone silently
        fails here, and the failure mode is a missed diagnosis rather than a
        wrong one, which nothing downstream reports.
        """
        parsed = parse_arity_error(
            "only_kw() takes 0 positional arguments but 1 was given"
        )

        assert parsed is not None
        assert parsed.callee == "only_kw"
        assert parsed.expected == 0
        assert parsed.actual == 1

    def test_a_qualified_method_name_keeps_only_the_final_component(self) -> None:
        """`C.m()` names the method; the graph stores it under its own qn.

        Asserting the bare name rather than `C.m` because the caller matches
        against qualified names that already carry their own class prefix,
        and a `C.m` needle would fail to match `project.mod.C.m`.
        """
        parsed = parse_arity_error(
            "C.m() takes 2 positional arguments but 3 were given"
        )

        assert parsed is not None
        assert parsed.callee == "m"
        assert parsed.expected == 2
        assert parsed.actual == 3

    def test_missing_required_argument_is_the_other_direction(self) -> None:
        """Too FEW arguments is the same defect class and must also parse."""
        parsed = parse_arity_error(
            "take_two() missing 1 required positional argument: 'b'"
        )

        assert parsed is not None
        assert parsed.callee == "take_two"
        assert parsed.missing == ("b",)

    def test_several_missing_arguments_are_all_named(self) -> None:
        parsed = parse_arity_error(
            "f() missing 2 required positional arguments: 'b' and 'c'"
        )

        assert parsed is not None
        assert parsed.missing == ("b", "c")

    @pytest.mark.parametrize(
        "message",
        [
            "unsupported operand type(s) for +: 'int' and 'str'",
            "'NoneType' object is not subscriptable",
            "argument of type 'int' is not iterable",
            "",
            "takes 2 positional arguments but 3 were given",
        ],
    )
    def test_a_non_arity_typeerror_does_not_parse(self, message: str) -> None:
        """Most TypeErrors are not arity errors and must return None.

        The last case is the important one: a message with the arity SHAPE but
        no callee name. Returning a diagnosis with an empty callee would make
        every function in the graph a candidate match.
        """
        assert parse_arity_error(message) is None

    def test_a_message_with_trailing_text_does_not_parse(self) -> None:
        """The `$` is the load-bearing anchor, so trailing text must reject.

        Established by mutation rather than assumed, and the first version of
        this test was WRONG. It used a message with a LEADING prefix, which
        rejects for a different reason: `re.match` already anchors at the
        start, so both `^` and `.search` versions passed it and the test
        discriminated nothing.

        Removing `$` is what actually changes behaviour, and only a TRAILING
        suffix reveals it. Without the terminal anchor a message like this
        parses and yields a confident diagnosis whose counts come from a
        substring of a longer message.
        """
        trailing = (
            "helper() takes 2 positional arguments but 3 were given (during import)"
        )

        assert parse_arity_error(trailing) is None, (
            "a message with trailing text parsed; the terminal anchor is what "
            "keeps a substring from being read as the whole message"
        )

        # The control: the same message without the suffix DOES parse, so the
        # rejection above is the anchor rather than the pattern being inert.
        assert (
            parse_arity_error("helper() takes 2 positional arguments but 3 were given")
            is not None
        )


class TestDiagnosis:
    """Comparing the parsed message against declared parameters."""

    def test_a_free_function_mismatch_is_named_outright(self) -> None:
        from codebase_rag.crash_correlation import diagnose_arity

        parsed = parse_arity_error(
            "take_two() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=("a", "b"), is_method=False)

        assert verdict is not None
        assert verdict.declared_count == 2
        assert verdict.confirmed is True

    def test_a_method_counts_self_before_comparing(self) -> None:
        """`C.m(self, a)` reports "takes 2" for ONE declared parameter.

        The subtlety that makes this rule worth testing rather than assuming:
        comparing the message's 2 against a stored `("a",)` would report a
        mismatch on correct code, turning a diagnostic aid into a source of
        false accusations.
        """
        from codebase_rag.crash_correlation import diagnose_arity

        parsed = parse_arity_error(
            "C.m() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=("self", "a"), is_method=True)

        assert verdict is not None
        assert verdict.declared_count == 2
        assert verdict.confirmed is True, (
            "a method whose declared parameters include `self` was reported as "
            "disagreeing with a message that also counts `self`"
        )

    def test_a_method_whose_stored_params_omit_self_still_matches(self) -> None:
        """`is_method` must ADD the receiver when the store omits it.

        Found by mutation: removing the `declared_count += 1` branch left all
        13 other tests passing, because every method fixture stored `self`
        explicitly and the two implementations then agree. The branch only
        does work when the receiver is ABSENT from the stored parameters,
        which no other fixture exercises.

        This is the case that decides whether the branch is load-bearing or
        decorative, and without it the panel reports the branch as untested
        while the suite reports it as covered.
        """
        from codebase_rag.crash_correlation import diagnose_arity

        parsed = parse_arity_error(
            "C.m() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=("a",), is_method=True)

        assert verdict is not None
        assert verdict.declared_count == 2, (
            "a method storing only ('a',) was counted as 1; CPython counts the "
            "bound receiver, so the graph count must include it to compare"
        )
        assert verdict.confirmed is True

    def test_a_free_function_never_gains_a_phantom_receiver(self) -> None:
        """The paired control: `is_method=False` must not add anything.

        Without this, an implementation that always incremented would satisfy
        the test above while breaking every free-function diagnosis.
        """
        from codebase_rag.crash_correlation import diagnose_arity

        parsed = parse_arity_error(
            "take_two() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=("a", "b"), is_method=False)

        assert verdict is not None
        assert verdict.declared_count == 2
        assert verdict.confirmed is True

    def test_keyword_only_parameters_must_not_be_counted_as_positional(self) -> None:
        """`def only_kw(*, a)` declares one name and ZERO positional arguments.

        CPython reports "takes 0 positional arguments but 1 was given". Passing
        the bare names `("a",)` counts 1, so the verdict would be
        `confirmed=False` -- a false graph mismatch on CORRECT code, which is
        the same false accusation the `self` handling exists to prevent
        (reported on #1485).

        Names cannot distinguish the kinds: `(*, a)` and `(a)` both declare the
        name `a`. So the caller must pass POSITIONAL names only, and this test
        pins the contract from the correct side -- zero positional names give
        a confirming verdict.
        """
        parsed = parse_arity_error(
            "only_kw() takes 0 positional arguments but 1 was given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=(), is_method=False)

        assert verdict is not None
        assert verdict.declared_count == 0
        assert verdict.confirmed is True, (
            "a keyword-only function whose POSITIONAL parameters are correctly "
            "empty was reported as disagreeing with a message that says the "
            "same thing"
        )

    def test_unknown_parameter_kinds_decline_rather_than_accuse(self) -> None:
        """ "Cannot corroborate" and "disagrees" are different claims.

        The graph stores no parameter data today, so every in-repo caller is
        in this position. Returning `confirmed=False` with a real
        `declared_count` would read as the graph CONTRADICTING the traceback,
        which is a stronger statement than the data supports.

        The sentinel yields `declared_count=-1`, distinguishable from any real
        count, so a consumer cannot mistake it for a signature comparison that
        actually happened.
        """
        from codebase_rag.crash_correlation import _ARITY_KINDS_UNKNOWN

        parsed = parse_arity_error(
            "take_two() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=_ARITY_KINDS_UNKNOWN, is_method=False)

        assert verdict is not None
        assert verdict.declared_count == -1
        assert verdict.confirmed is False

        # The control: an EMPTY tuple is a real claim (no positional
        # parameters) and must NOT be treated as unknown.
        empty = diagnose_arity(parsed, declared=(), is_method=False)
        assert empty is not None
        assert empty.declared_count == 0

    def test_a_disagreeing_signature_is_reported_unconfirmed(self) -> None:
        """The graph's signature not matching the message is itself a finding.

        It means the resolved function is not the one that raised -- a stale
        index, or a same-named function elsewhere. Saying so beats silently
        confirming, which would attach a confident diagnosis to the wrong
        function.
        """
        from codebase_rag.crash_correlation import diagnose_arity

        parsed = parse_arity_error(
            "take_two() takes 2 positional arguments but 3 were given"
        )
        assert parsed is not None

        verdict = diagnose_arity(parsed, declared=("a", "b", "c"), is_method=False)

        assert verdict is not None
        assert verdict.declared_count == 3
        assert verdict.confirmed is False
