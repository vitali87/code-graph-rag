"""`hardcoded_secret` needs a credential-named variable, not a substring.

Every language's rule tested the target name with `(password|secret|api_key|
token)` anywhere in it, so `tokenizer = "bert-base-uncased"`, a
`password_prompt` UI string or a `token_count` was reported as a hardcoded
secret (issue #2870). A name holds a credential when the credential word
ends it, optionally followed by key words (`SECRET_KEY`, `secret_key_base`,
`AWS_SECRET_ACCESS_KEY`) and digits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

HARDCODED_SECRET = "hardcoded_secret"
_VALUE = "sk_live_realcredentialvalue"

# The issue's names: each only contains a credential word.
_NOT_CREDENTIALS = [
    "tokenizer",
    "token_count",
    "password_prompt",
    "secret_santa_name",
    "api_key_header_name",
    "tokenCount",
    "secretary",
]

# Names that hold a credential, in every casing the rules saw before.
_CREDENTIALS = [
    "api_key",
    "db_password",
    "authToken",
    "API_SECRET",
    "SECRET_KEY",
    "secret_key_base",
    "AWS_SECRET_ACCESS_KEY",
    "password2",
    "githubtoken",
]

# file name, text before the lines, one line per name, text after.
_LANGUAGES: dict[str, tuple[str, str, str, str]] = {
    "python": ("s.py", "", '{name} = "{value}"', ""),
    "javascript": ("s.js", "", 'const {name} = "{value}";', ""),
    "typescript": ("s.ts", "", 'const {name} = "{value}";', ""),
    "tsx": ("s.tsx", "", 'const {name} = "{value}";', ""),
    "java": ("S.java", "class S {\n", '  String {name} = "{value}";', "}\n"),
    "csharp": ("S.cs", "class S {\n", '  string {name} = "{value}";', "}\n"),
    "go": ("s.go", "package main\n", 'var {name} = "{value}"', ""),
    "c": ("s.c", "", 'const char *{name} = "{value}";', ""),
    "cpp": ("s.cpp", "", 'const char *{name} = "{value}";', ""),
    "rust": ("s.rs", "fn f() {\n", '    let {name} = "{value}";', "}\n"),
    "php": ("s.php", "<?php\n", '${name} = "{value}";', ""),
    "ruby": ("s.rb", "", '{name} = "{value}"', ""),
    "lua": ("s.lua", "", 'local {name} = "{value}"', ""),
    "dart": ("s.dart", "void f() {\n", '  var {name} = "{value}";', "}\n"),
    "scala": ("S.scala", "object S {\n", '  val {name} = "{value}"', "}\n"),
}


def _flagged_names(tmp_path: Path, language: str, names: list[str]) -> list[str]:
    file_name, head, line, tail = _LANGUAGES[language]
    body = "\n".join(line.format(name=n, value=_VALUE) for n in names)
    first = head.count("\n") + 1
    flagged = {
        int(p[cs.KEY_START_LINE])
        for p in _fire(tmp_path, file_name, f"{head}{body}\n{tail}")
        if p[cs.KEY_NAME] == HARDCODED_SECRET
    }
    return [n for i, n in enumerate(names) if first + i in flagged]


@pytest.mark.parametrize("language", sorted(_LANGUAGES))
def test_a_name_containing_a_credential_word_is_not_one(
    tmp_path: Path, language: str
) -> None:
    assert _flagged_names(tmp_path, language, _NOT_CREDENTIALS) == []


@pytest.mark.parametrize("language", sorted(_LANGUAGES))
def test_a_credential_name_is_still_flagged(tmp_path: Path, language: str) -> None:
    # Negative: every credential spelling the old substring caught still fires.
    assert _flagged_names(tmp_path, language, _CREDENTIALS) == _CREDENTIALS
