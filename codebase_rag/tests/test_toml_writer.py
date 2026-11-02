"""Workspace TOML, written by the Rust extension.

`toml_writer.dumps` is the Rust writer (rust/cgr_graph_rs/src/formats/toml.rs),
which replaced tomli-w with no Python fallback, so it must keep writing what
tomli-w wrote: a workspace file saved before is rewritten unchanged. The
pinned texts below are tomli-w 1.2.0's, and every random table must read
back through the standard library's `tomllib` as the table it was.
"""

from __future__ import annotations

import random
import string
import tomllib
from pathlib import Path
from types import MappingProxyType

import pytest

from codebase_rag.utils.toml_writer import dumps
from codebase_rag.workspaces.models import WorkspaceConfig, WorkspaceRepo
from codebase_rag.workspaces.storage import load_workspace, save_workspace

_FUZZ_SEED = 2026
_FUZZ_CASES = 500
_LONG_PATH = "/very/long/path/" + "x" * 80


def _workspace(*repos: tuple[str, str, bool], description: str = "") -> dict:
    return {
        "workspace": {
            "name": "demo",
            "description": description,
            "repos": [
                {"path": path, "project_name": name, "project_named": named}
                for path, name, named in repos
            ],
        }
    }


_PINNED = {
    "inline-repos": (
        _workspace(("/r/a", "a", True), ("/r/b", "b", False)),
        '[workspace]\nname = "demo"\ndescription = ""\nrepos = [\n'
        '    { path = "/r/a", project_name = "a", project_named = true },\n'
        '    { path = "/r/b", project_name = "b", project_named = false },\n]\n',
    ),
    "sectioned-repos": (
        _workspace(
            (_LONG_PATH, "a", True),
            ("/r/b", "b", False),
            description='two\nlines "q" \\ \t\x01\x7f é',
        ),
        '[workspace]\nname = "demo"\n'
        'description = "two\\nlines \\"q\\" \\\\ \t\\u0001\\u007f é"\n\n'
        f'[[workspace.repos]]\npath = "{_LONG_PATH}"\nproject_name = "a"\n'
        "project_named = true\n\n"
        '[[workspace.repos]]\npath = "/r/b"\nproject_name = "b"\n'
        "project_named = false\n",
    ),
    "no-repos": (
        _workspace(),
        '[workspace]\nname = "demo"\ndescription = ""\nrepos = []\n',
    ),
    "keys-arrays-tables": (
        {
            "a b": 1,
            "": -2,
            "ok-key_1": [1, [2, 3], {}],
            "nested": {"t": {"x": True}},
        },
        '"a b" = 1\n"" = -2\nok-key_1 = [\n    1,\n    [\n        2,\n'
        "        3,\n    ],\n    {},\n]\n\n[nested.t]\nx = true\n",
    ),
    "numbers-tuples-mappings": (
        {
            "f": 0.5,
            "big": 2**64,
            "e": 1e16,
            "t": ("a", 1),
            "m": MappingProxyType({"k": "v"}),
        },
        'f = 0.5\nbig = 18446744073709551616\ne = 1e+16\nt = [\n    "a",\n    1,\n]\n\n'
        '[m]\nk = "v"\n',
    ),
}


@pytest.mark.parametrize("case", list(_PINNED))
def test_it_writes_what_tomli_w_wrote(case: str) -> None:
    table, expected = _PINNED[case]
    assert dumps(table) == expected


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"when": object()}, "Object of type 'object' is not TOML serializable"),
        ({1: "x"}, "Invalid mapping key '1' of type 'int'. A string is required."),
        ({"s": {1, 2}}, "Object of type 'set' is not TOML serializable"),
    ],
)
def test_what_tomli_w_refused_raises_the_same_type_error(
    table: dict, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        dumps(table)


def _text(rng: random.Random) -> str:
    alphabet = (
        string.ascii_letters
        + string.digits
        + " -_./\"'\\\t\n\r\x00\x1f\x7féü✓\U0001f600"
    )
    return "".join(rng.choice(alphabet) for _ in range(rng.choice((0, 1, 5, 30, 90))))


def _value(rng: random.Random, depth: int) -> object:
    match rng.randrange(8 if depth < 3 else 5):
        case 0:
            return _text(rng)
        case 1:
            return rng.choice((True, False))
        case 2:
            return rng.choice((0, -1, 7, 2**63 - 1, -(2**63), 2**80))
        case 3:
            return rng.choice((0.0, -1.5, 1e300, 3.14159))
        case 4:
            return rng.choice((_text(rng), rng.randrange(100)))
        case 5:
            return [_value(rng, depth + 1) for _ in range(rng.randrange(4))]
        case 6:
            return [_table(rng, depth + 1) for _ in range(rng.randint(1, 3))]
        case _:
            return _table(rng, depth + 1)


def _table(rng: random.Random, depth: int) -> dict[str, object]:
    return {_text(rng): _value(rng, depth) for _ in range(rng.randrange(4))}


def test_random_tables_read_back_as_written() -> None:
    rng = random.Random(_FUZZ_SEED)
    for _ in range(_FUZZ_CASES):
        table = _table(rng, 0)
        assert tomllib.loads(dumps(table)) == table, table


def test_a_saved_workspace_loads_back(tmp_path: Path) -> None:
    config = WorkspaceConfig(
        name="demo",
        description='quotes " and \\ and\nnewlines',
        repos=[
            WorkspaceRepo(path="/r/a", project_name="a"),
            WorkspaceRepo(path=_LONG_PATH, project_name="b", project_named=False),
        ],
    )
    path = save_workspace(config, home=tmp_path)
    assert load_workspace("demo", home=tmp_path) == config
    assert path.read_text(encoding="utf-8") == dumps({"workspace": config.model_dump()})
