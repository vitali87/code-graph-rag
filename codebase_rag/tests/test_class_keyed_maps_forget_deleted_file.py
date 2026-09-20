"""A deleted file's class-keyed maps must leave with it, in every language.

`remove_file_from_state` prunes the registry, the return-type maps and the
C# side tables, but never mentioned `class_field_types` or
`class_inheritance`: after `run()`, deleting a file and removing its state
left both holding the deleted file's entries on a reused updater (issue
#1772). Same defect class as #1668, #1738, #1753 and #1769, for two more
maps -- and unlike #1769's four, these two are written by every language's
class ingest, so the prune and these fixtures are language-agnostic.

A stale entry here is a wrong answer rather than a missing one:
`class_field_types` types a receiver reached through a field, and
`class_inheritance` is walked to reach base-class members and drives the
OVERRIDES arbitration.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

# Declared by `proj/Core.cs`. A C# class qn embeds the namespace, so
# `namespace Util` puts these classes at `proj.Core.Util.*` -- under the
# module qn of the SIBLING file below, not under this file's own `proj.Core`
# alone. Ownership must therefore come from the recorded declaring module;
# a prefix rule on the qn confuses the two files in both directions.
A_CS = """namespace Util
{
    public class Base
    {
        public int Shared;
    }
    public class K : Base
    {
        public string Field;
    }
}
"""

# Declared by `proj/Core/Util.cs`, whose MODULE qn is `proj.Core.Util` --
# the very prefix A_CS's class qns sit under. A prefix rule on the qn
# therefore attributes A_CS's classes to this file and vice versa, which is
# what makes the recorded owner load-bearing rather than decorative (#1769
# review).
B_CS = """namespace Deep
{
    public class SibBase
    {
        public int Held;
    }
    public class L : SibBase
    {
        public string Kept;
    }
}
"""

A_GO = """package main

type ABase struct {
\tShared int
}

type AThing struct {
\tField ABase
}
"""

B_GO = """package main

type BBase struct {
\tHeld int
}

type BThing struct {
\tKept BBase
}
"""

# JS prototype inheritance (`Child.prototype = Object.create(...)`) writes
# `class_inheritance` from js_ts/ingest.py directly, WITHOUT passing through
# class ingest -- so it needs its own owner record or the entry outlives the
# file. Measured: before that record existed, deleting a.js left
# `proj.a.AChild` in the map.
A_JS = """function AParent() {}
function AChild() {}
AChild.prototype = Object.create(AParent.prototype);
"""

B_JS = """function BParent() {}
function BChild() {}
BChild.prototype = Object.create(BParent.prototype);
"""

A_PY = """class ABase:
    pass


class AThing(ABase):
    pass
"""

B_PY = """class BBase:
    pass


class BThing(BBase):
    pass
