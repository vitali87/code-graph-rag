"""The reference graphs are written so a difference means a change (issue #3629).

`fixtures/reference_graphs` holds the graphs of pinned public repositories,
one or more per language, as `cgr` builds and exports them, written by
`scripts/reference_graphs.py` and checked by the `Reference Graphs` CI job.
These tests cover what the job cannot: that the written form does not depend
on where or when the index ran, and that every listed repository has a graph.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from codebase_rag import constants as cs
from codebase_rag.canonical_graph import (
    canonical,
    difference,
    read_graph,
    write_graph,
)
from codebase_rag.types_defs import ExportRecord, ReferenceRepo
from scripts import reference_graphs
from scripts.reference_graphs import (
    REFERENCE_DIR,
    _index_env,
    cgr_command,
    environment_path,
    graph_path,
    load_manifest,
)

ROOT = Path("/work/checkout/demo")
_COMMIT = re.compile(r"[0-9a-f]{40}")
WORKFLOW = reference_graphs.PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
_SEP = cs.REFERENCE_FIELD_SEPARATOR
ENVIRONMENT = sorted(
    [
        f"{cs.LanguageTier.AST_GREP}{_SEP}bash",
        f"{cs.LanguageTier.AST_GREP}{_SEP}ruby",
        f"{cs.LanguageTier.TREE_SITTER}{_SEP}c",
        f"{cs.LanguageTier.TREE_SITTER}{_SEP}python",
    ]
)
WITHOUT_AST_GREP = [
    line for line in ENVIRONMENT if not line.startswith(cs.LanguageTier.AST_GREP)
]


def _export(order: list[int]) -> ExportRecord:
    """A small export whose node ids follow `order`: the database's row
    numbers, which differ from one run to the next."""
    project, folder, function, method = order
    return {
        "nodes": [
            {
                "node_id": function,
                "labels": ["Function"],
                "properties": {
                    "qualified_name": "demo.app.main",
                    "name": "main",
                    "path": "app.py",
                    "start_line": 1,
                },
            },
            {
                "node_id": project,
                "labels": ["Project"],
                "properties": {
                    "name": "demo",
                    "root_path": str(ROOT),
                    "last_synced_at": "2026-11-18T13:14:10+00:00",
                },
            },
            {
                "node_id": folder,
                "labels": ["Folder"],
                "properties": {
                    "absolute_path": f"{ROOT}/pkg",
                    "path": "pkg",
                    "name": "pkg",
                },
            },
            {
                "node_id": method,
                "labels": ["Method"],
                "properties": {"qualified_name": "demo.app.C.run", "name": "run"},
            },
        ],
        "relationships": [
            {
                "from_id": project,
                "to_id": folder,
                "type": "CONTAINS_FOLDER",
                "properties": {},
            },
            {
                "from_id": function,
                "to_id": method,
                "type": "CALLS",
                "properties": {"line": 2, "resolution": "exact"},
            },
        ],
    }


def test_a_graph_reads_the_same_whatever_row_numbers_the_database_gave() -> None:
    assert canonical(_export([1, 2, 3, 4]), ROOT) == canonical(
        _export([40, 7, 12, 3]), ROOT
    )


def test_a_graph_names_no_directory_of_the_machine_and_no_time() -> None:
    text = "\n".join(canonical(_export([1, 2, 3, 4]), ROOT))

    assert str(ROOT) not in text
    assert f"{cs.REFERENCE_ROOT_PLACEHOLDER}/pkg" in text
    assert "last_synced_at" not in text


def test_a_node_is_named_by_the_property_its_label_is_keyed_on() -> None:
    lines = canonical(_export([1, 2, 3, 4]), ROOT)
    calls = next(line for line in lines if "\tCALLS\t" in line)

    assert calls.split(cs.REFERENCE_FIELD_SEPARATOR)[2:6] == [
        "Function",
        '"demo.app.main"',
        "Method",
        '"demo.app.C.run"',
    ]
    assert lines == sorted(lines)


def test_an_edge_recorded_twice_is_a_difference() -> None:
    once = canonical(_export([1, 2, 3, 4]), ROOT)
    twice_export = _export([1, 2, 3, 4])
    relationships = twice_export["relationships"]
    assert isinstance(relationships, list)
    relationships.append(relationships[-1])

    diff = difference(once, canonical(twice_export, ROOT))

    assert len(diff) == 1
    assert diff[0].startswith("+ E\tCALLS")


def test_difference_marks_each_side() -> None:
    assert difference(["a", "b"], ["b", "c"]) == ["- a", "+ c"]
    assert difference(["a"], ["a"]) == []


def test_an_index_runs_with_the_frontends_that_need_no_compiler(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GO_FRONTEND", "gotypes")
    monkeypatch.setitem(
        reference_graphs._CALLER_ENV, "SQLITE_GRAPH_PATH", "/somewhere/else.sqlite"
    )

    env = _index_env(tmp_path, seed=3)

    assert env["GO_FRONTEND"] == cs.REFERENCE_ENV["GO_FRONTEND"]
    assert "SQLITE_GRAPH_PATH" not in env
    assert env[cs.REFERENCE_HOME_ENV] == str(tmp_path)
    assert env[cs.REFERENCE_SEED_ENV] == "3"


def test_every_listed_repository_is_pinned_and_has_a_recorded_graph() -> None:
    repos = load_manifest()
    names = [repo["name"] for repo in repos]
    languages = {str(language) for language in cs.SupportedLanguage}

    assert len(names) == len(set(names))
    for repo in repos:
        assert _COMMIT.fullmatch(repo["commit"]), repo["name"]
        assert repo["language"] in languages, repo["name"]
        assert graph_path(repo).is_file(), repo["name"]


def test_every_language_has_a_reference_repository() -> None:
    # Checked against the languages cgr supports, not the manifest, so
    # dropping a language's entry and its graph together still fails.
    covered = {repo["language"] for repo in load_manifest()}
    required = {
        str(language) for language in cs.SupportedLanguage
    } - cs.REFERENCE_EXEMPT_LANGUAGES

    assert required <= covered, sorted(required - covered)


def test_a_record_holding_a_unicode_line_separator_reads_back_whole(
    tmp_path: Path,
) -> None:
    lines = ['N\tFunction\t"demo.f"\t{"docstring":"one\u2028two"}', "N\tx"]
    path = tmp_path / "demo.graph"

    write_graph(path, lines)

    assert read_graph(path) == lines


def test_the_callers_settings_reach_the_index_in_no_spelling(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        reference_graphs._CALLER_ENV, "sqlite_graph_path", "/somewhere/else.sqlite"
    )
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "go_frontend", "gotypes")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "cgr_home", "/somewhere/home")

    env = _index_env(tmp_path, seed=None)

    assert {name.upper() for name in env if name.islower()} & {
        "SQLITE_GRAPH_PATH",
        "GO_FRONTEND",
        "CGR_HOME",
    } == set()
    assert env[cs.REFERENCE_HOME_ENV] == str(tmp_path)


def test_no_cgr_setting_of_the_caller_reaches_the_index(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "CGR_CAPTURE", "-CALLS")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "PYTHON_FRONTEND", "jedi")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "PATH_FOR_THE_INDEX", "kept")

    env = _index_env(tmp_path, seed=None)

    assert "CGR_CAPTURE" not in env
    assert env["PYTHON_FRONTEND"] == cs.REFERENCE_ENV["PYTHON_FRONTEND"]
    assert env["PATH_FOR_THE_INDEX"] == "kept"


# Stands in for `cgr start ... -o OUT`: writes an empty export that says
# which engine selection the environment it ran with held.
_SELECTION_CGR = (
    "import json, os, sys\n"
    "out = sys.argv[sys.argv.index('-o') + 1]\n"
    "export = {'nodes': [], 'relationships': [],\n"
    "          'selection': os.environ.get('CGR_INDEX_ENGINE')}\n"
    "with open(out, 'w', encoding='utf-8') as file:\n"
    "    json.dump(export, file)\n"
)


def test_the_selection_asked_for_reaches_the_index_in_place_of_the_callers(
    monkeypatch, tmp_path: Path
) -> None:
    # Every cgr setting of the caller's is kept from the index, so a
    # selection in the shell never arrives; `--index-engine` is how one does.
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "CGR_INDEX_ENGINE", "python")
    repo = load_manifest()[0]
    cgr = [sys.executable, "-c", _SELECTION_CGR]

    asked = reference_graphs.index(cgr, repo, tmp_path, None, "go=rust")
    unasked = reference_graphs.index(cgr, repo, tmp_path, None)

    assert asked["selection"] == "go=rust"
    assert unasked["selection"] is None
    assert _index_env(tmp_path, None, "go=rust")["CGR_INDEX_ENGINE"] == "go=rust"
    assert "CGR_INDEX_ENGINE" not in _index_env(tmp_path, None)


def test_every_run_indexes_with_the_selection_main_is_given(
    monkeypatch, tmp_path: Path
) -> None:
    asked: list[str | None] = []

    def graphs(
        cgr: list[str],
        repo: ReferenceRepo,
        checkout: Path,
        runs: int,
        index_engine: str | None,
    ) -> tuple[list[str], list[tuple[int, list[str]]]]:
        asked.append(index_engine)
        return read_graph(graph_path(repo)), []

    monkeypatch.setattr(reference_graphs, "_runs_this_python", lambda cgr: True)
    monkeypatch.setattr(
        reference_graphs, "probe", lambda: read_graph(environment_path())
    )
    monkeypatch.setattr(reference_graphs, "fetch", lambda repo, cache: tmp_path)
    monkeypatch.setattr(reference_graphs, "_graphs", graphs)

    assert reference_graphs.main(["check", "uuid", "--index-engine", "go=rust"]) == 0
    assert reference_graphs.main(["check", "uuid"]) == 0
    assert asked == ["go=rust", None]


def test_an_invalid_selection_is_refused_before_any_index(monkeypatch, capsys) -> None:
    monkeypatch.setattr(reference_graphs, "fetch", _not_indexed)

    with pytest.raises(SystemExit) as exited:
        reference_graphs.main(["check", "--index-engine", "go=fortran"])

    assert exited.value.code == 2
    assert "'fortran' is not an engine" in capsys.readouterr().err


def test_a_forward_slash_spelling_of_the_root_is_replaced_too() -> None:
    root = Path("/work/checkout/demo")
    export = _export([1, 2, 3, 4])
    nodes = export["nodes"]
    assert isinstance(nodes, list)
    folder = nodes[2]
    assert isinstance(folder, dict)
    properties = folder["properties"]
    assert isinstance(properties, dict)
    properties["absolute_path"] = f"{root.as_posix()}/pkg"

    text = "\n".join(canonical(export, root))

    assert root.as_posix() not in text


def test_an_export_without_its_lists_is_refused() -> None:
    with pytest.raises(SystemExit):
        canonical({"nodes": []}, ROOT)
    assert canonical({"nodes": [], "relationships": []}, ROOT) == []


def test_a_recording_whose_runs_disagree_writes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    repo = load_manifest()[0]
    graph = graph_path(repo, tmp_path)
    graph.write_text("kept\n", encoding="utf-8")
    monkeypatch.setattr(reference_graphs, "_runs_this_python", lambda cgr: True)
    monkeypatch.setattr(reference_graphs, "probe", lambda: ENVIRONMENT)
    monkeypatch.setattr(reference_graphs, "fetch", lambda repo, cache: tmp_path)
    monkeypatch.setattr(
        reference_graphs,
        "_graphs",
        lambda cgr, repo, checkout, runs, index_engine: (["a"], [(1, ["b"])]),
    )

    status = reference_graphs.run("record", [repo], ["cgr"], tmp_path, 2, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert graph.read_text(encoding="utf-8") == "kept\n"
    assert not environment_path(tmp_path).exists()


def _machine(monkeypatch, environment: list[str], graph: list[str]) -> None:
    """A machine that parses `environment` and indexes every repository
    into `graph`."""
    monkeypatch.setattr(reference_graphs, "_runs_this_python", lambda cgr: True)
    monkeypatch.setattr(reference_graphs, "probe", lambda: environment)
    monkeypatch.setattr(reference_graphs, "fetch", lambda repo, cache: Path())
    monkeypatch.setattr(
        reference_graphs,
        "_graphs",
        lambda cgr, repo, checkout, runs, index_engine: (graph, []),
    )


def _not_indexed(repo: ReferenceRepo, cache: Path) -> Path:
    raise AssertionError(f"{repo['name']} fetched on a machine that cannot compare")


def test_a_machine_parsing_what_was_recorded_compares_the_graphs(
    monkeypatch, tmp_path: Path
) -> None:
    repo = load_manifest()[0]
    write_graph(environment_path(tmp_path), ENVIRONMENT)
    write_graph(graph_path(repo, tmp_path), ["a"])
    _machine(monkeypatch, ENVIRONMENT, ["a"])

    assert reference_graphs.run("check", [repo], ["cgr"], tmp_path, 1, tmp_path) == 0

    _machine(monkeypatch, ENVIRONMENT, ["b"])
    status = reference_graphs.run("check", [repo], ["cgr"], tmp_path, 1, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT


def test_a_machine_missing_a_language_is_told_so_before_any_index(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = load_manifest()[0]
    write_graph(environment_path(tmp_path), ENVIRONMENT)
    write_graph(graph_path(repo, tmp_path), ["a"])
    _machine(monkeypatch, WITHOUT_AST_GREP, ["a"])
    monkeypatch.setattr(reference_graphs, "fetch", _not_indexed)

    status = reference_graphs.run("check", [repo], ["cgr"], tmp_path, 1, tmp_path)
    out = capsys.readouterr().out

    assert status == cs.REFERENCE_EXIT_FAILED
    assert "this machine cannot parse: bash, ruby (ast-grep missing)" in out
    assert cs.REFERENCE_INSTALL in out
    assert len(out.splitlines()) == 1


def test_a_machine_parsing_more_than_was_recorded_is_told_so(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = load_manifest()[0]
    write_graph(environment_path(tmp_path), WITHOUT_AST_GREP)
    write_graph(graph_path(repo, tmp_path), ["a"])
    _machine(monkeypatch, ENVIRONMENT, ["a"])

    status = reference_graphs.run("check", [repo], ["cgr"], tmp_path, 1, tmp_path)

    assert status == cs.REFERENCE_EXIT_FAILED
    assert "this machine also parses: bash, ruby (ast-grep)" in capsys.readouterr().out


def test_a_check_without_the_recorded_environment_asks_for_a_new_recording(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    repo = load_manifest()[0]
    write_graph(graph_path(repo, tmp_path), ["a"])
    _machine(monkeypatch, ENVIRONMENT, ["a"])
    monkeypatch.setattr(reference_graphs, "fetch", _not_indexed)

    status = reference_graphs.run("check", [repo], ["cgr"], tmp_path, 1, tmp_path)
    out = capsys.readouterr().out

    assert status == cs.REFERENCE_EXIT_FAILED
    assert str(environment_path(tmp_path)) in out
    assert cs.REFERENCE_REGENERATE in out


def test_a_full_recording_writes_the_environment_it_ran_in(
    monkeypatch, tmp_path: Path
) -> None:
    repo = load_manifest()[0]
    write_graph(environment_path(tmp_path), WITHOUT_AST_GREP)
    _machine(monkeypatch, ENVIRONMENT, ["a"])

    status = reference_graphs.run("record", [repo], ["cgr"], tmp_path, 1, tmp_path)

    assert status == 0
    assert read_graph(environment_path(tmp_path)) == ENVIRONMENT
    assert read_graph(graph_path(repo, tmp_path)) == ["a"]


def test_a_partial_recording_must_parse_what_the_others_were_recorded_with(
    monkeypatch, tmp_path: Path
) -> None:
    repo = load_manifest()[0]
    write_graph(environment_path(tmp_path), ENVIRONMENT)
    write_graph(graph_path(repo, tmp_path), ["kept"])
    _machine(monkeypatch, WITHOUT_AST_GREP, ["a"])

    status = reference_graphs.run(
        "record", [repo], ["cgr"], tmp_path, 1, tmp_path, partial=True
    )

    assert status == cs.REFERENCE_EXIT_FAILED
    assert read_graph(graph_path(repo, tmp_path)) == ["kept"]
    assert read_graph(environment_path(tmp_path)) == ENVIRONMENT

    _machine(monkeypatch, ENVIRONMENT, ["a"])
    status = reference_graphs.run(
        "record", [repo], ["cgr"], tmp_path, 1, tmp_path, partial=True
    )

    assert status == 0
    assert read_graph(graph_path(repo, tmp_path)) == ["a"]


def _another_cgr(monkeypatch) -> None:
    """A `--cgr` that is not this Python's: the probe cannot describe it."""
    monkeypatch.setattr(reference_graphs, "_runs_this_python", lambda cgr: False)

    def _no_probe() -> list[str]:
        raise AssertionError("probed this Python for another cgr")

    monkeypatch.setattr(reference_graphs, "probe", _no_probe)


