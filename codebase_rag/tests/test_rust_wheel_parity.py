"""`scripts/rust_wheel_parity.py` records what the installed cgr-graph-rs
answers on a small graph, then checks another wheel against it in a bare
container. Pinned against the installed extension: a recording checks clean
against itself, a changed answer is reported, and `check` imports nothing
from code-graph-rag, which the musl container does not have."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cgr_graph_rs")

from scripts import rust_wheel_parity as parity

SCRIPT = Path(parity.__file__).resolve()


@pytest.fixture
def recording(tmp_path: Path) -> Path:
    directory = tmp_path / "parity"
    assert parity.main(["record", str(directory)]) == 0
    return directory


def test_a_recording_holds_every_procedure_and_its_setup(recording: Path) -> None:
    # SQLite's -wal/-shm sidecars may sit beside the graph until the last
    # connection to it is collected, so only the two files the check reads
    # are required.
    assert {parity.EXPECTED, parity.GRAPH} <= {p.name for p in recording.iterdir()}
    setup = json.loads((recording / parity.EXPECTED).read_text(encoding="utf-8"))
    assert set(setup["answers"]) == {*parity.PROCEDURES, "render_json"}
    assert setup["answers"]["render_json"] == '{"k":[1,2.5,null,true,"\\u00e9"]}'
    # All 60 nodes of the tangle have calls, so no procedure answers vacuously.
    degrees = setup["answers"]["degree_centrality.get"]
    assert degrees.count("'node':") == 60
    assert "'degree': 0}" not in degrees
    assert {"version", "busy_ms", "cache_kib", "version_sql"} <= set(setup)


def test_recording_again_replaces_the_graph_and_answers_alike(
    recording: Path,
) -> None:
    first = (recording / parity.EXPECTED).read_text(encoding="utf-8")
    assert parity.main(["record", str(recording)]) == 0
    assert (recording / parity.EXPECTED).read_text(encoding="utf-8") == first


def test_a_wheel_checked_against_its_own_recording_is_identical(
    recording: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert parity.main(["check", str(recording)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"{name}: identical" for name in (*parity.PROCEDURES, "render_json")
    ]


def test_a_changed_answer_fails_the_check_and_is_named(
    recording: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    expected = recording / parity.EXPECTED
    setup = json.loads(expected.read_text(encoding="utf-8"))
    setup["answers"]["pagerank.get"] = "[]"
    expected.write_text(json.dumps(setup), encoding="utf-8")

    assert parity.main(["check", str(recording)]) == 1
    out = capsys.readouterr().out
    assert "pagerank.get: DIFFERENT" in out
    assert out.count("DIFFERENT") == 1


def test_check_runs_without_code_graph_rag(recording: Path) -> None:
    # The check job installs only the wheel in a bare container, so the
    # script must reach a verdict with code-graph-rag unimportable.
    probe = (
        "import runpy, sys\n"
        "sys.modules['codebase_rag'] = None\n"
        f"sys.argv = ['rust_wheel_parity.py', 'check', {str(recording)!r}]\n"
        f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(": identical") == len(parity.PROCEDURES) + 1


def test_an_unknown_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parity.main(["compare", str(tmp_path)])
