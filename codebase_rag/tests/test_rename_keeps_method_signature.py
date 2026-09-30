"""Issue #2658: a Java or C# method rename passes its own contract.

Java and C# method qualified names end in their parameter types
(`...Greeter.greet(String)`). The rename contract built the expected new name
by replacing everything after the last dot, so it expected `...Greeter.welcome`
while the graph held `...Greeter.welcome(String)`. Every such rename failed its
postcondition and was undone, although the rewrite itself was right.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import QueryFn, rename, renamed_qualified_name
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "sigproj"

JAVA = {
    "src/main/java/com/acme/Greeter.java": (
        "package com.acme;\n"
        "\n"
        "public class Greeter {\n"
        "    public String greet(String name) {\n"
        '        return "hi " + name;\n'
        "    }\n"
        "\n"
        "    public String twice(String name) {\n"
        "        return greet(name) + greet(name);\n"
        "    }\n"
        "}\n"
    ),
}

CSHARP = {
    "Greeter.cs": (
        "namespace Acme\n"
        "{\n"
        "    public class Greeter\n"
        "    {\n"
        "        public string Greet(string name)\n"
        "        {\n"
        '            return "hi " + name;\n'
        "        }\n"
        "\n"
        "        public string Twice(string name)\n"
        "        {\n"
        "            return Greet(name) + Greet(name);\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
}

PYTHON = {
    "pkg/__init__.py": "",
    "pkg/greeter.py": (
        "class Greeter:\n"
        "    def greet(self, name):\n"
        '        return "hi " + name\n'
        "\n"
        "\n"
        "def twice(name):\n"
        "    g = Greeter()\n"
        "    return g.greet(name) + g.greet(name)\n"
    ),
}


def _indexed(
    root: Path, files: dict[str, str]
) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _method_qns(store: _StatefulIngestor) -> set[str]:
    return {str(qn) for label, qn in store.nodes if label == cs.NodeLabel.METHOD.value}


def _query(store: _StatefulIngestor) -> QueryFn:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return fetch_all


@pytest.mark.parametrize(
    ("files", "old_qn", "new_name", "new_qn", "path", "renamed_line"),
    [
        pytest.param(
            JAVA,
            f"{PROJECT}.src.main.java.com.acme.Greeter.Greeter.greet(String)",
            "welcome",
            f"{PROJECT}.src.main.java.com.acme.Greeter.Greeter.welcome(String)",
            "src/main/java/com/acme/Greeter.java",
            "return welcome(name) + welcome(name);",
            id="java",
        ),
        pytest.param(
            CSHARP,
            f"{PROJECT}.Greeter.Acme.Greeter.Greet(string)",
            "Welcome",
            f"{PROJECT}.Greeter.Acme.Greeter.Welcome(string)",
            "Greeter.cs",
            "return Welcome(name) + Welcome(name);",
            id="csharp",
        ),
    ],
)
def test_a_method_with_a_signature_is_renamed_and_kept(
    tmp_path: Path,
    files: dict[str, str],
    old_qn: str,
    new_name: str,
    new_qn: str,
    path: str,
    renamed_line: str,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, files)
    assert old_qn in _method_qns(store)

    report = rename(
        root, _query(store), PROJECT, old_qn, new_name, reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok
    assert renamed_line in (root / path).read_text(encoding="utf-8")
    assert new_qn in _method_qns(store)
    assert old_qn not in _method_qns(store)


@pytest.mark.parametrize(
    ("member", "new_name", "expected"),
    [
        pytest.param(
            "p.Greeter.greet(String)", "welcome", "p.Greeter.welcome(String)", id="sig"
        ),
        pytest.param(
            "p.Store.put(Map.Entry)",
            "store",
            "p.Store.store(Map.Entry)",
            id="dotted-param-type",
        ),
        pytest.param(
            "p.Box.set(java.util.List<a.B>, int)",
            "assign",
            "p.Box.assign(java.util.List<a.B>, int)",
            id="qualified-generic-params",
        ),
    ],
)
def test_the_expected_name_keeps_the_signature(
    member: str, new_name: str, expected: str
) -> None:
    assert renamed_qualified_name(member, new_name) == expected


# Negative: names without a signature are renamed as before.


@pytest.mark.parametrize(
    ("member", "new_name", "expected"),
    [
        ("p.pkg.util.helper", "assist", "p.pkg.util.assist"),
        ("p.pkg.greeter.Greeter.greet", "welcome", "p.pkg.greeter.Greeter.welcome"),
        ("p.Greeter", "Hello", "p.Hello"),
    ],
)
def test_a_name_without_a_signature_is_renamed_as_before(
    member: str, new_name: str, expected: str
) -> None:
    assert renamed_qualified_name(member, new_name) == expected


def test_a_python_method_rename_still_passes_its_contract(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, PYTHON)

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.pkg.greeter.Greeter.greet",
        "welcome",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok
    assert f"{PROJECT}.pkg.greeter.Greeter.welcome" in _method_qns(store)