def test_a_recording_with_another_cgr_is_refused_before_any_index(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # environment.txt is read under this Python; graphs indexed by another
    # install would be recorded beside a list of what that install never
    # parsed (CodeRabbit review of PR #3640).
    repo = load_manifest()[0]
    write_graph(graph_path(repo, tmp_path), ["kept"])
    _another_cgr(monkeypatch)
    monkeypatch.setattr(reference_graphs, "fetch", _not_indexed)

    status = reference_graphs.run(
        "record", [repo], ["/elsewhere/cgr"], tmp_path, 1, tmp_path
    )
    out = capsys.readouterr().out

    assert status == cs.REFERENCE_EXIT_FAILED
    assert "/elsewhere/cgr" in out
    assert cs.REFERENCE_REGENERATE in out
    assert read_graph(graph_path(repo, tmp_path)) == ["kept"]
    assert not environment_path(tmp_path).exists()


def test_a_check_with_another_cgr_compares_only_the_graphs(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # The Rust engine is checked against these graphs: what this Python
    # parses says nothing about it, so the environment is not compared, and
    # saying so keeps the skip visible.
    repo = load_manifest()[0]
    write_graph(graph_path(repo, tmp_path), ["a"])
    _another_cgr(monkeypatch)
    monkeypatch.setattr(reference_graphs, "fetch", lambda repo, cache: Path())
    monkeypatch.setattr(
        reference_graphs,
        "_graphs",
        lambda cgr, repo, checkout, runs, index_engine: (["a"], []),
    )

    status = reference_graphs.run("check", [repo], ["rust-cgr"], tmp_path, 1, tmp_path)

    assert status == 0
    assert "rust-cgr" in capsys.readouterr().out

    monkeypatch.setattr(
        reference_graphs,
        "_graphs",
        lambda cgr, repo, checkout, runs, index_engine: (["b"], []),
    )
    status = reference_graphs.run("check", [repo], ["rust-cgr"], tmp_path, 1, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT


def test_only_this_pythons_cgr_or_interpreter_runs_under_this_python() -> None:
    assert reference_graphs._runs_this_python([sys.executable, "-m", "codebase_rag"])
    own = shutil.which(cs.REFERENCE_CGR_PROGRAM, path=str(Path(sys.executable).parent))
    if own is not None:
        assert reference_graphs._runs_this_python([own])
    other = shutil.which("git")
    if other is not None:
        assert not reference_graphs._runs_this_python([other])
    assert not reference_graphs._runs_this_python(["no-such-cgr-anywhere"])


def test_the_probe_reports_what_an_index_would_parse() -> None:
    # In a process of its own, with the index's environment: the frontends
    # are the forced ones whatever this process's settings say.
    environment = reference_graphs.probe()

    for setting, mode in cs.REFERENCE_ENV.items():
        if setting.endswith("_FRONTEND"):
            assert f"{cs.REFERENCE_FRONTEND_TIER}{_SEP}{setting}={mode}" in environment
    assert f"{cs.LanguageTier.TREE_SITTER}{_SEP}python" in environment
    ast_grep = [
        line for line in environment if line.startswith(cs.LanguageTier.AST_GREP)
    ]
    try:
        import ast_grep_py  # noqa: F401
    except ImportError:
        assert ast_grep == []
    else:
        assert f"{cs.LanguageTier.AST_GREP}{_SEP}ruby" in ast_grep
    assert environment == sorted(environment)


def test_the_recorded_environment_parses_every_listed_repository() -> None:
    recorded = read_graph(environment_path())

    for repo in load_manifest():
        assert f"{cs.LanguageTier.TREE_SITTER}{_SEP}{repo['language']}" in recorded


def test_the_ci_job_installs_what_the_graphs_are_recorded_with() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding=cs.ENCODING_UTF8))
    steps = workflow["jobs"]["reference-graphs"]["steps"]
    flags = [arg for extra in cs.REFERENCE_EXTRAS for arg in ("--extra", extra)]
    installs = [
        shlex.split(step["run"])
        for step in steps
        if step.get("run", "").startswith("uv sync")
    ]

    assert installs == [["uv", "sync", *flags]]
    assert shlex.split(cs.REFERENCE_INSTALL) == ["uv", "sync", *flags]
    assert shlex.split(cs.REFERENCE_REGENERATE)[: 2 + len(flags)] == [
        "uv",
        "run",
        *flags,
    ]
    assert set(cs.TIER_EXTRAS.values()) <= set(cs.REFERENCE_EXTRAS)


def test_the_git_calls_ignore_a_repository_location_in_the_environment(
    monkeypatch,
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GIT_DIR", "/elsewhere/.git")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GIT_CONFIG_GLOBAL", "/g")

    env = reference_graphs._git_env()

    assert "GIT_DIR" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/g"


def test_reading_the_setting_names_leaves_the_environment_alone(
    tmp_path: Path,
) -> None:
    # In a process of its own: the settings module is imported once per
    # process, and this one has imported it already.
    (tmp_path / ".env").write_text("CGR_REFERENCE_PROBE=1\n", encoding="utf-8")
    probe = (
        "import os\n"
        "from scripts import reference_graphs\n"
        "reference_graphs._setting_env_names()\n"
        "print('CGR_REFERENCE_PROBE' in os.environ)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(reference_graphs.PROJECT_ROOT)}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_a_relative_cgr_program_is_made_absolute() -> None:
    program, *rest = cgr_command("./target/release/cgr --flag")

    assert Path(program).is_absolute()
    assert rest == ["--flag"]
    assert cgr_command("cgr") == ["cgr"]


def test_no_recorded_graph_names_a_directory_outside_its_checkout() -> None:
    # A path the placeholder missed would differ on every machine.
    for repo in load_manifest():
        for line in read_graph(graph_path(repo)):
            assert '"absolute_path":"/' not in line, (repo["name"], line[:200])
            assert '"root_path":"/' not in line, (repo["name"], line[:200])


def test_every_recorded_graph_is_listed() -> None:
    listed = {graph_path(repo).name for repo in load_manifest()}
    recorded = {
        path.name for path in REFERENCE_DIR.glob(f"*{cs.REFERENCE_GRAPH_SUFFIX}")
    }

    assert recorded == listed