"""


def _create_graph_updater(root: Path, language: str) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _only_qn(store: dict[str, object], needle: str) -> str | None:
    return next((qn for qn in store if needle in qn), None)


@pytest.mark.parametrize(
    ("language", "gone_name", "kept_name", "files", "checks_bases", "checks_fields"),
    [
        pytest.param(
            cs.SupportedLanguage.CSHARP,
            ".K",
            ".L",
            {"Core.cs": A_CS, "Core/Util.cs": B_CS},
            True,
            True,
            id="csharp",
        ),
        pytest.param(
            cs.SupportedLanguage.GO,
            ".AThing",
            ".BThing",
            {"a.go": A_GO, "b.go": B_GO},
            # Go records a class_inheritance key with an EMPTY base list:
            # struct embedding is not extracted into that map. Only the
            # field-type half carries a real value here.
            False,
            True,
            id="go",
        ),
        pytest.param(
            cs.SupportedLanguage.JS,
            ".AChild",
            ".BChild",
            {"a.js": A_JS, "b.js": B_JS},
            True,
            # JS records no class_field_types here; the prototype chain is
            # the inheritance half.
            False,
            id="javascript-prototype",
        ),
        pytest.param(
            cs.SupportedLanguage.PYTHON,
            ".AThing",
            ".BThing",
            {"a.py": A_PY, "b.py": B_PY},
            True,
            # Python records no field types; the inheritance half is the
            # one that carries a value.
            False,
            id="python",
        ),
    ],
)
def test_a_deleted_files_class_keyed_maps_are_forgotten(
    temp_repo: Path,
    language: str,
    gone_name: str,
    kept_name: str,
    files: dict[str, str],
    checks_bases: bool,
    checks_fields: bool,
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    updater = _create_graph_updater(root, language)
    updater.run()
    processor = updater.factory.definition_processor

    inheritance = processor.class_inheritance
    field_types = processor.class_field_types

    gone_inherit = kept_inherit = None
    if checks_bases:
        gone_inherit = _only_qn(inheritance, gone_name)
        kept_inherit = _only_qn(inheritance, kept_name)
        assert gone_inherit, (
            f"fixture guard: deleted file recorded no bases: {inheritance}"
        )
        assert kept_inherit, f"fixture guard: sibling recorded no bases: {inheritance}"
        # A recorded key with an EMPTY base list would satisfy the presence
        # check above while proving nothing about the map that steers
        # resolution.
        assert inheritance[gone_inherit], (
            f"fixture guard: deleted file's class has no bases: {inheritance}"
        )

    gone_fields = kept_fields = None
    if checks_fields:
        gone_fields = _only_qn(field_types, gone_name)
        kept_fields = _only_qn(field_types, kept_name)
        assert gone_fields, (
            f"fixture guard: deleted file recorded no field types: {field_types}"
        )
        assert kept_fields, (
            f"fixture guard: sibling recorded no field types: {field_types}"
        )
        assert field_types[gone_fields], (
            f"fixture guard: deleted file's class has no field types: {field_types}"
        )

    deleted = next(iter(files))
    (root / deleted).unlink()
    updater.remove_file_from_state(root / deleted)

    if checks_bases:
        assert gone_inherit not in inheritance, (
            f"the deleted file's class_inheritance entry survived: {inheritance}"
        )
        assert kept_inherit in inheritance, (
            f"a sibling's class_inheritance entry was swept along: {inheritance}"
        )
    if checks_fields:
        assert gone_fields not in field_types, (
            f"the deleted file's class_field_types entry survived: {field_types}"
        )
        assert kept_fields in field_types, (
            f"a sibling's class_field_types entry was swept along: {field_types}"
        )


def test_a_rehydrated_class_is_pruned_with_its_file(temp_repo: Path) -> None:
    """A class read back from the graph leaves with its file too.

    An incremental run re-parses only the changed files and REHYDRATES the
    rest from the graph, so on such a run most classes never pass through
    class ingest and get no `class_owner_module` row. Attributing only the
    parsed ones would leave every rehydrated class's bases behind -- the same
    defect #1772 closes, on the majority of an incremental run's map. The
    declaring file is known regardless: the rehydrate records it in
    `rehydrated_definition_paths`.
    """
    root = temp_repo / "proj"
    root.mkdir()
    (root / "a.py").write_text("class ABase:\n    pass\n", encoding="utf-8")
    (root / "b.py").write_text("class BBase:\n    pass\n", encoding="utf-8")

    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")

    ingestor = _MockIngestor()

    def rows(query: str, params: object = None) -> list[dict[str, object]]:
        if "INHERITS" in query:
            return [
                {
                    "child_qn": "proj.a.AThing",
                    "base_qn": "proj.a.ABase",
                    "base_index": 0,
                },
                {
                    "child_qn": "proj.b.BThing",
                    "base_qn": "proj.b.BBase",
                    "base_index": 0,
                },
            ]
        return [
            {"qualified_name": "proj.a.AThing", "label": "Class", "path": "a.py"},
            {"qualified_name": "proj.b.BThing", "label": "Class", "path": "b.py"},
        ]

    ingestor.fetch_all.side_effect = rows
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater._rehydrate_registry_from_graph()
    updater._rehydrate_class_inheritance_from_graph()

    processor = updater.factory.definition_processor
    inheritance = processor.class_inheritance
    # These classes came from the graph, not from a parse, so they carry no
    # ingest-time owner row -- which is exactly the condition under test.
    assert "proj.a.AThing" in inheritance, (
        f"fixture guard: not rehydrated: {inheritance}"
    )
    assert "proj.b.BThing" in inheritance, (
        f"fixture guard: not rehydrated: {inheritance}"
    )
    assert "proj.a.AThing" not in processor.class_owner_module, (
        "fixture guard: a rehydrated class must have no ingest owner row, "
        "or this test would pass through the parsed-class path instead"
    )

    (root / "a.py").unlink()
    updater.remove_file_from_state(root / "a.py")

    assert "proj.a.AThing" not in inheritance, (
        f"the deleted file's rehydrated class survived: {inheritance}"
    )
    assert "proj.b.BThing" in inheritance, (
        f"a sibling's rehydrated class was swept along: {inheritance}"
    )
