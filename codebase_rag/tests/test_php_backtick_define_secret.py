"""PHP's backtick operator runs a shell; `define()` and `const` hold secrets.

`command_exec` matched only calls, so `` `$cmd` `` (the same as
`shell_exec($cmd)`) was missed, and `hardcoded_secret` matched only
`$x = "..."`, so `define('DB_PASSWORD', '...')`, how wp-config.php stores
its secrets, and `const API_KEY = '...'` were missed (issue #2868).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

COMMAND_EXEC = "command_exec"
HARDCODED_SECRET = "hardcoded_secret"


def _flagged(tmp_path: Path, src: str) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {COMMAND_EXEC: [], HARDCODED_SECRET: []}
    for p in _fire(tmp_path, "app.php", src):
        if p[cs.KEY_NAME] in found:
            found[p[cs.KEY_NAME]].append(int(p[cs.KEY_START_LINE]))
    return {name: sorted(lines) for name, lines in found.items()}


def test_the_issue_finds_every_sink(tmp_path: Path) -> None:
    src = (
        "<?php\n"
        "function run_system($cmd) {\n"
        "    system($cmd);\n"
        "}\n"
        "function run_backtick($cmd) {\n"
        "    return `$cmd`;\n"
        "}\n"
        '$password = "s3cr3t_long_value";\n'
        "define('DB_PASSWORD', 's3cr3t_long_value');\n"
        "const API_KEY = 's3cr3t_long_value';\n"
    )
    assert _flagged(tmp_path, src) == {
        COMMAND_EXEC: [3, 6],
        HARDCODED_SECRET: [8, 9, 10],
    }


def test_other_spellings_of_the_new_forms(tmp_path: Path) -> None:
    src = (
        "<?php\n"
        "$out = `ls -la {$dir}`;\n"
        'define("DB_PASSWORD", "s3cr3t_long_value");\n'
        "\\define('SECRET_KEY', 's3cr3t_long_value');\n"
        "class Config {\n"
        '    const SECRET = "s3cr3t_long_value";\n'
        "    public const DB_PASSWORD = 's3cr3t_long_value', DB_HOST = 'x';\n"
        "}\n"
    )
    assert _flagged(tmp_path, src) == {
        COMMAND_EXEC: [2],
        HARDCODED_SECRET: [3, 4, 6, 7],
    }


def test_what_is_not_a_secret_or_a_shell_stays_clean(tmp_path: Path) -> None:
    # Negatives: the existing name and value guards carry over unchanged.
    src = (
        "<?php\n"
        "define('DB_HOST', 'database.internal.example');\n"
        "define('DB_PASSWORD', '');\n"
        "define('DB_PASSWORD', getenv('DB_PASSWORD'));\n"
        "define('DB_PASSWORD', \"pass {$suffix} value\");\n"
        "const API_KEY = 'short';\n"
        "const LABEL = 'a long but harmless label';\n"
        "define('s3cr3t_long_value', 'DB_PASSWORD');\n"
        '$msg = "run `ls` to list files";\n'
        "my_define('DB_PASSWORD', 's3cr3t_long_value');\n"
    )
    assert _flagged(tmp_path, src) == {COMMAND_EXEC: [], HARDCODED_SECRET: []}
