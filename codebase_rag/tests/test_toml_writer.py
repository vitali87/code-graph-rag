"""Workspace TOML, written by the Rust writer and by the reference writer.

`toml_writer.dumps` takes the Rust extension's text
(rust/cgr_graph_rs/src/formats/toml.rs) whenever it gives one, so both must
write the same characters; the Rust writer declines (None) any value it does
not handle, and the reference then writes it. Both must also keep writing
what tomli-w wrote, so a workspace file saved before cgr dropped tomli-w
is rewritten unchanged: the pinned texts below are tomli-w 1.2.0's.
"""

from __future__ import annotations

import random
import string
import tomllib
from pathlib import Path

import pytest

from codebase_rag.utils import toml_writer
from codebase_rag.utils.toml_writer import dumps, dumps_python
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
}


@pytest.mark.parametrize("case", list(_PINNED))
def test_the_reference_writes_what_tomli_w_wrote(case: str) -> None:
    table, expected = _PINNED[case]
    assert dumps_python(table) == expected


def test_unsupported_values_and_keys_raise_type_error() -> None:
    with pytest.raises(TypeError):
        dumps_python({"when": object()})
    with pytest.raises(TypeError):
        dumps_python({1: "x"})  # type: ignore[dict-item]


def _text(rng: random.Random) -> str:
    alphabet = (
        string.ascii_letters
        + string.digits
        + " -_./\"'\\\t\n\r\x00\x1f\x7féü✓\U0001f600"
    )
    return "".join(rng.choice(alphabet) for _ in range(rng.choice((0, 1, 5, 30, 90))))


def _value(rng: random.Random, depth: int) -> object:
    kind = rng.randrange(7 if depth < 3 else 4)
    match kind:
        case 0:
            return _text(rng)
        case 1:
            return rng.choice((True, False))
        case 2:
            return rng.choice((0, -1, 7, 2**63 - 1, -(2**63)))
        case 3:
            return rng.choice((_text(rng), rng.randrange(100)))
        case 4:
            return [_value(rng, depth + 1) for _ in range(rng.randrange(4))]
        case 5:
            return [_table(rng, depth + 1) for _ in range(rng.randint(1, 3))]
        case _:
            return _table(rng, depth + 1)


def _table(rng: random.Random, depth: int) -> dict[str, object]:
    return {_text(rng): _value(rng, depth) for _ in range(rng.randrange(4))}


def test_random_tables_get_the_same_text_both_ways_and_read_back() -> None:
    rust = pytest.importorskip("cgr_graph_rs")
    if not hasattr(rust, "toml_dumps"):
        pytest.skip("this cgr_graph_rs build predates its TOML writer")
    rng = random.Random(_FUZZ_SEED)
    for _ in range(_FUZZ_CASES):
        table = _table(rng, 0)
        expected = dumps_python(table)
        assert rust.toml_dumps(table) == expected, table
        assert tomllib.loads(expected) == table


@pytest.mark.parametrize("case", list(_PINNED))
def test_the_rust_writer_writes_the_pinned_text(case: str) -> None:
    rust = pytest.importorskip("cgr_graph_rs")
    if not hasattr(rust, "toml_dumps"):
        pytest.skip("this cgr_graph_rs build predates its TOML writer")
    table, expected = _PINNED[case]
    assert rust.toml_dumps(table) == expected


@pytest.mark.parametrize(
    "table",
    [
        pytest.param({"x": 1.5}, id="float"),
        pytest.param({"x": 2**64}, id="int-past-64-bits"),
        pytest.param({"x": ("a", 1.0)}, id="float-in-tuple"),
        pytest.param({1: "x"}, id="non-str-key"),
        pytest.param({"x": object()}, id="unsupported"),
    ],
)
def test_the_rust_writer_declines_what_it_does_not_write(table: dict) -> None:
    rust = pytest.importorskip("cgr_graph_rs")
    if not hasattr(rust, "toml_dumps"):
        pytest.skip("this cgr_graph_rs build predates its TOML writer")
    assert rust.toml_dumps(table) is None


def test_a_declined_table_is_written_by_the_reference() -> None:
    assert (
        dumps({"ratio": 0.5, "big": 2**64})
        == "ratio = 0.5\nbig = 18446744073709551616\n"
    )


def test_without_the_extension_the_reference_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table, expected = _PINNED["inline-repos"]
    monkeypatch.setattr(toml_writer, "rust_extension", lambda: None)
    assert dumps(table) == expected


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
    assert path.read_text(encoding="utf-8") == dumps_python(
        {"workspace": config.model_dump()}
    )
