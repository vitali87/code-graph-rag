"""Issues #2742 and #2646: an `Optional[T]`, `T | None`, `Union[T, None]` or
quoted `"T"` annotation types a Python value as `T`.

Parameter annotations were stored verbatim, and a class-level attribute
annotation was used only when it was a bare identifier. So
`self.repo.find(k)` on `repo: Optional[Repo] = None` (or on `self.repo =
repo` from an `Optional[Repo]` `__init__` parameter) fell through to the
bare-name fallback and bound to `Cache.find`, a class the module never
imports, and `s.get()` on an `Optional[Store]` parameter got no edge at all.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "pyopt"

MODELS = """class Cache:
    def find(self, k):
        return None


class Repo:
    def find(self, k):
        return k
"""

SERVICE = """import typing
from collections.abc import Sequence
from typing import Optional, Union
from pathlib import Path

from models import Cache, Repo


class ByAnnotation:
    repo: Optional[Repo] = None

    def lookup(self, k):
        return self.repo.find(k)


class ByPipe:
    repo: Repo | None = None

    def lookup(self, k):
        return self.repo.find(k)


class ByInit:
    def __init__(self, repo: Optional[Repo] = None):
        self.repo = repo

    def lookup(self, k):
        return self.repo.find(k)


class ByUnion:
    repo: Union[Repo, None] = None

    def lookup(self, k):
        return self.repo.find(k)


class ByTypingOptional:
    repo: typing.Optional[Repo] = None

    def lookup(self, k):
        return self.repo.find(k)


class ByQuoted:
    repo: "Repo"

    def lookup(self, k):
        return self.repo.find(k)


class ByQuotedInit:
    def __init__(self, repo: "Optional[Repo]" = None):
        self.repo = repo

    def lookup(self, k):
        return self.repo.find(k)


class Holder:
    repos: list[Repo]

    def lookup(self, k):
        for r in self.repos:
            r.find(k)


class SeqHolder:
    repos: "Sequence[Repo]"

    def lookup(self, k):
        for r in self.repos:
            r.find(k)


class Bare:
    repo: Repo

    def lookup(self, k):
        return self.repo.find(k)


class Either:
    store: Union[Repo, Cache]

    def lookup(self, k):
        return self.store.find(k)


def opt(s: Optional[Repo]):
    return s.find(1)


def typing_opt(s: typing.Optional[Repo]):
    return s.find(1)


def union(s: Union[Repo, None]):
    return s.find(1)


def union_none_first(s: Union[None, Repo] = None):
    return s.find(1)


def quoted(s: "Repo"):
    return s.find(1)


def quoted_opt(s: "Optional[Repo]" = None):
    return s.find(1)


def plain(s: Repo):
    return s.find(1)


def pipe(s: Repo | None):
    return s.find(1)


def either(s: Union[Repo, Cache]):
    return s.find(1)
"""

REPO_FIND = f"{PROJECT}.models.Repo.find"
CACHE_FIND = f"{PROJECT}.models.Cache.find"


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    root = tmp_path_factory.mktemp("repo") / PROJECT
    root.mkdir()
    (root / "models.py").write_text(MODELS, encoding="utf-8")
    (root / "service.py").write_text(SERVICE, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    out: dict[str, dict[str, str]] = {}
    for edge in store.keyed_edges:
        if edge[2] != cs.RelationshipType.CALLS.value:
            continue
        caller = str(edge[1]).removeprefix(f"{PROJECT}.service.")
        out.setdefault(caller, {})[str(edge[4])] = str(
            store.props_for(edge).get(cs.KEY_RESOLUTION)
        )
    return out


def _find_calls(calls: dict[str, dict[str, str]], caller: str) -> dict[str, str]:
    return {
        qn: res
        for qn, res in calls.get(caller, {}).items()
        if qn in (REPO_FIND, CACHE_FIND)
    }


@pytest.mark.parametrize(
    "owner",
    [
        "ByAnnotation",
        "ByPipe",
        "ByInit",
        "ByUnion",
        "ByTypingOptional",
        "ByQuoted",
        "ByQuotedInit",
        "Holder",
        "SeqHolder",
    ],
)
def test_an_optional_attribute_binds_to_its_own_class(
    calls: dict[str, dict[str, str]], owner: str
) -> None:
    assert _find_calls(calls, f"{owner}.lookup") == {
        REPO_FIND: cs.EdgeResolution.EXACT.value
    }


@pytest.mark.parametrize(
    "function",
    ["opt", "typing_opt", "union", "union_none_first", "quoted", "quoted_opt"],
)
def test_an_optional_parameter_binds_to_its_own_class(
    calls: dict[str, dict[str, str]], function: str
) -> None:
    assert _find_calls(calls, function) == {REPO_FIND: cs.EdgeResolution.EXACT.value}


@pytest.mark.parametrize(
    ("annotation", "reduced"),
    [
        ("Optional[Repo]", "Repo"),
        ("typing.Optional[Repo]", "Repo"),
        ("Union[Repo, None]", "Repo"),
        ("Union[None, Repo]", "Repo"),
        ('"Optional[Repo]"', "Repo"),
        ('Optional["Repo"]', "Repo"),
        ("Optional[list[Repo]]", "list[Repo]"),
    ],
)
def test_an_optional_annotation_reduces_to_its_class(
    annotation: str, reduced: str
) -> None:
    from codebase_rag.parsers.py.utils import reduce_optional_annotation

    assert reduce_optional_annotation(annotation) == reduced


# Negative: what must not change.


@pytest.mark.parametrize(
    "annotation",
    [
        "Repo",
        "list[Repo]",
        "Union[Repo, Cache]",
        "Repo | Cache | None",
        "dict[str, int | None]",
        "Callable[[Repo], None]",
        "None",
    ],
)
def test_other_annotations_keep_their_text(annotation: str) -> None:
    from codebase_rag.parsers.py.utils import reduce_optional_annotation

    assert reduce_optional_annotation(annotation) == annotation


@pytest.mark.parametrize("caller", ["Bare.lookup", "plain", "pipe"])
def test_a_plain_or_pipe_annotation_still_binds_exactly(
    calls: dict[str, dict[str, str]], caller: str
) -> None:
    assert _find_calls(calls, caller) == {REPO_FIND: cs.EdgeResolution.EXACT.value}


@pytest.mark.parametrize("caller", ["Either.lookup", "either"])
def test_a_union_of_two_classes_is_not_reduced_to_either(
    calls: dict[str, dict[str, str]], caller: str
) -> None:
    exact = {
        qn
        for qn, res in _find_calls(calls, caller).items()
        if res == cs.EdgeResolution.EXACT.value
    }

    assert exact == set()
