from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from codebase_rag import constants as cs
from codebase_rag.main import get_multiline_input

CTRL_J = "\x0a"
CTRL_E = "\x05"
CTRL_C = "\x03"
ENTER = "\r"


def _run_with_input(text: str) -> str:
    with create_pipe_input() as inp:
        inp.send_text(text)
        with create_app_session(input=inp, output=DummyOutput()):
            return get_multiline_input("Ask")


def test_ctrl_j_submits_buffer() -> None:
    assert _run_with_input(f"hello{CTRL_J}") == "hello"


def test_ctrl_e_submits_buffer() -> None:
    assert _run_with_input(f"hello{CTRL_E}") == "hello"


def test_ctrl_e_submits_after_multiline_with_enter() -> None:
    assert _run_with_input(f"line1{ENTER}line2{CTRL_E}") == "line1\nline2"


def test_ctrl_j_submits_after_multiline_with_enter() -> None:
    assert _run_with_input(f"line1{ENTER}line2{CTRL_J}") == "line1\nline2"


def test_result_is_stripped() -> None:
    assert _run_with_input(f"  padded  {CTRL_E}") == "padded"


def test_ctrl_c_raises_keyboard_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        _run_with_input(f"abc{CTRL_C}")


def test_keybinding_enum_has_submit_shortcuts() -> None:
    assert cs.KeyBinding.CTRL_J.value == "c-j"
    assert cs.KeyBinding.CTRL_E.value == "c-e"


def test_hint_mentions_both_submit_shortcuts() -> None:
    assert "Ctrl+J" in cs.MULTILINE_INPUT_HINT
    assert "Ctrl+E" in cs.MULTILINE_INPUT_HINT
