"""Jupyter notebooks index their Python code cells (issue #2480).

A `.ipynb` used to get a `File` node and nothing else: functions defined in
its cells were not in the graph, and its imports and calls into the
repository's own package were invisible, so `pkg.data.load` looked unused
although the notebook calls it. The code cells of a Python notebook are now
parsed as one module, `<project>.<path>.ipynb`, laid out line for line
against the notebook file so every recorded line is a line of the `.ipynb`.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import load_ignore_patterns
from codebase_rag.dead_code import dead_code_from_graph, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_graph_updater_incremental_rename import InMemoryGraph
from codebase_rag.utils.source_extraction import extract_source_lines

PROJECT = "nbproj"
NOTEBOOK = "analysis.ipynb"
NB_QN = f"{PROJECT}.analysis.ipynb"
LOAD_QN = f"{PROJECT}.pkg.data.load"
SUMMARIZE_QN = f"{NB_QN}.summarize"

_PACKAGE = {
    "pkg/__init__.py": "",
    "pkg/data.py": (
        "def load():\n    return [1, 2, 3]\n\n\n"
        "def _cached():\n    return load()\n\n\n"
        "def _stale():\n    return 0\n"
    ),
}


def _cell(cell_type: str, source: list[str] | str, **extra: object) -> dict:
    cell: dict[str, object] = {"cell_type": cell_type, "metadata": {}}
    if cell_type == "code":
        cell |= {"execution_count": None, "outputs": []}
    return cell | extra | {"source": source}


def _notebook(cells: list[dict], metadata: dict | None = None) -> str:
    # The layout Jupyter and nbformat write: one-space indent, keys sorted,
    # one source line per JSON line.
    if metadata is None:
        metadata = {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python"},
        }
    document = {"cells": cells, "metadata": metadata, "nbformat": 4}
    return json.dumps(document | {"nbformat_minor": 5}, indent=1, sort_keys=True) + "\n"


ANALYSIS_CELLS = [
    _cell("markdown", ["# Analysis\n", "def not_code():\n", "    pass\n"]),
    _cell(
        "code",
        [
            "%matplotlib inline\n",
            "!pip install pandas\n",
            "from pkg.data import load\n",
        ],
        outputs=[
            {
                "name": "stdout",
                "output_type": "stream",
                "text": ["def from_output():\n", "    pass\n"],
            }
        ],
    ),
    _cell("code", ["def summarize(xs):\n", "    return sum(xs)\n"]),
    _cell("raw", ["def from_raw():\n", "    pass\n"]),
    _cell("code", ["summarize(load())"]),
]


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
    return root


def _repo(tmp_path: Path, cells: list[dict] = ANALYSIS_CELLS, **more: str) -> Path:
    return _write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: _notebook(cells), **more})


def _updater(root: Path, graph: InMemoryGraph | MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    # The CLI reads `.cgrignore` and hands the updater its patterns.
    return GraphUpdater(
        ingestor=graph,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        exclude_paths=load_ignore_patterns(root).exclude or None,
    )


def _index(root: Path) -> InMemoryGraph:
    graph = InMemoryGraph()
    _updater(root, graph).run(force=True)
    return graph


def _qns(graph: InMemoryGraph, label: cs.NodeLabel) -> set[str]:
    return {str(uid) for (node_label, uid) in graph.nodes if node_label == label}


def _edges(graph: InMemoryGraph, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    return {
        (str(from_val), str(to_val))
        for (_fl, _fk, from_val, rel_type, _tl, _tk, to_val) in graph.rels
        if rel_type == rel
    }


def _file_line(path: Path, fragment: str) -> int:
    """The notebook file line holding a source fragment's JSON string."""
    encoded = json.dumps(fragment)
    lines = path.read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, 1) if encoded in line)


def _has_file(graph: InMemoryGraph, path: str) -> bool:
    return any(
        props.get(cs.KEY_PATH) == path
        for (label, _uid), props in graph.nodes.items()
        if label == cs.NodeLabel.FILE
    )


def _dead(graph: InMemoryGraph) -> set[str]:
    rels = [(fl, fv, rel, tl, tv) for (fl, _fk, fv, rel, tl, _tk, tv) in graph.rels]
    return dead_code_from_graph(
        graph.nodes, rels, f"{PROJECT}.", default_dead_code_config(False, False)
    )


def _function_props(graph: InMemoryGraph, qn: str) -> dict:
    return dict(graph.nodes[(cs.NodeLabel.FUNCTION.value, qn)])


