"""Every C++ module-interface extension must also be a parsed C++ extension.

`CPP_MODULE_EXTENSIONS` says which suffixes mark a C++ module interface unit;
`CPP_EXTENSIONS` is what `language_spec.py` registers as C++ and therefore
decides which files are parsed at all. They were two separate literal lists,
and they disagreed: `.mxx` was a module extension parsed by nothing, so a
`.mxx` interface produced no `Module`, no `ModuleInterface`, and any
`module X;` implementation unit pointing at it had nothing to resolve against
(issue #1727).

Same shape as the JS/TS restatement in #1720: a language extension set spelled
out a second time drifts from the first without anything noticing. The fix
defines the module set FROM the parsed set's named constants, and this file
pins both halves: that the sets agree, and that every module extension really
is indexed end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import get_language_for_extension
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def test_every_module_extension_is_a_parsed_cpp_extension() -> None:
    missing = [ext for ext in cs.CPP_MODULE_EXTENSIONS if ext not in cs.CPP_EXTENSIONS]
    assert not missing, (
        f"{missing} are C++ module extensions that no language spec parses, so "
        "a module interface with that suffix is invisible to the graph"
    )
    # The control: the set is not empty and each member is a suffix, so an
    # empty tuple or a stray bare name could not pass the check above.
    assert len(cs.CPP_MODULE_EXTENSIONS) >= 4
    assert all(ext.startswith(".") for ext in cs.CPP_MODULE_EXTENSIONS)


@pytest.mark.parametrize("ext", cs.CPP_MODULE_EXTENSIONS)
def test_every_module_extension_resolves_to_cpp(ext: str) -> None:
    assert get_language_for_extension(ext) == cs.SupportedLanguage.CPP, (
        f"{ext} is a C++ module extension claimed by "
        f"{get_language_for_extension(ext)!r}, not C++"
    )


@pytest.mark.parametrize("ext", cs.CPP_MODULE_EXTENSIONS)
def test_a_module_interface_with_every_extension_is_indexed(
    tmp_path: Path, ext: str
) -> None:
    """End to end: the interface exists in the graph and its impl resolves to it.

    Asserting the `IMPLEMENTS` edge and not just the `ModuleInterface` node:
    the edge is what a `module M;` implementation unit needs, and it is the
    symptom the issue was found by. Parametrised over the whole set rather
    than `.mxx` alone, so the next extension added to one list and not the
    other fails here instead of in a user's graph.
    """
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    root = tmp_path / "proj"
    root.mkdir()
    (root / f"iface{ext}").write_text(
        "export module M;\nexport int f();\n", encoding="utf-8"
    )
    (root / "impl.cpp").write_text(
        "module M;\nint f() { return 1; }\n", encoding="utf-8"
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

    interfaces = [
        key for key in store.nodes if key[0] == cs.NodeLabel.MODULE_INTERFACE.value
    ]
    assert interfaces, f"no ModuleInterface node for iface{ext}: {sorted(store.nodes)}"
    implements = [
        edge for edge in store.edges if edge[2] == cs.RelationshipType.IMPLEMENTS.value
    ]
    assert implements, (
        f"impl.cpp's `module M;` resolved to nothing when the interface is "
        f"iface{ext}: {sorted(store.edges)}"
    )
