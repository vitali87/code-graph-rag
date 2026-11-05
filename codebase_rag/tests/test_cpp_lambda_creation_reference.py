"""Issue #2732: a C++ lambda is reachable from the function that creates it.

A lambda is a value used where it is written, but its node got no incoming
CALLS or REFERENCES: the creation-site REFERENCES that keeps a Rust closure
reachable was Rust-only, so `cgr dead-code` reported every C++ lambda. A
lambda in a declaration (`auto sq = [...]`, `std::thread t([...])`) was also
DEFINEd by the module, because C++ lists `declaration` as a module boundary
and the parent walk stopped at the local declaration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor
from evals.dead_code import cgr_dead_code

PROJECT = "cpplam"

LAM = """#include <algorithm>
#include <functional>
#include <thread>
#include <vector>

static int helper(int v) { return v * 2; }

int sorted_sum(std::vector<int> xs) {
    std::sort(xs.begin(), xs.end(), [](int a, int b) { return helper(a) < helper(b); });
    int total = 0;
    std::for_each(xs.begin(), xs.end(), [&total](int v) { total += v; });
    return total;
}

int local_lambda() {
    auto sq = [](int v) { return v * v; };
    return sq(3);
}

int stored_callback() {
    std::function<int(int)> cb = [](int v) { return v + 1; };
    return cb(1);
}

void spawn() {
    std::thread t([] { helper(1); });
    t.join();
}

int main() {
    spawn();
    return sorted_sum({3, 1, 2}) + local_lambda() + stored_callback();
}
"""

OWNERS = {
    "lambda_8_36": "sorted_sum",
    "lambda_10_40": "sorted_sum",
    "lambda_15_14": "local_lambda",
    "lambda_20_33": "stored_callback",
    "lambda_25_18": "spawn",
}

EXTRA = """#include <vector>

auto global_cmp = [](int a, int b) { return a < b; };

int unused() {
    auto twice = [](int v) { return v * 2; };
    return twice(2);
}

struct Widget {
    int run();
};

int Widget::run() {
    auto inc = [](int v) { return v + 1; };
    return inc(1);
}
"""


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    missing = sorted(
        str(lang.value) for lang in (cs.SupportedLanguage.CPP,) if lang not in parsers
    )
    if missing:
        # A module-scoped fixture runs before the per-test grammar skip
        # hook is installed, so a base install must skip here.
        pytest.skip(f"{', '.join(missing)} parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return store


def _incoming(store: _StatefulIngestor, rel: str, leaf: str) -> set[str]:
    return {
        str(edge[1])
        for edge in store.keyed_edges
        if edge[2] == rel and str(edge[4]).endswith(f".{leaf}")
    }


@pytest.fixture(scope="module")
def lam(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    root = tmp_path_factory.mktemp(PROJECT)
    return _index(root, {"lam.cpp": LAM})


@pytest.fixture(scope="module")
def extra(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    root = tmp_path_factory.mktemp(PROJECT)
    return _index(root, {"extra.cpp": EXTRA})


@pytest.mark.parametrize(("leaf", "owner"), sorted(OWNERS.items()))
def test_a_lambda_is_referenced_by_the_function_that_creates_it(
    lam: _StatefulIngestor, leaf: str, owner: str
) -> None:
    references = _incoming(lam, cs.RelationshipType.REFERENCES.value, leaf)

    assert references == {f"{PROJECT}.lam.{owner}"}


@pytest.mark.parametrize(("leaf", "owner"), sorted(OWNERS.items()))
def test_a_lambda_is_defined_by_the_function_that_creates_it(
    lam: _StatefulIngestor, leaf: str, owner: str
) -> None:
    defines = _incoming(lam, cs.RelationshipType.DEFINES.value, leaf)

    assert defines == {f"{PROJECT}.lam.{owner}"}


def test_no_lambda_of_a_running_program_is_dead(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    (root / "lam.cpp").write_text(LAM, encoding="utf-8")

    dead = cgr_dead_code(root, PROJECT, default_dead_code_config(False, False))

    assert not [qn for qn in dead if ".lambda_" in qn]


def test_a_lambda_in_an_out_of_class_method_is_referenced_by_the_method(
    extra: _StatefulIngestor,
) -> None:
    lambdas = {
        str(edge[4]) for edge in extra.keyed_edges if ".lambda_14_" in str(edge[4])
    }
    (inc,) = lambdas
    leaf = inc.rsplit(".", 1)[-1]

    assert _incoming(extra, cs.RelationshipType.REFERENCES.value, leaf) == {
        f"{PROJECT}.extra.Widget.run"
    }


# Negative: what must not change.


def test_a_lambda_in_a_dead_function_stays_dead(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    (root / "extra.cpp").write_text(EXTRA, encoding="utf-8")

    dead = cgr_dead_code(root, PROJECT, default_dead_code_config(False, False))

    assert f"{PROJECT}.extra.unused" in dead
    assert [qn for qn in dead if ".lambda_5_" in qn]


def test_a_namespace_scope_lambda_is_still_defined_by_its_module(
    extra: _StatefulIngestor,
) -> None:
    (leaf,) = {
        str(edge[4]).rsplit(".", 1)[-1]
        for edge in extra.keyed_edges
        if ".lambda_2_" in str(edge[4])
    }

    assert _incoming(extra, cs.RelationshipType.DEFINES.value, leaf) == {
        f"{PROJECT}.extra"
    }
    assert _incoming(extra, cs.RelationshipType.REFERENCES.value, leaf) == set()


def test_the_enclosing_functions_are_still_defined_by_the_module(
    lam: _StatefulIngestor,
) -> None:
    for owner in set(OWNERS.values()):
        assert _incoming(lam, cs.RelationshipType.DEFINES.value, owner) == {
            f"{PROJECT}.lam"
        }
