"""A call site whose recorded position no longer holds a call is refused.

The no-grammar fallback in `_last_identifier` cuts at the last `(`, which on
`helper(helper(1))` picks the INNER callee; reached with a grammar because
the index is stale, it would rewrite the wrong token (bot review, #2163).
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.editing.rename import _STALE_CALL, _last_identifier

SOURCE = b"x = 0\ny = helper(helper(1))\n"
LANG = cs.SupportedLanguage.PYTHON


def test_a_valid_call_site_names_the_outer_callee() -> None:
    token = _last_identifier(SOURCE, 2, 4, 2, 21, "helper", LANG, is_call=True)
    assert token == (2, 4)


def test_a_stale_call_site_is_refused_not_guessed() -> None:
    # The recorded call no longer starts at (2, 0): `y` does.
    token = _last_identifier(SOURCE, 2, 0, 2, 21, "helper", LANG, is_call=True)
    assert token == _STALE_CALL


def test_a_reference_site_is_not_treated_as_a_stale_call() -> None:
    token = _last_identifier(SOURCE, 2, 0, 2, 21, "helper", LANG, is_call=False)
    assert token is not None
    assert token != _STALE_CALL
