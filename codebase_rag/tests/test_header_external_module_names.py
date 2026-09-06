"""A header's ExternalModule is named by the header, not by its extension.

`_ensure_external_module_node` derived the node's `name` by splitting the
qualified name on its last dot. For a system header that qualified name is
`std.stdio.h`, so the last segment is the EXTENSION and every `.h` include
minted an ExternalModule called `h` (issue #1758). A slashed include kept its
raw slash in the qualified name too (`std.sys/types.h`), which no dotted
lookup can address and which does not segment like any other module path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SRC = """#include <stdio.h>
#include <sys/types.h>
#include <vector>
int main(void) { return 0; }
"""


def _external_modules(root: Path) -> dict[str, str]:
    """{qualified name: name} for every ExternalModule the run emits."""
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    (root / "m.c").write_text(SRC, encoding="utf-8")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run()
    store.flush_all()
    label = str(cs.NodeLabel.EXTERNAL_MODULE)
    return {
        str(props[cs.KEY_QUALIFIED_NAME]): str(props[cs.KEY_NAME])
        for (node_label, _uid), props in store.nodes.items()
        if node_label == label and cs.KEY_QUALIFIED_NAME in props
    }


def test_a_header_external_module_is_named_by_the_header(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    external = _external_modules(root)

    assert external, "fixture guard: the run emitted no ExternalModule nodes at all"
    # The control: a non-header system include was already named correctly,
    # so a fix that renamed everything would show up here.
    assert external.get("std.vector") == "vector", (
        f"a bare system include's name changed: {external}"
    )

    assert external.get("std.stdio.h") == "stdio", (
        f"a .h include is still named by its extension: {external}"
    )


def test_a_slashed_system_include_is_segmented_like_a_module_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    external = _external_modules(root)

    assert external, "fixture guard: the run emitted no ExternalModule nodes at all"
    assert not any(cs.SEPARATOR_SLASH in qn for qn in external), (
        f"a slash survived into an ExternalModule qualified name: {external}"
    )
    assert external.get("std.sys.types.h") == "types", (
        f"a slashed header is not segmented and named by its stem: {external}"
    )


COLLIDING = "#include <std>\n#include <std.h>\nint main(void) { return 0; }\n"


def _import_targets(root: Path, source: str) -> set[str]:
    """Every IMPORTS target the run emits for `m.c`."""
    parsers, queries = load_parsers()
    for language in (cs.SupportedLanguage.C, cs.SupportedLanguage.CPP):
        if language not in parsers:
            pytest.skip(f"{language} parser not available")
    (root / "m.c").write_text(source, encoding="utf-8")
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


def test_two_headers_binding_one_local_name_each_keep_their_edge(
    tmp_path: Path,
) -> None:
    """`<std>` and `<std.h>` both bind the local name `std`.

    `import_mapping` holds one binding per local name and the IMPORTS edges
    are derived from it, so the second include overwrote the first and the
    file ended up importing only one of the two headers it includes
    (issue #1758). The binding can still only name one of them; the edge for
    the displaced header is kept beside it.
    """
    root = tmp_path / "proj"
    root.mkdir()
    targets = _import_targets(root, COLLIDING)

    assert targets, "fixture guard: the run emitted no IMPORTS edges at all"
    assert targets == {"std", "std.h"}, (
        f"a header lost its IMPORTS edge to another binding the same name: {targets}"
    )


def test_the_header_rule_does_not_rename_other_languages_externals() -> None:
    """The `.h` rule must not reach a package whose last segment is `h`.

    `_external_module_name` serves EVERY language's external imports, not
    just C/C++ includes, and `h` is a real Python package (the HTTP/2
    library). An unconditional "last segment is an extension" rule renamed
    `mypkg.h` to `mypkg` for languages that have no headers at all, so the
    rule is confined to the `std.` prefix `_cpp_include_full_name` applies.
    """
    from codebase_rag.parsers.import_processor import _external_module_name

    # The header shapes the rule exists for.
    assert _external_module_name("std.stdio.h") == "stdio"
    assert _external_module_name("std.sys.types.h") == "types"
    assert _external_module_name("std.a.b.c.hpp") == "c"
    # A non-header external keeps its last segment, header-shaped or not.
    assert _external_module_name("std.vector") == "vector"
    assert _external_module_name("os.path") == "path"
    assert _external_module_name("mypkg.h") == "h", (
        "the header rule renamed a package whose last segment is literally h"
    )
    assert _external_module_name("a.b.h") == "h", (
        "the header rule reached a qn that carries no std. include prefix"
    )
