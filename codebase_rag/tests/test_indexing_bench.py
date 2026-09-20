import json
from pathlib import Path

from codebase_rag import constants as cs
from evals.indexing_bench import CORPORA, markdown_row, measure_indexing_run
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
    stats = measure_indexing_run(tmp_path, "fixture")
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
    assert row.startswith("|")
    assert row.endswith("| — |")


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


def _git(checkout: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.email=bench@test",
            "-c",
            "user.name=bench",
            *args,
        ],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=True,
    )
    return proc.stdout.strip()


def test_ensure_corpus_resets_dirty_cached_checkout(tmp_path: Path) -> None:
    # A cached checkout at the right HEAD but with tracked edits or untracked
    # files would index modified content while the report pins the result to
    # spec.commit; the cache must be restored to the pinned tree (CodeRabbit
    # review on PR #1388).
    from evals.indexing_bench import CorpusSpec, _ensure_corpus

    corpus_dir = tmp_path / "corpora"
    checkout = corpus_dir / "demo"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    (checkout / "a.py").write_text("print('clean')\n")
    _git(checkout, "add", "a.py")
    _git(checkout, "commit", "-q", "--no-verify", "-m", "pin")
    sha = _git(checkout, "rev-parse", "HEAD")
    (checkout / "a.py").write_text("print('dirty')\n")
    (checkout / "stray.py").write_text("x = 1\n")

    spec = CorpusSpec(name="demo", url="unused", commit=sha, subdir="")
    target = _ensure_corpus(spec, corpus_dir)

    assert target == checkout
    assert (checkout / "a.py").read_text() == "print('clean')\n"
    assert not (checkout / "stray.py").exists()


def test_ensure_corpus_leaves_clean_checkout_untouched(tmp_path: Path) -> None:
    from evals.indexing_bench import CorpusSpec, _ensure_corpus

    corpus_dir = tmp_path / "corpora"
    checkout = corpus_dir / "demo"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    (checkout / "a.py").write_text("print('clean')\n")
    _git(checkout, "add", "a.py")
    _git(checkout, "commit", "-q", "--no-verify", "-m", "pin")
    sha = _git(checkout, "rev-parse", "HEAD")

    spec = CorpusSpec(name="demo", url="unused", commit=sha, subdir="")
    assert _ensure_corpus(spec, corpus_dir) == checkout
    assert (checkout / "a.py").read_text() == "print('clean')\n"


def test_ensure_corpus_cleans_untracked_after_commit_switch(tmp_path: Path) -> None:
    # When the cache sits at a DIFFERENT commit, the fetch/checkout path must
    # also drop untracked files: `git checkout --force` keeps non-conflicting
    # strays, which would leak into the indexed graph while the report
    # records spec.commit (CodeRabbit review on PR #1388).
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    _git(src, "config", "uploadpack.allowAnySHA1InWant", "true")
    (src / "a.py").write_text("one = 1\n")
    _git(src, "add", "a.py")
    _git(src, "commit", "-q", "--no-verify", "-m", "c1")
    c1 = _git(src, "rev-parse", "HEAD")
    (src / "a.py").write_text("two = 2\n")
    _git(src, "add", "a.py")
    _git(src, "commit", "-q", "--no-verify", "-m", "c2")
    c2 = _git(src, "rev-parse", "HEAD")

    from evals.indexing_bench import CorpusSpec, _ensure_corpus

    corpus_dir = tmp_path / "corpora"
    checkout = corpus_dir / "demo"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    _git(checkout, "fetch", "-q", str(src), c1)
    _git(checkout, "checkout", "-q", "--force", c1)
    (checkout / "stray.py").write_text("x = 1\n")

    spec = CorpusSpec(name="demo", url=str(src), commit=c2, subdir="")
    target = _ensure_corpus(spec, corpus_dir)

    assert target == checkout
    assert _git(checkout, "rev-parse", "HEAD") == c2
    assert (checkout / "a.py").read_text() == "two = 2\n"
    assert not (checkout / "stray.py").exists()


def test_ensure_corpus_removes_ignored_stray_at_pinned_commit(
    tmp_path: Path,
) -> None:
    # An IGNORED stale file hides from `status --porcelain` and survives
    # `git clean` without -x, yet still gets indexed while the report records
    # spec.commit (CodeRabbit review on PR #1388).
    corpus_dir = tmp_path / "corpora"
    checkout = corpus_dir / "demo"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    (checkout / ".gitignore").write_text("build/\n")
    (checkout / "a.py").write_text("print('clean')\n")
    _git(checkout, "add", ".gitignore", "a.py")
    _git(checkout, "commit", "-q", "--no-verify", "-m", "pin")
    sha = _git(checkout, "rev-parse", "HEAD")
    (checkout / "build").mkdir()
    (checkout / "build" / "stale.py").write_text("x = 1\n")

    from evals.indexing_bench import CorpusSpec, _ensure_corpus

    spec = CorpusSpec(name="demo", url="unused", commit=sha, subdir="")
    assert _ensure_corpus(spec, corpus_dir) == checkout
    assert not (checkout / "build" / "stale.py").exists()
