# Bundled javac fact provider, PR1 of issue #1181. Java's heuristics match by
# name and ARITY, so two same-arity overloads are indistinguishable to them;
# javac attributes each call to the declaration the language actually selects.
# This PR ships the provider only -- resolver consumption is the follow-up, so
# these tests assert the FACTS, not graph edges.
from __future__ import annotations

import subprocess
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java_frontend import frontend as java_frontend_module
from codebase_rag.parsers.java_frontend import (
    java_frontend_available,
    resolve_java_frontend,
    run_java_frontend,
)
from codebase_rag.parsers.java_frontend.frontend import _compile_tool, _parse_payload

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


def test_parse_payload_rejects_non_list_sections() -> None:
    for payload in ('{"calls": null}', '{"externals": 5}', '{"calls": {}}'):
        facts = _parse_payload(payload)
        assert facts.call_sites == {}
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


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_declaration_name_wins_over_a_same_named_annotation(tmp_path: Path) -> None:
    # A declaration annotation belongs to the method header and may carry
    # arguments, so an annotation type sharing the method's name looks exactly
    # like the name token being searched for.
    repo = tmp_path / "proj"
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "make.java").write_text(
        "package com.app;\n\npublic @interface make {\n"
        '    String value() default "";\n}\n',
        encoding="utf-8",
    )
    (package / "Annotated.java").write_text(
        "package com.app;\n\npublic class Annotated {\n"
        '    @make("x")\n'
        '    public String make() {\n        return "m";\n    }\n\n'
        "    public String pick() {\n        return make();\n    }\n}\n",
        encoding="utf-8",
    )
    facts = run_java_frontend(repo)
    site = facts.call_sites[("src/main/java/com/app/Annotated.java", 10, 15, "make")]
    assert (site.target_file, site.target_line) == (
        "src/main/java/com/app/Annotated.java",
        5,
    )


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_non_ascii_identifiers_survive_the_wire(tmp_path: Path) -> None:
    # The payload crosses a pipe, so both ends pin UTF-8: a platform-default
    # encoder would mangle these names and drop the facts on the floor.
    repo = tmp_path / "proj"
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "Grusse.java").write_text(
        "package com.app;\n\npublic class Grusse {\n"
        '    public String gr\u00fc\u00dfe() {\n        return "hi";\n    }\n\n'
        "    public String call() {\n        return gr\u00fc\u00dfe();\n    }\n}\n",
        encoding="utf-8",
    )
    facts = run_java_frontend(repo)
    names = {key[3] for key in facts.call_sites}
    assert "gr\u00fc\u00dfe" in names


def _seeded_cache(tmp_path: Path) -> tuple[Path, Path, Path]:
    cache = tmp_path / "java_javac"
    out = cache / "out"
    (out / "cgr").mkdir(parents=True)
    published = out / "cgr/Frontend.class"
    published.write_bytes(b"good")
    return cache, out, published


def _staging_writer(returncode: int):
    def fake_run(command, **kwargs):
        staging = Path(command[2])
        (staging / "cgr").mkdir(parents=True, exist_ok=True)
        (staging / "cgr/Frontend.class").write_bytes(b"partial")
        return subprocess.CompletedProcess(command, returncode, "", "boom")

    return fake_run


def test_a_failed_build_never_publishes_a_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A build that dies mid-write would otherwise leave a truncated class file
    # whose mtime looks fresh, and every later run would launch it and fail.
    cache, out, published = _seeded_cache(tmp_path)
    monkeypatch.setattr(
        java_frontend_module.subprocess, "run", _staging_writer(returncode=1)
    )
    assert _compile_tool("javac", cache, out) is False
    assert published.read_bytes() == b"good"
    assert not (cache / "staging").exists()


def test_a_timed_out_build_leaves_no_staging_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, out, published = _seeded_cache(tmp_path)

    def timeout_run(command, **kwargs):
        staging = Path(command[2])
        (staging / "cgr").mkdir(parents=True, exist_ok=True)
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(java_frontend_module.subprocess, "run", timeout_run)
    assert _compile_tool("javac", cache, out) is False
    assert published.read_bytes() == b"good"
    assert not (cache / "staging").exists()


def test_a_successful_build_replaces_the_published_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, out, published = _seeded_cache(tmp_path)
    monkeypatch.setattr(
        java_frontend_module.subprocess, "run", _staging_writer(returncode=0)
    )
    assert _compile_tool("javac", cache, out) is True
    assert published.read_bytes() == b"partial"
    assert not (cache / "staging").exists()


@pytest.mark.skipif(
    not java_frontend_available(), reason="javac frontend needs a working JDK"
)
def test_a_comment_inside_the_annotation_does_not_hide_it(tmp_path: Path) -> None:
    # Java treats a comment as whitespace, so it may sit between the '@' and
    # the annotation name. Annotation extents come from the AST for exactly
    # this reason: no lexical scan has to know that.
    repo = tmp_path / "proj"
    package = repo / "src/main/java/com/app"
    package.mkdir(parents=True)
    (package / "make.java").write_text(
        "package com.app;\n\npublic @interface make {\n"
        '    String value() default "";\n}\n',
        encoding="utf-8",
    )
    (package / "Commented.java").write_text(
        "package com.app;\n\npublic class Commented {\n"
        '    @/* here */make("x")\n'
        '    public String make() {\n        return "m";\n    }\n\n'
        "    public String pick() {\n        return make();\n    }\n}\n",
        encoding="utf-8",
    )
    facts = run_java_frontend(repo)
    site = facts.call_sites[("src/main/java/com/app/Commented.java", 10, 15, "make")]
    assert site.target_line == 5


def test_the_tool_source_ships_in_the_wheel() -> None:
    # The provider builds from a bundled source file, so an installed wheel
    # that omits it would report the frontend available and then fail to
    # build it on every run.
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["codebase_rag"]
    source = java_frontend_module._TOOL_SRC / java_frontend_module._TOOL_SOURCE
    relative = source.relative_to(root / "codebase_rag").as_posix()
    assert any(fnmatch(relative, pattern) for pattern in patterns)
