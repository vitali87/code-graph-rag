"""Python injection rules see `str.format()` queries and `os.system` f-strings.

`sqli_concat` and `sqli_fstring` covered `+`, `%` and f-string queries, and
`os_system_concat` only `os.system("..." + x)`, so `"... {}".format(u)`
passed to `execute()` or `os.system()`, and `os.system(f"ping {x}")`, gave
no finding: the same injection slipped through depending on how the string
was built (issue #2863).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

_INJECTION = (
    "sqli_concat",
    "sqli_fstring",
    "sqli_format",
    "os_system_concat",
    "os_system_fstring",
    "os_system_format",
)

_ISSUE = """\
import os

def sql_concat(cur, u):   return cur.execute("SELECT * FROM t WHERE id=" + u)
def sql_percent(cur, u):  return cur.execute("SELECT * FROM t WHERE id=%s" % u)
def sql_fstring(cur, u):  return cur.execute(f"SELECT * FROM t WHERE id={u}")
def sql_format(cur, u):   return cur.execute("SELECT * FROM t WHERE id={}".format(u))

def sys_concat(x):        os.system("ping " + x)
def sys_fstring(x):       os.system(f"ping {x}")
def sys_format(x):        os.system("ping {}".format(x))
"""


def _findings(tmp_path: Path, src: str) -> list[tuple[int, str]]:
    return sorted(
        (int(p[cs.KEY_START_LINE]), str(p[cs.KEY_NAME]))
        for p in _fire(tmp_path, "v.py", src)
        if p[cs.KEY_NAME] in _INJECTION
    )


def test_the_issue_flags_every_sink(tmp_path: Path) -> None:
    assert _findings(tmp_path, _ISSUE) == [
        (3, "sqli_concat"),
        (4, "sqli_concat"),
        (5, "sqli_fstring"),
        (6, "sqli_format"),
        (8, "os_system_concat"),
        (9, "os_system_fstring"),
        (10, "os_system_format"),
    ]


@pytest.mark.parametrize(
    ("statement", "rule"),
    [
        (
            'cur.executemany("INSERT INTO t VALUES ({})".format(row), rows)',
            "sqli_format",
        ),
        ('cur.execute("SELECT {id}".format(id=u))', "sqli_format"),
        ('cur.execute("SELECT {}".format(*parts))', "sqli_format"),
        ('cur.execute(("SELECT {} FROM t".format(table)))', "sqli_format"),
        ('cur.execute("SELECT {} " "FROM t".format(col))', "sqli_format"),
        ('cur.execute("SELECT {}".format(req.args["id"]))', "sqli_format"),
        ('os.system(f"tar czf {name}.tgz " "data")', "os_system_fstring"),
        ('os.system("rm -rf {}".format(path.name))', "os_system_format"),
    ],
)
def test_other_spellings_are_flagged(tmp_path: Path, statement: str, rule: str) -> None:
    src = f"import os\n\ndef f(cur, u, row, rows, parts, table, col, req, name, path):\n    {statement}\n"
    assert _findings(tmp_path, src) == [(4, rule)]


@pytest.mark.parametrize(
    "statement",
    [
        'cur.execute("SELECT {}".format(5))',
        'cur.execute("SELECT {id}".format(id="x"))',
        'cur.execute("SELECT * FROM t WHERE id=?", ["{}".format(u)])',
        "cur.execute(query.format(u))",
        'log("SELECT {}".format(u))',
        'os.system(f"ping localhost")',
        'os.system(build("ping {}".format(u)))',
        'os.system("ping {}".format("localhost"))',
    ],
)
def test_constant_or_indirect_strings_stay_clean(
    tmp_path: Path, statement: str
) -> None:
    # Negatives: a constant formatted string, a value formatted into the
    # bound parameters, a receiver that is not a literal, another callee, and
    # a string built inside a nested call are not this sink's query.
    src = f"import os\n\ndef f(cur, u, query, log, build):\n    {statement}\n"
    assert _findings(tmp_path, src) == []
