"""The context slice's printed output stays within its token budget, fast.

`context` counted only each piece's source against the budget, listed every
candidate that did not fit in `omitted`, fetched every reaching test's
definition before budgeting, and trimmed an oversized target one line and
one full re-encode at a time. A 2-line function reached by 2,000 tests
printed 163,019 tokens for a 4,000 budget after 21 s, and a 9,000-line class
took 87 s to trim (issue #3243).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import context_slice
from codebase_rag.context_slice import context
from codebase_rag.graph_query import DefinitionRow, QueryFn
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.token_utils import count_tokens
from evals.cgr_graph import _StatefulIngestor

PROJECT = "ctxb"
TEST_FILES = 6
TESTS_PER_FILE = 50
# The slice's own fields beside its pieces: target, resolved, the counts and
# a capped sample of what was left out.
ENVELOPE = 400


def _repo(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return store


def _central(root: Path) -> _StatefulIngestor:
    files = {"app.py": "def core(x):\n    return x + 1\n", "tests/__init__.py": ""}
    for f in range(TEST_FILES):
        body = "from app import core\n\n\n" + "".join(
            f"def test_{f}_{i}():\n    assert core({i}) == {i + 1}\n\n\n"
            for i in range(TESTS_PER_FILE)
        )
        files[f"tests/test_{f}.py"] = body
    return _repo(root, files)


def _printed(slice_: context_slice.ContextSlice) -> int:
    return count_tokens(json.dumps(slice_, indent=cs.MCP_JSON_INDENT))


@pytest.mark.parametrize("budget", [4000, 1000, 300])
def test_the_printed_slice_fits_the_budget(temp_repo: Path, budget: int) -> None:
    store = _central(temp_repo)

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.app.core", budget, temp_repo)

    total = TEST_FILES * TESTS_PER_FILE
    assert slice_["used_tokens"] <= budget
    assert slice_["used_tokens"] == sum(p["tokens"] for p in slice_["pieces"])
    assert _printed(slice_) <= budget + ENVELOPE, _printed(slice_)
    # What was left out is counted, and named only in a short sample.
    assert len(slice_["omitted"]) <= cs.CONTEXT_OMITTED_SAMPLE
    assert slice_["omitted_count"] > 0
    assert slice_["omitted_count"] + len(slice_["pieces"]) >= total


def test_a_piece_is_charged_for_its_fields(temp_repo: Path) -> None:
    store = _central(temp_repo)

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.app.core", 4000, temp_repo)

    for piece in slice_["pieces"]:
        assert piece["tokens"] > count_tokens(piece["source"]), piece


def test_each_quoted_file_is_read_once_and_no_test_is_fetched_by_name(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _central(temp_repo)
    reads: list[Path] = []
    real = context_slice.read_source_lines

    def counting(path: Path) -> list[str]:
        reads.append(path)
        return real(path)

    definitions: list[str] = []
    real_definition = context_slice.graph_query.definition

    def counting_definition(
        fetch_all: QueryFn, project: str, qualified_name: str, root: Path | None
    ) -> DefinitionRow:
        definitions.append(qualified_name)
        return real_definition(fetch_all, project, qualified_name, root)

    monkeypatch.setattr(context_slice, "read_source_lines", counting)
    monkeypatch.setattr(context_slice.graph_query, "definition", counting_definition)

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.app.core", 4000, temp_repo)

    # 300 tests and 300 call sites in six files reach `core`: each file is
    # decoded once, and only the target's definition comes from the graph.
    assert len(reads) == len(set(reads)) <= TEST_FILES, reads
    assert definitions == [f"{PROJECT}.app.core"]
    assert slice_["pieces"]


def test_nothing_is_read_once_the_budget_is_spent(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A budget the target fills leaves every caller and test unread.
    store = _central(temp_repo)
    full = context(store.fetch_all, PROJECT, f"{PROJECT}.app.core", 4000, temp_repo)
    target_tokens = full["pieces"][0]["tokens"]
    loads: list[tuple[str | None, int]] = []
    real = context_slice._lines

    def counting(root: Path | None, path: str | None, start: int, end: int) -> str:
        loads.append((path, start))
        return real(root, path, start, end)

    monkeypatch.setattr(context_slice, "_lines", counting)

    slice_ = context(
        store.fetch_all, PROJECT, f"{PROJECT}.app.core", target_tokens, temp_repo
    )

    assert [p["why_included"] for p in slice_["pieces"]] == [cs.CONTEXT_WHY_TARGET]
    assert loads == []
    assert slice_["omitted_count"] == 2 * TEST_FILES * TESTS_PER_FILE


def test_a_large_target_trims_without_re_encoding_every_line(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "class Big:\n" + "".join(
        f"    def m{i}(self, x):\n        return x * {i}\n\n" for i in range(1500)
    )
    store = _repo(temp_repo, {"big.py": body})
    calls = 0
    real = context_slice.count_tokens

    def counting(text: str) -> int:
        nonlocal calls
        calls += 1
        return real(text)

    monkeypatch.setattr(context_slice, "count_tokens", counting)
    started = time.perf_counter()

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.big.Big", 500, temp_repo)

    elapsed = time.perf_counter() - started
    assert slice_["truncated"]
    (target,) = slice_["pieces"]
    assert target["source"].startswith("class Big:\n    def m0(self, x):")
    assert slice_["used_tokens"] <= 500
    # Bisecting the line count: a few dozen encodes, not one per line.
    assert calls < 100, calls
    assert elapsed < 5, elapsed


def test_the_trim_keeps_the_most_whole_lines_that_fit(temp_repo: Path) -> None:
    # The bisected trim keeps exactly the lines a line-by-line trim kept.
    body = "class Big:\n" + "".join(
        f"    def m{i}(self, x):\n        return x * {i}\n\n" for i in range(60)
    )
    store = _repo(temp_repo, {"big.py": body})

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.big.Big", 300, temp_repo)

    (target,) = slice_["pieces"]
    lines = body.rstrip("\n").split("\n")
    kept = target["source"].split("\n")
    assert kept == lines[: len(kept)]
    one_more = "\n".join(lines[: len(kept) + 1])
    assert (
        context_slice._piece_tokens(
            f"{PROJECT}.big.Big",
            "big.py",
            [1, len(lines)],
            cs.CONTEXT_WHY_TARGET,
            one_more,
        )
        > 300
    )


def test_a_budget_that_exactly_fits_some_lines_keeps_them(temp_repo: Path) -> None:
    # A prefix whose piece costs exactly the budget fits: the trim's bound
    # is inclusive.
    body = "class Big:\n" + "".join(
        f"    def m{i}(self, x):\n        return x * {i}\n\n" for i in range(60)
    )
    store = _repo(temp_repo, {"big.py": body})
    lines = body.rstrip("\n").split("\n")
    full = context(store.fetch_all, PROJECT, f"{PROJECT}.big.Big", 10_000, temp_repo)
    span = full["pieces"][0]["span"]
    budget = context_slice._piece_tokens(
        f"{PROJECT}.big.Big",
        "big.py",
        span,
        cs.CONTEXT_WHY_TARGET,
        "\n".join(lines[:7]),
    )

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.big.Big", budget, temp_repo)

    assert slice_["pieces"][0]["source"] == "\n".join(lines[:7])
    assert slice_["used_tokens"] == budget


def test_a_neighbourhood_that_fits_omits_nothing(temp_repo: Path) -> None:
    # Negative: a small slice is whole, with nothing counted as left out.
    store = _repo(
        temp_repo,
        {
            "app.py": "def core(x):\n    return x + 1\n\n\ndef use():\n    return core(1)\n",
        },
    )

    slice_ = context(store.fetch_all, PROJECT, f"{PROJECT}.app.core", 4000, temp_repo)

    assert slice_["omitted"] == []
    assert slice_["omitted_count"] == 0
    assert {p["why_included"] for p in slice_["pieces"]} == {
        cs.CONTEXT_WHY_TARGET,
        cs.CONTEXT_WHY_CALLER,
    }
