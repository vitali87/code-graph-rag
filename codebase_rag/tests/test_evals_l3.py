"""`python -m evals.l3` grades cgr's CALLS against execution: it traces cgr
indexing a multi-language fixture and reports the traced calls cgr's static
graph of its own source misses. The static index and the trace of the real
source take minutes, so both are replaced by fixed edge sets here; the
fixture, the workload that indexes it and the report built from the two sets
run for real."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag.config import settings
from codebase_rag.language_spec import get_language_for_extension
from codebase_rag.parser_loader import load_parsers
from evals import constants as ec
from evals import l3

_STATIC = {("p.a.run", "p.a.helper"), ("p.a.run", "p.b.other")}
_TRACED = {
    ("p.a.run", "p.a.helper"),
    ("p.a.run", "p.a.Dog.__init__"),
    ("p.b.greet", "p.b.describe"),
}


@pytest.fixture(autouse=True)
def _skip_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The workload indexes into a null ingestor; embedding would only import
    # torch.
    monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", True)


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Console:
    recording = Console(record=True, width=120, color_system=None)
    monkeypatch.setattr(l3, "console", recording)
    return recording


def _patch_sources(
    monkeypatch: pytest.MonkeyPatch,
    traced: set[tuple[str, str]],
    run_workload: bool,
) -> list[tuple[str, Path, str]]:
    seen: list[tuple[str, Path, str]] = []

    def static(target: Path, project: str) -> set[tuple[str, str]]:
        seen.append(("static", target, project))
        return set(_STATIC)

    def trace(
        workload: Callable[[], None], target: Path, project: str
    ) -> set[tuple[str, str]]:
        seen.append(("trace", target, project))
        if run_workload:
            workload()
        return set(traced)

    monkeypatch.setattr(l3, "extract_cgr_calls", static)
    monkeypatch.setattr(l3, "trace_calls", trace)
    return seen


@pytest.mark.parametrize(
    ("qn", "dunder"),
    [
        ("p.a.Dog.__init__", True),
        ("__call__", True),
        ("p.a.helper", False),
        ("p.a.__private", False),
        ("p.a._sunder_", False),
    ],
)
def test_only_a_double_underscore_name_on_both_sides_is_a_dunder(
    qn: str, dunder: bool
) -> None:
    assert l3._is_dunder_callee(qn) is dunder


def test_the_fixture_is_written_whole_and_rewritten_in_place(tmp_path: Path) -> None:
    l3._write_fixture(tmp_path)
    l3._write_fixture(tmp_path)
    files = sorted(p.name for p in (tmp_path / "fixture").iterdir())
    assert len(files) == 18
    assert (tmp_path / "fixture" / "a.py").read_text(encoding="utf-8") == l3.FIXTURE_A
    assert (tmp_path / "fixture" / "__init__.py").read_bytes() == b""


def test_every_fixture_file_parses_cleanly(tmp_path: Path) -> None:
    # A syntax error would send the traced workload through the grammar's
    # error recovery instead of the extraction code it is meant to exercise.
    l3._write_fixture(tmp_path)
    parsers, _ = load_parsers()
    broken = []
    for path in sorted((tmp_path / "fixture").iterdir()):
        language = get_language_for_extension(path.suffix)
        assert language is not None, path.name
        tree = parsers[language].parse(path.read_bytes())
        if tree.root_node.has_error:
            broken.append(path.name)
    assert broken == []


def test_main_reports_the_traced_calls_the_static_graph_misses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    target = tmp_path / "src"
    target.mkdir()
    out_dir = tmp_path / "out"
    seen = _patch_sources(monkeypatch, _TRACED, run_workload=True)

    l3.main(target=target, project_name="", out_dir=out_dir)

    # Both sides name the same source and project, which defaults to the
    # target directory's name.
    assert seen == [
        ("static", target.resolve(), "src"),
        ("trace", target.resolve(), "src"),
    ]
    assert (out_dir / ec.L3_WORKSPACE / "fixture" / "Service.java").is_file()
    diff = json.loads((out_dir / ec.L3_DIFF_FILENAME).read_text(encoding="utf-8"))
    assert diff == {
        "missing": ["p.a.run -> p.a.Dog.__init__", "p.b.greet -> p.b.describe"]
    }
    rows = {
        line.split("│")[1].strip(): [c.strip() for c in line.split("│")[2:-1]]
        for line in console.export_text().splitlines()
        if line.startswith("│")
    }
    # The dunder callee counts toward all calls but not the explicit ones.
    assert rows == {
        "all calls": ["3", "1", "2", "0.3333"],
        "explicit (no dunders)": ["2", "1", "1", "0.5000"],
    }


def test_nothing_traced_reads_as_full_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    _patch_sources(monkeypatch, set(), run_workload=False)

    l3.main(target=tmp_path, project_name="proj", out_dir=tmp_path / "out")

    diff = json.loads(
        (tmp_path / "out" / ec.L3_DIFF_FILENAME).read_text(encoding="utf-8")
    )
    assert diff == {"missing": []}
    text = console.export_text()
    assert text.count("1.0000") == 2


def test_the_command_reads_its_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    seen = _patch_sources(monkeypatch, set(), run_workload=False)
    # What `typer.run(main)` builds when the module runs.
    app = typer.Typer()
    app.command()(l3.main)
    out_dir = tmp_path / "nested" / "out"

    result = CliRunner().invoke(
        app,
        ["--target", str(tmp_path), "--project-name", "cgr", "--out-dir", str(out_dir)],
    )

    assert result.exit_code == 0, result.output
    assert seen[0] == ("static", tmp_path.resolve(), "cgr")
    assert (out_dir / ec.L3_DIFF_FILENAME).is_file()