class TestNotebookCellsAreIndexed:
    def test_the_code_cells_make_a_module_that_defines_their_functions(
        self, tmp_path: Path
    ) -> None:
        graph = _index(_repo(tmp_path))

        assert NB_QN in _qns(graph, cs.NodeLabel.MODULE)
        module = graph.nodes[(cs.NodeLabel.MODULE.value, NB_QN)]
        assert module[cs.KEY_PATH] == NOTEBOOK
        assert SUMMARIZE_QN in _qns(graph, cs.NodeLabel.FUNCTION)
        assert (NB_QN, SUMMARIZE_QN) in _edges(graph, cs.RelationshipType.DEFINES)

    def test_its_imports_and_calls_into_the_package_are_edges(
        self, tmp_path: Path
    ) -> None:
        graph = _index(_repo(tmp_path))

        assert (NB_QN, f"{PROJECT}.pkg.data") in _edges(
            graph, cs.RelationshipType.IMPORTS
        )
        calls = _edges(graph, cs.RelationshipType.CALLS)
        assert (NB_QN, LOAD_QN) in calls, sorted(calls)
        assert (NB_QN, SUMMARIZE_QN) in calls, sorted(calls)

    def test_lines_are_lines_of_the_notebook_file(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        root = _repo(tmp_path)
        nb_path = root / NOTEBOOK
        create_and_run_updater(root, mock_ingestor)

        function = next(
            c.args[1]
            for c in mock_ingestor.ensure_node_batch.call_args_list
            if c.args[0] == cs.NodeLabel.FUNCTION
            and c.args[1][cs.KEY_QUALIFIED_NAME].endswith(".analysis.ipynb.summarize")
        )
        assert function[cs.KEY_START_LINE] == _file_line(
            nb_path, "def summarize(xs):\n"
        )
        assert function[cs.KEY_END_LINE] == _file_line(nb_path, "    return sum(xs)\n")
        site_line = _file_line(nb_path, "summarize(load())")
        sites = {
            (c.args[2][2].rsplit(".", 1)[-1], c.kwargs["properties"][cs.KEY_LINE])
            for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS)
            if c.args[0][2].endswith(".analysis.ipynb")
        }
        assert sites >= {("summarize", site_line), ("load", site_line)}, sites

    def test_a_package_helper_only_the_notebook_calls_is_not_dead(
        self, tmp_path: Path
    ) -> None:
        # Public Python names are dead-code roots already; a private helper is
        # live only through a call, and the notebook's is the only one.
        cells = [
            _cell("code", ["from pkg.data import _cached\n"]),
            _cell("code", ["_cached()"]),
        ]
        dead = _dead(_index(_repo(tmp_path, cells)))

        assert f"{PROJECT}.pkg.data._cached" not in dead, sorted(dead)
        # Not vacuous: the analysis does report what nothing calls.
        assert f"{PROJECT}.pkg.data._stale" in dead, sorted(dead)

    def test_the_notebook_top_level_is_an_entry_point(self, tmp_path: Path) -> None:
        cells = [
            _cell(
                "code",
                [
                    "def _helper():\n",
                    "    return 1\n",
                    "\n",
                    "def _orphan():\n",
                    "    return 2\n",
                ],
            ),
            _cell("code", ["_helper()"]),
        ]
        dead = _dead(_index(_repo(tmp_path, cells)))

        assert f"{NB_QN}._orphan" in dead, sorted(dead)
        assert f"{NB_QN}._helper" not in dead, sorted(dead)

    def test_a_snippet_of_a_notebook_definition_reads_back_as_python(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        props = _function_props(_index(root), SUMMARIZE_QN)

        snippet = extract_source_lines(
            root / NOTEBOOK, props[cs.KEY_START_LINE], props[cs.KEY_END_LINE]
        )
        assert snippet == "def summarize(xs):\n    return sum(xs)"

    def test_an_evicted_notebook_reparses_as_python(self, tmp_path: Path) -> None:
        root = _repo(tmp_path)
        updater = _updater(root, InMemoryGraph())
        updater.run(force=True)

        reloaded = updater._load_ast_from_disk(root / NOTEBOOK)

        assert reloaded is not None
        node, language = reloaded
        assert language == cs.SupportedLanguage.PYTHON
        assert node.text is not None and b"def summarize(xs):" in node.text
        assert b'"cells"' not in node.text


class TestIPythonSyntax:
    def test_magics_and_shell_escapes_do_not_hide_the_code_around_them(
        self, tmp_path: Path
    ) -> None:
        cells = [
            _cell(
                "code",
                [
                    "%load_ext autoreload\n",
                    "files = !ls\n",
                    "for name in files:\n",
                    "    !echo {name}\n",
                    "?load\n",
                    "def after_magics():\n",
                    "    return load()\n",
                ],
            ),
            _cell("code", ["from pkg.data import load\n", "after_magics()"]),
        ]
        graph = _index(_repo(tmp_path, cells))

        assert f"{NB_QN}.after_magics" in _qns(graph, cs.NodeLabel.FUNCTION)
        calls = _edges(graph, cs.RelationshipType.CALLS)
        assert (f"{NB_QN}.after_magics", LOAD_QN) in calls, sorted(calls)

    def test_a_cell_magic_running_python_keeps_its_body(self, tmp_path: Path) -> None:
        cells = [_cell("code", ["%%time\n", "def timed():\n", "    return 1\n"])]
        graph = _index(_repo(tmp_path, cells))

        assert f"{NB_QN}.timed" in _qns(graph, cs.NodeLabel.FUNCTION)

    @pytest.mark.parametrize("magic", ["bash", "writefile helper.py", "html"])
    def test_a_cell_magic_handing_its_body_elsewhere_skips_the_cell(
        self, tmp_path: Path, magic: str
    ) -> None:
        cells = [
            _cell("code", [f"%%{magic}\n", "def elsewhere():\n", "    return 1\n"]),
            _cell("code", ["def kept():\n", "    return 2\n"]),
        ]
        graph = _index(_repo(tmp_path, cells))

        functions = _qns(graph, cs.NodeLabel.FUNCTION)
        assert f"{NB_QN}.kept" in functions
        assert f"{NB_QN}.elsewhere" not in functions

    def test_magic_looking_lines_inside_a_string_are_kept(self, tmp_path: Path) -> None:
        # A docstring is Python text whatever its lines start with; IPython
        # leaves it alone, so the snippet must read exactly as written.
        code = [
            "def documented():\n",
            '    """Usage:\n',
            "    %timeit documented()\n",
            "    !echo done\n",
            "    ?documented\n",
            '    """\n',
            "    return 1\n",
        ]
        root = _repo(tmp_path, [_cell("code", code)])
        props = _function_props(_index(root), f"{NB_QN}.documented")

        snippet = extract_source_lines(
            root / NOTEBOOK, props[cs.KEY_START_LINE], props[cs.KEY_END_LINE]
        )
        assert snippet == "".join(code).strip()

    @pytest.mark.parametrize(
        "code",
        [
            ["total = (10\n", "         %divisor())\n"],
            ["total = 10 \\\n", "    %divisor()\n"],
        ],
        ids=["bracket", "backslash"],
    )
    def test_an_unspaced_modulo_continuation_keeps_its_call(
        self, tmp_path: Path, code: list[str]
    ) -> None:
        cells = [
            _cell("code", ["def divisor():\n", "    return 3\n"]),
            _cell("code", code),
        ]
        root = _repo(tmp_path, cells)
        graph = _index(root)

        calls = _edges(graph, cs.RelationshipType.CALLS)
        assert (NB_QN, f"{NB_QN}.divisor") in calls, sorted(calls)
        first = _file_line(root / NOTEBOOK, code[0])
        snippet = extract_source_lines(root / NOTEBOOK, first, first + 1)
        assert snippet == "".join(code).strip()


class TestNotebookLayouts:
    def test_a_cell_stored_as_one_string_still_indexes(self, tmp_path: Path) -> None:
        cells = [
            _cell("code", "from pkg.data import load\n"),
            _cell("code", "def one_string():\n    return load()\n"),
        ]
        graph = _index(_repo(tmp_path, cells))

        assert f"{NB_QN}.one_string" in _qns(graph, cs.NodeLabel.FUNCTION)
        assert (f"{NB_QN}.one_string", LOAD_QN) in _edges(
            graph, cs.RelationshipType.CALLS
        )

    def test_a_minified_notebook_still_indexes(self, tmp_path: Path) -> None:
        minified = json.dumps(json.loads(_notebook(ANALYSIS_CELLS)))
        root = _write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: minified})
        graph = _index(root)

        assert SUMMARIZE_QN in _qns(graph, cs.NodeLabel.FUNCTION)
        assert (NB_QN, LOAD_QN) in _edges(graph, cs.RelationshipType.CALLS)

    def test_windows_line_endings_and_a_byte_order_mark_still_index(
        self, tmp_path: Path
    ) -> None:
        text = "\ufeff" + _notebook(ANALYSIS_CELLS).replace("\n", "\r\n")
        root = _write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: text})
        graph = _index(root)

        props = _function_props(graph, SUMMARIZE_QN)
        assert props[cs.KEY_START_LINE] == _file_line(
            root / NOTEBOOK, "def summarize(xs):\n"
        )


