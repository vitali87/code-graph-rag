# Bundled javac fact provider, PR1 of issue #1181. Java's heuristics match by
# name and ARITY, so two same-arity overloads are indistinguishable to them;
# javac attributes each call to the declaration the language actually selects.
# This PR ships the provider only -- resolver consumption is the follow-up, so
# these tests assert the FACTS, not graph edges.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java_frontend import (
    java_frontend_available,
    resolve_java_frontend,
    run_java_frontend,
)
from codebase_rag.parsers.java_frontend.frontend import _parse_payload

_WIDGET = (
    "package com.app;\n\n"
    "public class Widget {\n"
    "    public String handle(String text) {\n        return text;\n    }\n\n"
    "    public String handle(int count) {\n"
    "        return String.valueOf(count);\n    }\n}\n"
)
_CALLER = (
    "package com.app;\n\n"
    "import java.util.ArrayList;\nimport java.util.List;\n\n"
    "public class Caller {\n"
    "    public String run() {\n"
    "        Widget widget = new Widget();\n"
    "        List<String> items = new ArrayList<>();\n"
    "        items.add(widget.handle(42));\n"
    '        return widget.handle("text");\n    }\n}\n'
)


# Byte-identical geometry in two files: the same callee name at the same line
# and column, each binding inside its own file.
_TWIN = (
    "package com.app;\n\n"
    "public class {name} {{\n"
    "    public String pick() {{\n        return make();\n    }}\n\n"
    '    public String make() {{\n        return "x";\n    }}\n}}\n'
)


def _write_repo(repo: Path) -> None:
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "Widget.java").write_text(_WIDGET, encoding="utf-8")
    (package / "Caller.java").write_text(_CALLER, encoding="utf-8")


def test_parse_payload_reads_both_sections() -> None:
    facts = _parse_payload(
        '{"calls": [{"file": "A.java", "line": 5, "col": 8, "name": "handle",'
        ' "tfile": "B.java", "tline": 3, "tcol": 4}],'
        ' "externals": [{"file": "A.java", "line": 9, "col": 2, "name": "add"}]}'
    )
    assert facts.call_sites[("A.java", 5, 8, "handle")].target_line == 3
    assert facts.external_sites == {("A.java", 9, 2, "add")}


def test_parse_payload_degrades_on_bad_output() -> None:
    for payload in ("", "not json", "[1, 2]", "{}"):
        facts = _parse_payload(payload)
        assert facts.call_sites == {}
        assert facts.external_sites == set()


def test_parse_payload_drops_malformed_rows() -> None:
    facts = _parse_payload(
        '{"calls": [{"file": "A.java", "line": "x", "col": 8, "name": "h",'
        ' "tfile": "B.java", "tline": 3, "tcol": 4},'
        ' {"file": "A.java", "line": 5, "col": 8, "name": "h",'
        ' "tfile": "B.java", "tline": 3, "tcol": 4}],'
        ' "externals": [{"file": "A.java", "name": "broken"}]}'
    )
    assert list(facts.call_sites) == [("A.java", 5, 8, "h")]
    assert facts.external_sites == set()


def test_heuristic_is_the_default_resolution() -> None:
    assert resolve_java_frontend() == cs.JavaFrontend.HEURISTIC


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_same_arity_overloads_bind_by_argument_type(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    facts = run_java_frontend(repo)
    caller = "src/main/java/com/app/Caller.java"
    widget = "src/main/java/com/app/Widget.java"
    targets = {
        key[1]: (site.target_file, site.target_line)
        for key, site in facts.call_sites.items()
        if key[0] == caller and key[3] == "handle"
    }
    # Same name, same arity: only attribution can tell these apart.
    assert targets[10] == (widget, 8)
    assert targets[11] == (widget, 4)


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_jdk_calls_become_external_proofs(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    _write_repo(repo)
    facts = run_java_frontend(repo)
    external_names = {key[3] for key in facts.external_sites}
    assert {"add", "valueOf"} <= external_names
    # A proven-external site must never also carry a first-party target.
    assert not {key for key in facts.call_sites if key in facts.external_sites}


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_same_position_in_two_files_keeps_both_sites(tmp_path: Path) -> None:
    # The dedup key spans the whole repo, so it must carry the file: two
    # files laid out alike put an identical call at an identical position.
    repo = tmp_path / "proj"
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    for name in ("Alpha", "Beta"):
        (package / f"{name}.java").write_text(_TWIN.format(name=name), encoding="utf-8")
    facts = run_java_frontend(repo)
    bound = {
        key[0]: site.target_file
        for key, site in facts.call_sites.items()
        if key[3] == "make"
    }
    assert bound == {
        "src/main/java/com/app/Alpha.java": "src/main/java/com/app/Alpha.java",
        "src/main/java/com/app/Beta.java": "src/main/java/com/app/Beta.java",
    }
