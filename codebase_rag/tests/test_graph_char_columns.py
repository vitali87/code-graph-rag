"""Issue #3162: the graph's columns are UTF-8 byte offsets, and `cgr graph`
and its MCP tools now also answer the same position in characters.

tree-sitter counts a column in bytes, and the graph stores it that way:
`rename` slices those bytes. On a line with non-ASCII text before a site the
byte column is larger than the character column an editor, an LSP client or
an agent indexing a `str` counts, so `line[col:]` landed several characters
past the call. Every column `callers`, `callees` and `importers` answer now
has a `_char` twin, derived from the file when the answer is read; the byte
columns stay as they were.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.utils.char_columns import CharColumns
from evals.cgr_graph import _StatefulIngestor

P = "cols"
LABEL = f"{P}.greet.label"
GREET = f"{P}.greet.greet"

# The issue's repro: two calls on a line whose text before them is Cyrillic.
GREET_LINE = "    return f\"Привет, {label('мир')} и {label('друг')}\""
GREET_PY = f"def label(x):\n    return x\n\n\ndef greet():\n{GREET_LINE}\n"
ASCII_LINE = "    return label('world') + label('friend')"
ASCII_PY = f"def label(x):\n    return x\n\n\ndef greet():\n{ASCII_LINE}\n"
# An import statement after a non-ASCII assignment on the same line.
MAIN_LINE = 'GRUSS = "Grüße"; from greet import label'
MAIN_PY = f"{MAIN_LINE}\n\n\ndef main():\n    return label(GRUSS)\n"
# `main` reaches `pkg.greet` through the package facade (issue #2573).
FACADE_LINE = 'NAME = "Grüße"; from .greet import label'
APP_LINE = 'GRUSS = "ä"; from pkg import label'
REEXPORT = {
    "pkg/__init__.py": f"{FACADE_LINE}\n",
    "pkg/greet.py": GREET_PY,
    "app.py": f"{APP_LINE}\n\n\ndef main():\n    return label(GRUSS)\n",
}


def _store(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=P,
    ).run(force=True)
    return store


def _fetch(store: _StatefulIngestor) -> graph_query.QueryFn:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        p = {} if params is None else dict(params)
        if query != cq.CYPHER_GRAPH_IMPORTERS_OF:
            return store.fetch_all(query, None if params is None else p)
        # The emulator answers the direct read only; one hop of the re-export
        # walk is that read for each module of the frontier.
        qns = p.pop(cs.KEY_QNS)
        assert isinstance(qns, list)
        return [
            {**row, cs.KEY_TO_QN: qn}
            for qn in qns
            for row in store.fetch_all(cq.CYPHER_GRAPH_IMPORTERS, {**p, cs.KEY_QN: qn})
        ]

    return fetch_all


def _ingestor(store: _StatefulIngestor) -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch(store))
    ingestor.list_projects.return_value = [P]
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    return ingestor


def _cli(store: _StatefulIngestor, root: Path, *args: str) -> list[dict]:
    with patch(
        "codebase_rag.cli_runtime.connect_memgraph", return_value=_ingestor(store)
    ):
        result = CliRunner().invoke(
            graph_cli, [*args, "--project", P, "--repo-path", str(root)]
        )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _registry(store: _StatefulIngestor, root: Path) -> MCPToolsRegistry:
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(root), ingestor=_ingestor(store), cypher_gen=MagicMock()
        )


def _sites(rows: list[dict]) -> list[tuple[int, int | None, int, int | None]]:
    return [
        (r["col"], r.get(cs.KEY_COL_CHAR), r["end_col"], r.get(cs.KEY_END_COL_CHAR))
        for r in rows
    ]


# --- the reported bug -------------------------------------------------------------


def test_cli_callers_answer_the_character_column_beside_the_byte_column(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY})
    rows = _cli(store, temp_repo, "callers", LABEL)
    assert [(r["line"], r["col"], r[cs.KEY_COL_CHAR]) for r in rows] == [
        (6, 28, 22),
        (6, 49, 39),
    ]
    raw = GREET_LINE.encode()
    for row in rows:
        # The byte columns slice the bytes and the character columns the
        # `str` to the same call.
        call = raw[row["col"] : row["end_col"]].decode()
        assert call.startswith("label(")
        assert GREET_LINE[row[cs.KEY_COL_CHAR] : row[cs.KEY_END_COL_CHAR]] == call


def test_cli_callees_answer_the_character_columns_too(temp_repo: Path) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY})
    rows = _cli(store, temp_repo, "callees", GREET)
    assert _sites(rows) == [(28, 22, 43, 34), (49, 39, 66, 52)]


async def test_mcp_callers_answer_the_character_columns_too(temp_repo: Path) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY})
    rows = await _registry(store, temp_repo).callers(LABEL, project=P)
    assert isinstance(rows, list), rows
    assert _sites(rows) == [(28, 22, 43, 34), (49, 39, 66, 52)]
    # Beside its byte column, the order a client reads the row in.
    keys = list(rows[0])
    assert keys[keys.index("col") + 1] == cs.KEY_COL_CHAR
    assert keys[keys.index("end_col") + 1] == cs.KEY_END_COL_CHAR


async def test_cli_and_mcp_importers_answer_the_character_columns(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY, "main.py": MAIN_PY})
    prefix = 'GRUSS = "Grüße"; '
    for rows in (
        _cli(store, temp_repo, "importers", f"{P}.greet"),
        await _registry(store, temp_repo).importers(f"{P}.greet", project=P),
    ):
        assert [
            (r["module"], r["col"], r[cs.KEY_COL_CHAR], r[cs.KEY_END_COL_CHAR])
            for r in rows
        ] == [(f"{P}.main", len(prefix.encode()), len(prefix), len(MAIN_LINE))]


async def test_importers_through_reexports_answer_them_on_every_via_row(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, REEXPORT)
    facade = (FACADE_LINE.index("from"), len(FACADE_LINE))
    app = (APP_LINE.index("from"), len(APP_LINE))
    for rows in (
        _cli(store, temp_repo, "importers", f"{P}.pkg.greet", "--through-reexports"),
        await _registry(store, temp_repo).importers(
            f"{P}.pkg.greet", project=P, through_reexports=True
        ),
    ):
        assert isinstance(rows, list), rows
        assert [
            (
                r["module"],
                (r[cs.KEY_COL_CHAR], r[cs.KEY_END_COL_CHAR]),
                [(v[cs.KEY_COL_CHAR], v[cs.KEY_END_COL_CHAR]) for v in r["via"]],
            )
            for r in rows
        ] == [(f"{P}.pkg", facade, []), (f"{P}.app", app, [facade])]
        assert rows[1]["col"] == len(APP_LINE[: app[0]].encode())


# --- what must not change ---------------------------------------------------------


def test_an_ascii_line_has_the_same_character_and_byte_columns(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, {"greet.py": ASCII_PY})
    rows = _cli(store, temp_repo, "callers", LABEL)
    assert len(rows) == 2
    for row in rows:
        assert row[cs.KEY_COL_CHAR] == row["col"]
        assert row[cs.KEY_END_COL_CHAR] == row["end_col"]


def test_a_file_that_cannot_be_read_leaves_the_character_columns_out(
    temp_repo: Path,
) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY})
    (temp_repo / "greet.py").unlink()
    rows = _cli(store, temp_repo, "callers", LABEL)
    assert [(r["col"], r["end_col"]) for r in rows] == [(28, 43), (49, 66)]
    assert all(cs.KEY_COL_CHAR not in r and cs.KEY_END_COL_CHAR not in r for r in rows)


def test_another_checkout_is_not_read_for_the_character_columns(
    temp_repo: Path, tmp_path: Path
) -> None:
    """A repository the project was not indexed from may hold a file at the
    same path with other text; its columns would be a guess."""
    store = _store(temp_repo, {"greet.py": GREET_PY})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "greet.py").write_text(ASCII_PY, encoding="utf-8")
    rows = _cli(store, elsewhere, "callers", LABEL)
    assert [r["col"] for r in rows] == [28, 49]
    assert all(cs.KEY_COL_CHAR not in r for r in rows)


def test_the_graph_readers_answer_no_character_columns_without_a_root(
    temp_repo: Path,
) -> None:
    """`rename`, `move` and `cgr check` read the same rows for their byte
    columns and pass no root."""
    store = _store(temp_repo, {"greet.py": GREET_PY, "main.py": MAIN_PY})
    fetch = _fetch(store)
    for rows in (
        graph_query.callers(fetch, P, LABEL),
        graph_query.importers(fetch, P, f"{P}.greet"),
    ):
        assert rows
        assert all(cs.KEY_COL_CHAR not in r for r in rows)


# --- the conversion -----------------------------------------------------------------


def test_each_file_is_read_once_per_answer(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(temp_repo, {"greet.py": GREET_PY})
    reads: list[Path] = []
    read_bytes = Path.read_bytes

    def counting(self: Path) -> bytes:
        reads.append(self)
        return read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    rows = graph_query.callers(_fetch(store), P, LABEL, repo_root=temp_repo)
    assert [r[cs.KEY_COL_CHAR] for r in rows] == [22, 39]
    assert [p.name for p in reads] == ["greet.py"]


@pytest.mark.parametrize(
    ("line", "col", "expected"),
    [
        (6, 28, 22),
        (6, 0, 0),
        (6, len(GREET_LINE.encode()), len(GREET_LINE)),
        # Inside the two bytes of `П`: no character starts there.
        (6, 14, None),
        (6, len(GREET_LINE.encode()) + 1, None),
        (0, 0, None),
        (99, 0, None),
        (6, -1, None),
        (6, None, None),
        (None, 0, None),
    ],
)
def test_a_position_off_the_line_or_inside_a_character_answers_nothing(
    temp_repo: Path, line: int | None, col: int | None, expected: int | None
) -> None:
    (temp_repo / "greet.py").write_text(GREET_PY, encoding="utf-8")
    assert CharColumns(temp_repo).at("greet.py", line, col) == expected


def test_no_root_a_missing_file_or_a_path_outside_the_root_answers_nothing(
    temp_repo: Path,
) -> None:
    root = temp_repo / "repo"
    root.mkdir()
    (root / "greet.py").write_text(GREET_PY, encoding="utf-8")
    (temp_repo / "outside.py").write_text(GREET_PY, encoding="utf-8")
    assert CharColumns(root).at("greet.py", 6, 28) == 22
    assert CharColumns(None).at("greet.py", 6, 28) is None
    assert CharColumns(root).at("missing.py", 6, 28) is None
    assert CharColumns(root).at(None, 6, 28) is None
    assert CharColumns(root).at("../outside.py", 6, 28) is None


def test_a_declared_encoding_is_read_as_the_indexer_parsed_it(
    temp_repo: Path,
) -> None:
    """A latin-1 `é` is one byte on disk and two in the UTF-8 text the
    grammar parsed, which is what the byte column counts (issue #2901)."""
    (temp_repo / "m.py").write_bytes(
        '# -*- coding: latin-1 -*-\ns = "é"; f()\n'.encode("latin-1")
    )
    assert CharColumns(temp_repo).at("m.py", 2, len('s = "é"; '.encode())) == len(
        's = "é"; '
    )
