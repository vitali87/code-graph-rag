"""The re-ingest benchmark edits a real file in the user's corpus over and
over. It is only safe to point at a working tree if every run leaves that file
byte- and timestamp-identical and drops the state it wrote, and its numbers
only mean something if each iteration re-parses the one file it edited."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from benchmarks import bench_reingest as bench
from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.sync.checkout_state import state_dir

_UTIL = "def helper():\n    return 1\n"
_APP = "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"


@pytest.fixture(autouse=True)
def _skip_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The embeddings pass would import torch for a corpus with nothing to
    # embed; the benchmark measures parsing and graph construction.
    monkeypatch.setattr(settings, "SKIP_EMBEDDINGS", True)


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    yield
    # measure_reingest swaps loguru's sinks for an INFO one on stderr; the
    # suite runs with none installed.
    logger.remove()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(_UTIL, encoding="utf-8")
    (root / "pkg" / "app.py").write_text(_APP, encoding="utf-8")
    return root


def test_percentile_picks_the_nearest_sample() -> None:
    samples = [40.0, 10.0, 30.0, 20.0]
    assert bench._percentile(samples, bench.PERCENTILE_95) == 40.0
    assert bench._percentile(samples, 0.0) == 10.0
    assert bench._percentile([7.0], bench.PERCENTILE_95) == 7.0


def test_the_edit_toggles_without_leaving_a_trace(corpus: Path) -> None:
    target = corpus / "pkg" / "util.py"
    target.chmod(0o640)

    bench._toggle_edit(target, 0)
    assert target.read_text(encoding="utf-8") == _UTIL + bench.NEUTRAL_EDIT
    bench._toggle_edit(target, 1)

    assert target.read_text(encoding="utf-8") == _UTIL
    # Swapped in from a sibling, which must not outlive the swap or change
    # the file's permissions.
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "__init__.py",
        "app.py",
        "util.py",
    ]
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o640


def test_a_measurement_restores_the_corpus_it_edited(corpus: Path) -> None:
    target = corpus / "pkg" / "util.py"
    before = target.stat()

    result = bench.measure_reingest(corpus, Path("pkg/util.py"), 2, 1)

    assert result.corpus_name == "corpus"
    assert result.target == "pkg/util.py"
    assert (result.file_count, result.line_count) == (3, 7)
    assert (result.iterations, result.full_update_runs) == (2, 1)
    # Each re-ingest re-parses the edited file and nothing else.
    assert result.reparsed_files == 1
    assert 0 < result.reingest_p50_ms <= result.reingest_max_ms
    assert result.reingest_p95_ms <= result.reingest_max_ms
    assert result.full_update_p50_ms > 0

    assert target.read_text(encoding="utf-8") == _UTIL
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    for name in (cs.HASH_CACHE_FILENAME, cs.DIR_MTIMES_FILENAME):
        assert not (state_dir(corpus) / name).exists(), name


@pytest.mark.parametrize(
    ("iterations", "target", "message"),
    [
        (0, "pkg/util.py", "iterations must be positive"),
        (1, "../outside.py", "must be inside the corpus"),
        (1, "pkg/missing.py", "does not exist"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_edited(
    corpus: Path, iterations: int, target: str, message: str
) -> None:
    (corpus.parent / "outside.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        bench.measure_reingest(corpus, Path(target), iterations, 1)


def test_the_markdown_row_reads_as_a_table_row() -> None:
    result = bench.ReingestMeasurement(
        corpus_name="cgr",
        corpus_path="/src/cgr",
        target="a.py",
        file_count=12,
        line_count=34567,
        iterations=5,
        reingest_p50_ms=101.4,
        reingest_p95_ms=150.6,
        reingest_max_ms=170.0,
        reparsed_files=1,
        affected_files=3,
        delta_p50_ms=2.2,
        delta_p95_ms=3.7,
        full_update_p50_ms=900.0,
        full_update_runs=3,
    )
    assert bench.format_markdown_row(result) == (
        "| cgr (12 files, 34,567 lines) | `a.py` (+3 dependents) "
        "| 101 ms | 151 ms | 2 ms | 4 ms | 900 ms |"
    )


def test_main_emits_the_measurement_as_json(
    corpus: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["bench_reingest", str(corpus), "--file", "pkg/app.py"]
    argv += ["--iterations", "1", "--full-runs", "0", "--json"]
    monkeypatch.setattr(sys, "argv", argv)

    assert bench.main() == 0

    emitted = json.loads(capsys.readouterr().out)
    assert emitted["target"] == "pkg/app.py"
    assert emitted["iterations"] == 1
    # No full runs asked for, so none is reported rather than a zero-sample
    # median raising.
    assert (emitted["full_update_runs"], emitted["full_update_p50_ms"]) == (0, 0.0)
    assert (corpus / "pkg" / "app.py").read_text(encoding="utf-8") == _APP
