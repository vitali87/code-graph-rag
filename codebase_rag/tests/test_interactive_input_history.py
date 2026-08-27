# Up arrow must recall the previous message in interactive chat (issue #1495).
#
# Two independent causes, either of which alone breaks recall:
#
# 1. `get_multiline_input` called the BARE `prompt()` function. Its own
#    docstring says "This will create a new PromptSession", and it passes
#    `history=None`, so every turn got a fresh empty history. Nothing to
#    recall, no matter which key was pressed.
#
# 2. The input is genuinely multiline -- Enter inserts a newline, Ctrl+J
#    and Ctrl+E submit -- and in a multiline buffer the up arrow moves the
#    CURSOR rather than walking history.
#
# Fixing only one leaves the bug: a persistent history nothing navigates to,
# or a navigation binding over a history that is always empty.
from __future__ import annotations

import pytest
from prompt_toolkit.history import InMemoryHistory

from codebase_rag import constants as cs


@pytest.fixture(autouse=True)
def _fresh_session() -> object:
    """The session is process-wide by design, so tests must not inherit it.

    Without this a history entry written by one test leaks into the next,
    and the suite passes or fails depending on execution order.
    """
    from codebase_rag.main import _input_history, _input_session

    _input_session.cache_clear()
    _input_history.cache_clear()
    yield
    _input_session.cache_clear()
    _input_history.cache_clear()


class TestHistoryPersists:
    """Cause 1: the session, and therefore the history, must outlive a turn."""

    def test_the_history_is_reused_across_calls(self) -> None:
        """A fresh history per turn cannot remember the previous turn.

        Asserts the SAME object comes back, not merely that one exists:
        constructing a new history each call would satisfy a weaker
        "is not None" check while preserving the bug exactly.

        Exercised through `_input_history` rather than `_input_session`
        because building a real `PromptSession` attaches to the console,
        which hangs a headless Windows runner until the job times out.
        Persistence is the property under test; the prompt is not.
        """
        from codebase_rag.main import _input_history

        assert _input_history() is _input_history()

    def test_an_earlier_entry_is_still_there_on_a_later_turn(self) -> None:
        """What the user actually experiences: recall across turns.

        Deliberately NOT asserting `history is not None` --
        prompt_toolkit supplies an `InMemoryHistory` by default, so that
        assertion is true of a correct wiring AND of one that passes no
        history at all. It reads like a check and tests nothing.

        Writing on one lookup and reading on a second is what actually
        distinguishes a persistent history from a per-turn one.
        """
        from codebase_rag.main import _input_history, _remember_input

        _remember_input(_input_history(), "asked on an earlier turn")

        assert "asked on an earlier turn" in list(_input_history().get_strings())

    def test_the_session_is_wired_to_that_history(self) -> None:
        """The seam must not drift from the prompt that reads it.

        Splitting history out removes the console dependency from the two
        tests above, but it also creates a way for them to pass while the
        SESSION reads some other history -- so this asserts the wiring,
        and is the one test here that builds a real prompt.
        """
        from codebase_rag.main import _input_history, _input_session

        assert _input_session().history is _input_history()

    def test_submitted_text_is_appended_to_history(self) -> None:
        """The history must actually be fed, not merely exist.

        A session can hold a perfectly good empty history forever if
        nothing ever appends to it -- which is what the bare `prompt()`
        call produced.
        """
        from codebase_rag.main import _remember_input

        history = InMemoryHistory()
        _remember_input(history, "first question")
        _remember_input(history, "second question")

        assert list(history.get_strings()) == ["first question", "second question"]

    def test_blank_input_is_not_remembered(self) -> None:
        """Recalling a blank line is worse than useless.

        Without this, holding Ctrl+J on an empty buffer fills the history
        with blanks and pushes the real previous message out of easy reach.
        """
        from codebase_rag.main import _remember_input

        history = InMemoryHistory()
        _remember_input(history, "real question")
        _remember_input(history, "   ")
        _remember_input(history, "")

        assert list(history.get_strings()) == ["real question"]

    def test_a_repeated_question_is_not_duplicated(self) -> None:
        """Asking the same thing twice should not need two up-arrows."""
        from codebase_rag.main import _remember_input

        history = InMemoryHistory()
        _remember_input(history, "same")
        _remember_input(history, "same")

        assert list(history.get_strings()) == ["same"]


