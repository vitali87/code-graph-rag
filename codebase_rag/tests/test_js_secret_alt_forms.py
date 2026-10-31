"""JS/TS `hardcoded_secret` sees every way a name is bound to a literal.

The rule matched only `const`/`let`/`var` declarators, so a secret assigned
to a member (`this.token = "..."`), written as an object property
(`{ password: "..." }`) or as a class field (`api_key = "..."`) gave no
finding, while Python's rule flags `self.password = "..."` (issue #2866).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

HARDCODED_SECRET = "hardcoded_secret"
_FILES = ["app.js", "app.ts", "app.tsx"]


def _flagged(tmp_path: Path, file_name: str, src: str) -> list[int]:
    return sorted(
        int(p[cs.KEY_START_LINE])
        for p in _fire(tmp_path, file_name, src)
        if p[cs.KEY_NAME] == HARDCODED_SECRET
    )


@pytest.mark.parametrize("file_name", _FILES)
def test_the_issue_flags_every_form(tmp_path: Path, file_name: str) -> None:
    src = (
        "// app\n"
        'const password = "s3cr3t_value_long";\n'
        'let api_key = "s3cr3t_value_long";\n'
        "\n"
        "function setup() {\n"
        '  this.token = "s3cr3t_value_long";\n'
        '  globalThis.secret = "s3cr3t_value_long";\n'
        "}\n"
        "\n"
        'const config = { password: "s3cr3t_value_long" };\n'
        "\n"
        "class Client {\n"
        '  api_key = "s3cr3t_value_long";\n'
        "}\n"
    )
    assert _flagged(tmp_path, file_name, src) == [2, 3, 6, 7, 10, 13]


@pytest.mark.parametrize("file_name", _FILES)
def test_other_spellings_of_the_forms(tmp_path: Path, file_name: str) -> None:
    src = (
        "let db_password;\n"
        'db_password = "s3cr3t_value_long";\n'
        'settings["api_key"] = "s3cr3t_value_long";\n'
        'const headers = { "auth_token": "s3cr3t_value_long" };\n'
        'class K { static secret = "s3cr3t_value_long"; }\n'
    )
    assert _flagged(tmp_path, file_name, src) == [2, 3, 4, 5]


def test_a_typed_typescript_field_is_flagged(tmp_path: Path) -> None:
    src = 'class K {\n  private readonly token: string = "s3cr3t_value_long";\n}\n'
    assert _flagged(tmp_path, "app.ts", src) == [2]


@pytest.mark.parametrize("file_name", _FILES)
def test_what_is_not_a_secret_stays_clean(tmp_path: Path, file_name: str) -> None:
    # Negatives: the name and value guards hold for the new forms too.
    src = (
        'this.label = "s3cr3t_value_long";\n'
        'token.value = "s3cr3t_value_long";\n'
        'const a = { password: "" };\n'
        "const b = { password: getPassword() };\n"
        'this.password = "Hello {name}, welcome";\n'
        'class C { secret = "%s and %d"; }\n'
        'const c = { title: "s3cr3t_value_long" };\n'
    )
    assert _flagged(tmp_path, file_name, src) == []


@pytest.mark.parametrize("file_name", _FILES)
def test_a_template_literal_secret_is_flagged(tmp_path: Path, file_name: str) -> None:
    # A backtick literal with no substitution is as constant as a quoted one
    # (`const secret = \`sk_live_...\``); its node is a template_string, which
    # the rule did not accept (issue #2779).
    src = (
        "const secret = `s3cr3t_value_long`;\n"
        "this.token = `s3cr3t_value_long`;\n"
        "const c = { password: `s3cr3t_value_long` };\n"
    )
    assert _flagged(tmp_path, file_name, src) == [1, 2, 3]


@pytest.mark.parametrize("file_name", _FILES)
def test_a_template_with_a_substitution_is_not_a_secret(
    tmp_path: Path, file_name: str
) -> None:
    # Negative: `${...}` builds the value at run time.
    src = "const secret = `${prefix}_value_long`;\nthis.token = `Bearer ${jwt}`;\n"
    assert _flagged(tmp_path, file_name, src) == []
