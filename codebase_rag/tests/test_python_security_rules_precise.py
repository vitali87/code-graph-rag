"""Issue #2691: the Python `yaml_load` and `sqli_fstring` rules fire on the
unsafe call only.

`yaml_load` matched every `yaml.load(...)`, including the `Loader=SafeLoader`
its own message asks for. `sqli_fstring` matched any call holding, anywhere
inside it, an `execute` identifier and an f-string: a keyword argument named
`execute`, an f-string beside a constant `cursor.execute("SELECT 1")`, and
every call enclosing a real `execute(f"...")`, which gave one finding per
enclosing call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ast_grep_py")

from codebase_rag import constants as cs  # noqa: E402
from codebase_rag.tests.test_ast_grep_analyzer import _fire  # noqa: E402


def _hits(tmp_path: Path, rule: str, src: str) -> list[int]:
    return sorted(
        int(p[cs.KEY_START_LINE])
        for p in _fire(tmp_path, "probe.py", src)
        if p[cs.KEY_NAME] == rule
    )


@pytest.mark.parametrize(
    "call",
    [
        "yaml.load(f, Loader=yaml.SafeLoader)",
        "yaml.load(f, Loader=yaml.CSafeLoader)",
        "yaml.load(f, Loader=SafeLoader)",
        "yaml.load(f, yaml.SafeLoader)",
    ],
)
def test_a_safe_loader_is_not_a_finding(tmp_path: Path, call: str) -> None:
    assert _hits(tmp_path, "yaml_load", f"import yaml\n{call}\n") == []


@pytest.mark.parametrize(
    "call",
    [
        'scheduler.submit(f"nightly-{name}", execute=True)',
        'log.info(f"checked {n} rows", cursor.execute("SELECT 1"))',
        'cursor.execute("SELECT 1", (f"{uid}",))',
        'cursor.execute("SELECT 1", ((f"{uid}",)))',
        'run(f"{x}", lambda: execute)',
    ],
)
def test_an_execute_without_an_f_string_argument_is_not_a_finding(
    tmp_path: Path, call: str
) -> None:
    assert _hits(tmp_path, "sqli_fstring", f"{call}\n") == []


def test_a_nested_execute_is_one_finding(tmp_path: Path) -> None:
    src = 'results.append(cursor.execute(f"SELECT * FROM t WHERE id = {uid}"))\n'

    assert _hits(tmp_path, "sqli_fstring", src) == [1]


def test_executemany_with_an_f_string_is_a_finding(tmp_path: Path) -> None:
    src = 'cursor.executemany(f"INSERT INTO {table} VALUES (?)", rows)\n'

    assert _hits(tmp_path, "sqli_fstring", src) == [1]


# Negative: what must still be found.


@pytest.mark.parametrize(
    "call",
    [
        "yaml.load(f)",
        "yaml.load(f, Loader=yaml.Loader)",
        "yaml.load(f, Loader=yaml.FullLoader)",
        "yaml.load(f, Loader=yaml.UnsafeLoader)",
    ],
)
def test_an_unsafe_load_is_still_a_finding(tmp_path: Path, call: str) -> None:
    assert _hits(tmp_path, "yaml_load", f"import yaml\n{call}\n") == [2]


@pytest.mark.parametrize(
    "call",
    [
        'cursor.execute(f"SELECT * FROM users WHERE id = {uid}")',
        'execute(f"DELETE FROM t WHERE id = {uid}")',
        'self.db.execute(f"SELECT {col} FROM t")',
        'get_cursor().execute(f"SELECT {col} FROM t", params)',
        'cursor.execute(f"SELECT * FROM users WHERE id = {uid}" " LIMIT 1")',
        'cursor.execute((f"SELECT * FROM users WHERE id = {uid}"))',
        'cursor.execute((f"SELECT {col} " "FROM t"), params)',
    ],
)
def test_execute_with_an_f_string_is_still_a_finding(tmp_path: Path, call: str) -> None:
    assert _hits(tmp_path, "sqli_fstring", f"{call}\n") == [1]
