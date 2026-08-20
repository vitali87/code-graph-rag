# Resolver consumption of the javac facts (PR2 of issue #1181). The heuristics
# match by name and ARITY; the language selects an overload by ARGUMENT TYPE, so
# a call to a same-arity overload is a coin flip without the compiler. These
# tests assert the CALLS edges, and each one asserts what the heuristics do on
# the same repo, so a passing result cannot come from the fallback.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.java_frontend import java_frontend_available
from codebase_rag.tests.conftest import get_relationships

# The argument is a call into the JDK, whose return type the tree-sitter side
# cannot read: the argument stays untyped, so it is a wildcard and the pick
# falls back to arity, taking the first same-arity declaration. javac knows
# `substring` returns String and selects the most specific applicable overload
# (CharSequence).
_WIDGET = (
    "package com.app;\n\n"
    "public class Widget {\n"
    '    public String handle(Object value) {\n        return "object";\n    }\n\n'
    "    public String handle(CharSequence value) {\n"
    '        return "chars";\n    }\n}\n'
)
_CALLER = (
    "package com.app;\n\n"
    "public class Caller {\n"
    "    public String run() {\n"
    "        Widget widget = new Widget();\n"
    '        String text = "x";\n'
    "        return widget.handle(text.substring(1));\n    }\n}\n"
)


def _write_repo(repo: Path) -> None:
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "Widget.java").write_text(_WIDGET, encoding="utf-8")
    (package / "Caller.java").write_text(_CALLER, encoding="utf-8")


def _call_targets(repo: Path) -> set[str]:
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=repo, parsers=parsers, queries=queries).run()
    return {c.args[2][2] for c in get_relationships(mock, "CALLS")}


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_the_frontend_binds_the_overload_the_compiler_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    targets = _call_targets(repo)
    assert any(t.endswith("Widget.handle(CharSequence)") for t in targets)
    assert not any(t.endswith("Widget.handle(Object)") for t in targets)


def test_the_heuristics_alone_pick_the_wrong_overload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The discriminator: with the frontend OFF the same repo binds to the FIRST
    # same-arity declaration. If this ever starts passing, the test above no
    # longer proves the frontend is doing the work.
    repo = tmp_path / "proj"
    _write_repo(repo)
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.HEURISTIC)
    targets = _call_targets(repo)
    assert any(t.endswith("Widget.handle(Object)") for t in targets)


# A static import makes the call UNQUALIFIED, so the heuristic's module-wide
# name scan finds the same-named local static; javac knows it resolved to the
# JDK.
_JDK_CALLER = (
    "package com.app;\n\n"
    "import static java.lang.String.valueOf;\n\n"
    "class Decoy {\n"
    '    public static String valueOf(int n) {\n        return "decoy";\n    }\n}\n\n'
    "public class JdkCaller {\n"
    "    public String run() {\n"
    "        return valueOf(42);\n    }\n}\n"
)


def _write_jdk_repo(repo: Path) -> None:
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "JdkCaller.java").write_text(_JDK_CALLER, encoding="utf-8")


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_a_proven_external_call_fabricates_no_first_party_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "proj"
    _write_jdk_repo(repo)
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    targets = _call_targets(repo)
    assert not any(t.endswith("Decoy.valueOf(int)") for t in targets)


def test_the_heuristics_alone_fabricate_the_jdk_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The discriminator for the external proof: `String.valueOf` leaves the
    # repo, but a same-named local static is indistinguishable to a name scan.
    repo = tmp_path / "proj"
    _write_jdk_repo(repo)
    monkeypatch.setattr(settings, "JAVA_FRONTEND", cs.JavaFrontend.HEURISTIC)
    targets = _call_targets(repo)
    assert any(t.endswith("Decoy.valueOf(int)") for t in targets)
