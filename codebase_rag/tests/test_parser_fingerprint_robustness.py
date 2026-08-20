# Issue #1339: a broken or half-written dist-info anywhere in site-packages
# made importlib_metadata raise inside `dist.name`, and that propagated out of
# compute_parser_fingerprint, aborting the whole index run. An unreadable
# distribution must be skipped instead.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import parser_fingerprint as pf


class _UnreadableDistribution:
    """Mirrors importlib_metadata's failure mode: the property raises."""

    @property
    def name(self) -> str:
        raise TypeError("'NoneType' object is not subscriptable")

    @property
    def version(self) -> str:
        raise TypeError("'NoneType' object is not subscriptable")


class _UnreadableVersionDistribution:
    """A grammar distribution whose NAME reads fine but whose version does
    not: the guard must cover the second read too, not just the first."""

    @property
    def name(self) -> str:
        return f"{cs.GRAMMAR_DIST_PREFIX}rust"

    @property
    def version(self) -> str:
        raise TypeError("'NoneType' object is not subscriptable")


class _GrammarDistribution:
    def __init__(self, name: str, version: str) -> None:
        self.name = name
        self.version = version


def test_unreadable_distribution_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _GrammarDistribution(f"{cs.GRAMMAR_DIST_PREFIX}python", "0.23.0")
    monkeypatch.setattr(
        pf.metadata,
        "distributions",
        lambda: iter([_UnreadableDistribution(), good]),
    )
    versions = pf._grammar_versions()
    assert versions == [
        cs.GRAMMAR_VERSION_FMT.format(name=good.name.lower(), version=good.version)
    ]


def test_fingerprint_survives_an_unreadable_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pf.metadata, "distributions", lambda: iter([_UnreadableDistribution()])
    )
    fingerprint = pf.compute_parser_fingerprint(repo_path=tmp_path)
    assert fingerprint


def test_unreadable_version_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    good = _GrammarDistribution(f"{cs.GRAMMAR_DIST_PREFIX}python", "0.23.0")
    monkeypatch.setattr(
        pf.metadata,
        "distributions",
        lambda: iter([_UnreadableVersionDistribution(), good]),
    )
    assert pf._grammar_versions() == [
        cs.GRAMMAR_VERSION_FMT.format(name=good.name.lower(), version=good.version)
    ]
