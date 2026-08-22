import json
from pathlib import Path

from evals.indexing_bench import CORPORA, markdown_row, measure
from evals.types_defs import IndexingStats

_CORE = """\
def helper():
    return 1


def caller():
    return helper()
"""

_USER = """\
from core import caller


def run():
    return caller()
"""


def _write_fixture(root: Path) -> None:
    (root / "core.py").write_text(_CORE)
    (root / "user.py").write_text(_USER)


def test_measure_reports_graph_size_and_timings(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    stats = measure(tmp_path, "fixture")
    assert stats["corpus"] == "fixture"
    assert stats["modules"] == 2
    assert stats["files"] == 2
    assert stats["nodes"] > stats["modules"]
    assert stats["edges"] > 0
    assert stats["build_s"] > 0
    assert stats["parser_load_s"] >= 0
    # The result must survive the child-process JSON hop verbatim.
    assert json.loads(json.dumps(stats)) == stats


def test_markdown_row_pins_corpus_commit_and_scale(tmp_path: Path) -> None:
    stats = IndexingStats(
        corpus="django",
        commit="9e7cc2b628fe8fd3895986af9b7fc9525034c1b0",
        parser_load_s=1.5,
        build_s=63.0,
        peak_rss_mib=1536.0,
        nodes=50000,
        edges=120000,
        modules=2900,
        files=2900,
        python="3.12.13",
        platform="darwin",
    )
    row = markdown_row(stats)
    assert "`django` @ `9e7cc2b62`" in row
    assert "63 s wall" in row
    assert "1.5 GiB peak RSS" in row
    assert "50000 nodes / 120000 edges" in row
    assert row.startswith("|") and row.endswith("| — |")


def test_markdown_row_without_rss_or_commit() -> None:
    stats = IndexingStats(
        corpus="adhoc",
        commit="",
        parser_load_s=0.1,
        build_s=2.0,
        peak_rss_mib=None,
        nodes=10,
        edges=5,
        modules=2,
        files=2,
        python="3.12.13",
        platform="win32",
    )
    row = markdown_row(stats)
    assert "@ `local`" in row
    assert "n/a peak RSS" in row


def test_corpora_are_pinned_to_full_shas() -> None:
    for spec in CORPORA.values():
        assert len(spec.commit) == 40, spec
        assert spec.url.startswith("https://"), spec