class TestIncrementalSync:
    @pytest.mark.parametrize("sync", ["fresh", "reused", "reingest"])
    def test_an_edited_notebook_reparses_like_any_file(
        self, tmp_path: Path, sync: str
    ) -> None:
        edited = [
            *ANALYSIS_CELLS[:2],
            _cell("code", ["def total(xs):\n", "    return sum(xs)\n"]),
            _cell("code", ["total(load())"]),
        ]
        golden = _index(_repo(tmp_path / "golden", edited))

        root = _repo(tmp_path / "incr")
        graph = InMemoryGraph()
        updater = _updater(root, graph)
        updater.run(force=True)
        assert SUMMARIZE_QN in _qns(graph, cs.NodeLabel.FUNCTION)
        (root / NOTEBOOK).write_text(_notebook(edited), encoding="utf-8")
        match sync:
            case "fresh":
                _updater(root, graph).run(force=False)
            case "reused":
                updater.run(force=False)
            case _:
                # The watcher's and the MCP `reingest` tool's path.
                updater.reingest([root / NOTEBOOK])

        functions = _qns(graph, cs.NodeLabel.FUNCTION)
        assert f"{NB_QN}.total" in functions
        assert SUMMARIZE_QN not in functions
        assert graph.snapshot() == golden.snapshot()

    def test_an_edit_to_the_package_keeps_the_notebook_call(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        graph = InMemoryGraph()
        _updater(root, graph).run(force=True)
        data = root / "pkg" / "data.py"
        data.write_text(data.read_text() + "\n\ndef added():\n    return 2\n")
        _updater(root, graph).run(force=False)

        assert (NB_QN, LOAD_QN) in _edges(graph, cs.RelationshipType.CALLS)


class TestWhatStaysAFile:
    """What is not notebook code: it is skipped, or the file stays a `File`."""

    def test_markdown_raw_cells_and_outputs_are_never_code(
        self, tmp_path: Path
    ) -> None:
        graph = _index(_repo(tmp_path))

        names = {qn.rsplit(".", 1)[-1] for qn in _qns(graph, cs.NodeLabel.FUNCTION)}
        assert not names & {"not_code", "from_raw", "from_output"}

    @pytest.mark.parametrize(
        "metadata",
        [
            {"kernelspec": {"language": "R", "name": "ir"}},
            {"language_info": {"name": "julia"}},
            {
                "kernelspec": {"language": "python", "name": "python3"},
                "language_info": {"name": "scala"},
            },
        ],
        ids=["r-kernelspec", "julia-language-info", "reported-wins"],
    )
    def test_a_notebook_in_another_language_stays_a_file(
        self, tmp_path: Path, metadata: dict
    ) -> None:
        cells = [_cell("code", ["def looks_like_python():\n", "    return 1\n"])]
        root = _write(
            tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: _notebook(cells, metadata)}
        )
        graph = _index(root)

        assert NB_QN not in _qns(graph, cs.NodeLabel.MODULE)
        functions = _qns(graph, cs.NodeLabel.FUNCTION)
        assert not any("looks_like_python" in qn for qn in functions)
        assert _has_file(graph, NOTEBOOK)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json at all",
            '{"cells": [',
            "[1, 2, 3]",
            '{"worksheets": [{"cells": []}], "nbformat": 3}',
            '{"cells": {"cell_type": "code"}}',
            '{"cells": [{"cell_type": "code", "source": 7}]}',
            '{"cells": [{"cell_type": "code", "source": ["ok\\n", null]}]}',
            '{"cells": [{"cell_type": "code", "source": "unterminated}]}',
        ],
        ids=[
            "empty",
            "not-json",
            "truncated",
            "array",
            "nbformat-3",
            "cells-not-a-list",
            "source-a-number",
            "source-holds-null",
            "unterminated-string",
        ],
    )
    def test_a_malformed_notebook_stays_a_file_and_the_run_goes_on(
        self, tmp_path: Path, text: str
    ) -> None:
        root = _write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: text})
        graph = _index(root)

        assert NB_QN not in _qns(graph, cs.NodeLabel.MODULE)
        assert LOAD_QN in _qns(graph, cs.NodeLabel.FUNCTION)
        assert _has_file(graph, NOTEBOOK)

    @pytest.mark.parametrize(
        ("nbformat", "trailer"),
        [
            (None, "\n"),
            (3, "\n"),
            (5, "\n"),
            ("4", "\n"),
            (4, "\n<<<<<<< HEAD\n"),
            (4, "\n{}\n"),
        ],
        ids=["missing", "v3", "v5", "string", "merge-marker", "second-object"],
    )
    def test_a_notebook_that_is_not_one_nbformat_4_document_stays_a_file(
        self, tmp_path: Path, nbformat: int | str | None, trailer: str
    ) -> None:
        document = json.loads(_notebook(ANALYSIS_CELLS))
        if nbformat is None:
            del document["nbformat"]
        else:
            document["nbformat"] = nbformat
        text = json.dumps(document, indent=1, sort_keys=True) + trailer
        graph = _index(_write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: text}))

        assert NB_QN not in _qns(graph, cs.NodeLabel.MODULE)
        assert SUMMARIZE_QN not in _qns(graph, cs.NodeLabel.FUNCTION)
        assert _has_file(graph, NOTEBOOK)

    def test_whitespace_after_the_notebook_is_fine(self, tmp_path: Path) -> None:
        text = _notebook(ANALYSIS_CELLS) + " \r\n\t\n"
        graph = _index(_write(tmp_path / PROJECT, {**_PACKAGE, NOTEBOOK: text}))

        assert SUMMARIZE_QN in _qns(graph, cs.NodeLabel.FUNCTION)

    def test_jupyter_checkpoint_copies_are_not_indexed(self, tmp_path: Path) -> None:
        checkpoint = ".ipynb_checkpoints/analysis-checkpoint.ipynb"
        root = _repo(tmp_path, **{checkpoint: _notebook(ANALYSIS_CELLS)})
        graph = _index(root)

        assert not any("checkpoint" in qn for qn in _qns(graph, cs.NodeLabel.FUNCTION))
        assert not _has_file(graph, checkpoint)

    def test_cgrignore_opts_notebooks_out(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, **{".cgrignore": "*.ipynb\n"})
        graph = _index(root)

        assert NB_QN not in _qns(graph, cs.NodeLabel.MODULE)
        assert not any(
            props.get(cs.KEY_PATH) == NOTEBOOK for props in graph.nodes.values()
        )
        assert LOAD_QN in _qns(graph, cs.NodeLabel.FUNCTION)

    def test_a_same_stem_python_module_keeps_its_name(self, tmp_path: Path) -> None:
        root = _repo(tmp_path, **{"analysis.py": "def from_py():\n    return 1\n"})
        graph = _index(root)

        module = graph.nodes[(cs.NodeLabel.MODULE.value, f"{PROJECT}.analysis")]
        assert module[cs.KEY_PATH] == "analysis.py"
        assert f"{PROJECT}.analysis.from_py" in _qns(graph, cs.NodeLabel.FUNCTION)

    def test_a_notebook_call_never_binds_to_another_languages_function(
        self, tmp_path: Path
    ) -> None:
        cells = [_cell("code", ["def run():\n", "    return helper()\n"])]
        root = _repo(
            tmp_path, cells, **{"web/helper.js": "function helper() { return 1; }\n"}
        )
        graph = _index(root)

        calls = _edges(graph, cs.RelationshipType.CALLS)
        assert not {edge for edge in calls if edge[1].endswith(".helper")}, calls

    def test_an_import_of_a_stem_shared_across_languages_takes_the_python_file(
        self, tmp_path: Path
    ) -> None:
        # `util.py` beside `util.js` leaves no bare `proj.util` (issue #2586);
        # a notebook's import, like a `.py` file's, lands on the Python one.
        cells = [_cell("code", ["from util import helper\n", "helper()"])]
        root = _repo(
            tmp_path,
            cells,
            **{
                "util.py": "def helper():\n    return 1\n",
                "util.js": "function helper() { return 2; }\n",
            },
        )
        calls = _edges(_index(root), cs.RelationshipType.CALLS)

        assert (NB_QN, f"{PROJECT}.util.py.helper") in calls, sorted(calls)
        assert (NB_QN, f"{PROJECT}.util.js.helper") not in calls, sorted(calls)
