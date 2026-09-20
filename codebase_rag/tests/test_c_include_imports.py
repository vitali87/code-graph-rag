"""A `.c` file's `#include` must emit the same IMPORTS edge a `.h` file's does.

`ImportProcessor.parse_imports` dispatches on language, and the arm that
parses `preproc_include` nodes was keyed on C++ alone. Headers are registered
as C++ (`.h` is in `CPP_EXTENSIONS`), so `bar.h` including `foo.h` produced
`(Module bar) -[IMPORTS]-> (Module foo)`, while `bar.c` with the identical
directive reached no arm and emitted nothing (issue #1654). A C translation
unit's includes were therefore invisible to every consumer of IMPORTS edges:
dependents queries, importer lists, the incremental re-parse set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

HEADER = "#ifndef FOO_H\n#define FOO_H\nint foo(void);\n#endif\n"


def _imports(root: Path, includer: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    (root / "foo.h").write_text(HEADER, encoding="utf-8")
    (root / includer).write_text('#include "foo.h"\n', encoding="utf-8")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return {
        (str(src), str(dst))
        for (_sl, src, rel, _tl, dst) in store.edges
        if rel == cs.RelationshipType.IMPORTS.value
    }


def test_a_c_files_include_emits_the_same_imports_edge_as_a_headers(
    tmp_path: Path,
) -> None:
    c_root = tmp_path / "c"
    h_root = tmp_path / "h"
    c_root.mkdir()
    h_root.mkdir()

    from_c = _imports(c_root, "bar.c")
    from_h = _imports(h_root, "bar.h")

    expected = {("proj.bar", "proj.foo")}
    assert from_h == expected, f"fixture guard: the .h includer lost its edge: {from_h}"
    assert from_c == expected, (
        f"a .c file's #include emitted no IMPORTS edge while a .h file's did: {from_c}"
    )
    assert from_c == from_h


def _system_import_targets(root: Path, includer: str) -> set[str]:
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    (root / includer).write_text("#include <Foo>\nint x;\n", encoding="utf-8")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    return {
        str(dst)
        for (_sl, _src, rel, _tl, dst) in store.edges
        if rel == cs.RelationshipType.IMPORTS.value
    }


def test_a_c_files_system_include_names_the_same_external_module_as_cpps(
    tmp_path: Path,
) -> None:
    # One dispatch further down the path this fix opens to C: the stdlib
    # extractor also keyed its C++ arm on CPP alone, so a C entry fell to the
    # generic extractor, which strips a capitalised last segment as an
    # "entity". `<Foo>` is the extension-less shape where the two diverge; the
    # common `<stdio.h>` forms happen to agree (found in local review).
    c_root = tmp_path / "c"
    cpp_root = tmp_path / "cpp"
    c_root.mkdir()
    cpp_root.mkdir()
    from_c = _system_import_targets(c_root, "a.c")
    from_cpp = _system_import_targets(cpp_root, "a.cpp")
    assert from_cpp, "fixture guard: the .cpp includer emitted no IMPORTS edge"
    assert from_c == from_cpp, (
        f"a .c system include named {from_c} where the same include from .cpp "
        f"named {from_cpp}"
    )


@pytest.mark.parametrize("includer", ["a.c", "a.cpp"])
def test_every_system_header_lands_under_the_std_prefix(
    tmp_path: Path, includer: str
) -> None:
    # `startswith("std")` was a substring match: `<stdio.h>` and its family
    # read as already prefixed and named ExternalModule `stdio.h`, while
    # `<signal.h>` and `<vector>` in the same file named `std.signal.h` and
    # `std.vector` (issue #1744). Every system header takes the one prefix.
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    root = tmp_path / "proj"
    root.mkdir()
    (root / includer).write_text(
        "#include <stdio.h>\n#include <stdlib.h>\n#include <signal.h>\n"
        "#include <vector>\nint x;\n",
        encoding="utf-8",
    )
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    targets = {
        str(dst)
        for (_sl, _src, rel, _tl, dst) in store.edges
        if rel == cs.RelationshipType.IMPORTS.value
    }
    assert targets == {"std.stdio.h", "std.stdlib.h", "std.signal.h", "std.vector"}, (
        targets
    )