class TestUpArrowIsBound:
    """Cause 2: in a multiline buffer the arrow keys move the cursor."""

    def test_up_and_down_are_bound(self) -> None:
        """The binding must exist, or history is unreachable by arrow key."""
        from codebase_rag.main import _input_keybindings

        # `.value`, not `str(k)`: prompt_toolkit renders a key as
        # "Keys.Up" while the comparable spelling is "up".
        bound = {
            key
            for binding in _input_keybindings().bindings
            for key in (getattr(k, "value", str(k)) for k in binding.keys)
        }

        assert cs.KeyBinding.UP in bound
        assert cs.KeyBinding.DOWN in bound

    def test_the_existing_bindings_are_untouched(self) -> None:
        """The control: adding history navigation must not drop submit.

        Rebuilding the binding set is exactly the kind of edit that
        silently loses a key, and losing Ctrl+J makes the prompt
        impossible to submit -- a total outage with no error attached.
        """
        from codebase_rag.main import _input_keybindings

        # `.value`, not `str(k)`: prompt_toolkit renders a key as
        # "Keys.Up" while the comparable spelling is "up".
        bound = {
            key
            for binding in _input_keybindings().bindings
            for key in (getattr(k, "value", str(k)) for k in binding.keys)
        }

        for required in (
            cs.KeyBinding.CTRL_J,
            cs.KeyBinding.CTRL_E,
            cs.KeyBinding.CTRL_C,
            cs.KeyBinding.SHIFT_TAB,
        ):
            assert required in bound, required

        # Enter normalises to ControlM ("c-m") -- the same physical key, so
        # asserting the literal "enter" would fail against a correct
        # binding set.
        assert "c-m" in bound


class TestMultilineEditingStillWorks:
    """Up arrow must not steal cursor movement inside a multiline draft.

    The whole point of this prompt is multiline input, so binding the up
    arrow unconditionally to history would trade one bug for a worse one:
    a half-written three-line question would become uneditable.
    """

    @staticmethod
    def _press_up(text: str, cursor_row: int) -> tuple[str, int]:
        """Invoke the REAL up-arrow handler and report what it did.

        An earlier version of this test drove a bare `Buffer` and asserted
        on `cursor_position_row`, which never called the binding at all --
        it would have passed against any implementation, including one that
        always navigates history. Reaching the handler is the whole point.
        """
        from unittest.mock import MagicMock

        from prompt_toolkit.buffer import Buffer

        from codebase_rag.main import _input_keybindings

        buffer = Buffer()
        buffer.text = text
        lines = text.split("\n")
        buffer.cursor_position = sum(len(line) + 1 for line in lines[:cursor_row])

        moved: list[str] = []
        buffer.history_backward = lambda count=1: moved.append("history")
        buffer.cursor_up = lambda count=1: moved.append("cursor")

        handler = next(
            b.handler
            for b in _input_keybindings().bindings
            if any(getattr(k, "value", None) == cs.KeyBinding.UP for k in b.keys)
        )
        event = MagicMock()
        event.current_buffer = buffer
        event.arg = 1
        handler(event)

        return (moved[0] if moved else "nothing"), len(moved)

    @pytest.mark.parametrize(
        "text,cursor_row,expected",
        [
            ("single line", 0, "history"),
            # Row 0 of a MULTILINE draft still recalls: there is nowhere to
            # move up to, so the arrow would otherwise do nothing at all.
            ("line one\nline two", 0, "history"),
            ("line one\nline two", 1, "cursor"),
            ("a\nb\nc", 1, "cursor"),
            ("a\nb\nc", 2, "cursor"),
        ],
    )
    def test_up_recalls_only_from_the_first_row(
        self, text: str, cursor_row: int, expected: str
    ) -> None:
        """Below the first row the arrow must move the cursor, not recall.

        Binding it unconditionally would make a half-written multiline
        question uneditable -- trading the reported bug for a worse one.
        """
        action, count = self._press_up(text, cursor_row)

        assert action == expected
        assert count == 1
