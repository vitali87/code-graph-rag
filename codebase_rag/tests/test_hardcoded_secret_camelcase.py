"""Every language's hardcoded_secret rule knows the camelCase credential names.

The credential-word list was copy-pasted per language and broadened only for
Go and C#. The other thirteen packs kept `(password|secret|api_key|token)`,
which has no `apikey` spelling, so `apiKey`, the standard spelling in JS, TS,
Java, Dart and Scala, was flagged in Go and missed everywhere else, and so
were `accessKey` and `passwd` (issue #2779).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codebase_rag.tests.test_hardcoded_secret_name_boundary import (
    _LANGUAGES,
    _flagged_names,
)

pytest.importorskip("ast_grep_py")

_CAMEL_CREDENTIALS = [
    "apiKey",
    "stripeApiKey",
    "APIKEY",
    "accessKey",
    "awsAccessKey",
    "passwd",
    "dbPasswd",
]

# Each continues past a credential word into a non-credential one.
_NOT_CREDENTIALS = [
    "passwordLabel",
    "passwordHint",
    "tokenizerName",
    "apiKeyHeader",
    "accessKeyId",
]


@pytest.mark.parametrize("language", sorted(_LANGUAGES))
def test_a_camel_case_credential_is_flagged(tmp_path: Path, language: str) -> None:
    assert _flagged_names(tmp_path, language, _CAMEL_CREDENTIALS) == _CAMEL_CREDENTIALS


@pytest.mark.parametrize("language", sorted(_LANGUAGES))
def test_a_name_that_continues_past_the_word_is_not(
    tmp_path: Path, language: str
) -> None:
    # Negative: the end-of-name boundary of issue #2870 holds for the new
    # spellings too.
    assert _flagged_names(tmp_path, language, _NOT_CREDENTIALS) == []


_RULES_DIR = (
    Path(__file__).resolve().parents[1] / "analyzers" / "ast_grep_rules" / "security"
)
_WORDS = "(?i)(password|passwd|secret|api_?key|access_?key|token)"


def test_every_pack_shares_one_credential_word_list() -> None:
    # The lists drifted apart because each pack carried its own copy; every
    # credential-name regex now starts with the same one.
    regexes = re.findall(r"regex: '(\(\?i\)\(password[^']*)'", _all_rules())
    assert regexes, "no credential-name regex found"
    assert {r[: len(_WORDS)] for r in regexes} == {_WORDS}, regexes


def _all_rules() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(_RULES_DIR.glob("*.yaml"))
    )
