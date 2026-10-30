"""TraceEval end to end, on a hand-built corpus in the benchmark's layout.

No download: a tiny `benchmark/<language>/<id>/` tree with tracer-style
`callgraph.json` files stands in for the real archive, and each program is
indexed with cgr exactly as the eval indexes the real corpus. The truth
edges are what the benchmark's tracers would have recorded for these
programs, so the scores pin the whole pipeline: staging, capture, naming,
normalization and the micro/macro arithmetic.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from evals.traceeval import constants as tc
from evals.traceeval import corpus as corpus_module
from evals.traceeval.corpus import CorpusError, ensure_corpus, ground_truth, programs
from evals.traceeval.runner import run_programs
from evals.traceeval.score import score_results

PY_PROGRAM = """class Greeter:
    def __init__(self, name):
        self.name = name

    def greet(self):
        return shout(self.name)


def shout(text):
    return text.upper()


if __name__ == "__main__":
    print(Greeter("x").greet())
"""
PY_TRUTH = {
    "main": ["main.Greeter.__init__", "main.Greeter.greet", "builtins.print"],
    "main.Greeter.greet": ["main.shout"],
    "main.shout": [],
}

JS_PROGRAM = """function double(x) {
  return x * 2;
}

function run() {
  return double(2);
}

console.log(run());
"""
JS_TRUTH = {"main": ["main.run", "main.console.log"], "main.run": ["main.double"]}

JAVA_PROGRAM = """package demo.app;

public class Main {
    static int twice(int n) { return n * 2; }

    public static void main(String[] args) {
        Helper h = new Helper("a");
        System.out.println(h.size() + twice(1));
    }
}

class Helper {
    private final String label;
    Helper(String label) { this.label = label; }
    int size() { return label.length(); }
}
"""
JAVA_TRUTH = {
    "demo.app.Main:main(java.lang.String[])": [
        "demo.app.Helper:<init>(java.lang.String)",
        "demo.app.Helper:size()",
        "demo.app.Main:twice(int)",
    ],
    # Recorded at runtime, never visible statically: an untaken-branch-free
    # miss that keeps Java recall below 1 on purpose.
    "demo.app.Helper:size()": ["demo.app.Main:twice(int)"],
}


def _program(
    root: Path,
    language: str,
    pid: str,
    files: dict[str, str],
    truth: dict[str, list[str]],
) -> None:
    directory = root / language / pid
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (directory / tc.GROUND_TRUTH_FILENAME).write_text(
        json.dumps(truth), encoding="utf-8"
    )
    (directory / "README.md").write_text("Source: test", encoding="utf-8")


@pytest.fixture
def benchmark(tmp_path: Path) -> Path:
    root = tmp_path / "benchmark"
    _program(root, "python", "py_0001", {"main.py": PY_PROGRAM}, PY_TRUTH)
    _program(root, "javascript", "js_0001", {"main.js": JS_PROGRAM}, JS_TRUTH)
    _program(
        root, "java", "java_0001", {"demo/app/Main.java": JAVA_PROGRAM}, JAVA_TRUTH
    )
    return root


def test_ground_truth_flattens_the_callgraph(benchmark: Path) -> None:
    edges = ground_truth(benchmark / "python" / "py_0001")

    assert ("main.Greeter.greet", "main.shout") in edges
    assert len(edges) == 4


def test_scores_each_language(benchmark: Path, tmp_path: Path) -> None:
    ids = {
        tc.Language.PYTHON: ["py_0001", "absent"],
        tc.Language.JAVASCRIPT: ["js_0001"],
        tc.Language.JAVA: ["java_0001"],
    }
    selected = programs(benchmark, ids, list(tc.Language))
    assert [p.program_id for p in selected] == ["py_0001", "js_0001", "java_0001"]

    results = run_programs(selected, tmp_path / "work", workers=1, quiet=False)
    assert not [r.error for r in results if r.error]
    rows = {row["label"]: row for row in score_results(results).rows}

    # print and console.log are stdlib and leave both sides; the rest of the
    # truth is exactly what cgr resolves.
    for label in ("python", "javascript", "python:static", "javascript:static"):
        assert (rows[label]["precision"], rows[label]["recall"]) == (1.0, 1.0), label
    assert (rows["java"]["tp"], rows["java"]["fp"], rows["java"]["fn"]) == (3, 0, 1)
    macro = rows["macro_avg"]
    assert macro["recall"] == round((1.0 + 1.0 + 0.75) / 3, 4)


def _zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, text in members.items():
            bundle.writestr(name, text)


def _fake_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: dict[str, str]
) -> Path:
    checkout = tmp_path / "corpora" / tc.CORPUS_NAME
    archive = checkout / tc.CORPUS_ZIP
    archive.parent.mkdir(parents=True)
    _zip(archive, members)
    monkeypatch.setattr(corpus_module, "_ensure_corpus", lambda _spec, _dir: checkout)
    return archive


def test_extraction_skips_macos_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _fake_checkout(
        tmp_path,
        monkeypatch,
        {
            "benchmark/python/a/main.py": "x = 1\n",
            "__MACOSX/benchmark/._python": "junk",
            "benchmark/.DS_Store": "junk",
        },
    )
    monkeypatch.setattr(tc, "CORPUS_ZIP_SHA256", corpus_module._sha256(archive))

    root = ensure_corpus(tmp_path / "corpora")

    assert (root / "python" / "a" / "main.py").is_file()
    assert not (root / ".DS_Store").exists()
    assert not (root.parent / "__MACOSX").exists()


def test_a_tampered_archive_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_checkout(tmp_path, monkeypatch, {"benchmark/python/a/main.py": "x = 1\n"})

    with pytest.raises(CorpusError):
        ensure_corpus(tmp_path / "corpora")


def test_path_traversal_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _fake_checkout(tmp_path, monkeypatch, {"../escape.py": "x = 1\n"})
    monkeypatch.setattr(tc, "CORPUS_ZIP_SHA256", corpus_module._sha256(archive))

    with pytest.raises(CorpusError):
        ensure_corpus(tmp_path / "corpora")
