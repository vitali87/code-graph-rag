"""A deleted C# file's four remaining side tables must leave with its rows.

`remove_file_from_state` prunes the registry, the return-type maps and
`csharp_partial_groups`, but never reached `csharp_generic_methods`,
`csharp_class_generic_arity`, `csharp_local_functions` or
`csharp_extension_methods`: after `run()`, deleting `A.cs` and removing its
state left every one of them holding the deleted file's entries on a reused
updater (issue #1769). Same defect class as #1668, #1738 and #1753.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import _MockIngestor

A_CS = """namespace N
{
    public class K<T>
    {
        public U Gen<U>(U value) { return value; }
        public int Loc()
        {
            int inner(int x) { return x; }
            return inner(1);
        }
    }
    public static class Ext
    {
        public static int Twice(this int self) { return self * 2; }
    }
}
"""

B_CS = """namespace N
{
    public class L<T>
    {
        public U Sib<U>(U value) { return value; }
        public int Keep()
        {
            int held(int x) { return x; }
            return held(1);
        }
    }
    public static class SibExt
    {
        public static int Thrice(this int self) { return self * 3; }
    }
}
"""


def _create_graph_updater(root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CSHARP not in parsers:
        pytest.skip("csharp parser not available")
    return GraphUpdater(
        ingestor=_MockIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _ext_owners(store: dict[str, list[tuple[str, str, str, int]]]) -> set[str]:
    return {owner for entries in store.values() for owner, *_rest in entries}


def test_a_deleted_csharp_files_side_tables_are_forgotten(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    (root / "A.cs").write_text(A_CS, encoding="utf-8")
    (root / "B.cs").write_text(B_CS, encoding="utf-8")
    updater = _create_graph_updater(root)
    updater.run()
    engine = updater.factory.type_inference

    generic = engine.csharp_generic_methods
    arity = engine.csharp_class_generic_arity
    local = engine.csharp_local_functions
    extension = engine.csharp_extension_methods

    gone_generic = next((qn for qn in generic if ".K." in qn), None)
    kept_generic = next((qn for qn in generic if ".L." in qn), None)
    assert gone_generic, f"fixture guard: A.cs recorded no generic method: {generic}"
    assert kept_generic, f"fixture guard: B.cs recorded no generic method: {generic}"

    gone_arity = next((qn for qn in arity if qn.endswith(".K")), None)
    kept_arity = next((qn for qn in arity if qn.endswith(".L")), None)
    assert gone_arity, f"fixture guard: A.cs recorded no class arity: {arity}"
    assert kept_arity, f"fixture guard: B.cs recorded no class arity: {arity}"

    gone_local = next((qn for qn in local if ".K." in qn), None)
    kept_local = next((qn for qn in local if ".L." in qn), None)
    assert gone_local, f"fixture guard: A.cs recorded no local function: {local}"
    assert kept_local, f"fixture guard: B.cs recorded no local function: {local}"

    gone_ext = next((qn for qn in _ext_owners(extension) if ".Ext." in qn), None)
    kept_ext = next((qn for qn in _ext_owners(extension) if ".SibExt." in qn), None)
    assert gone_ext, f"fixture guard: A.cs recorded no extension method: {extension}"
    assert kept_ext, f"fixture guard: B.cs recorded no extension method: {extension}"

    (root / "A.cs").unlink()
    updater.remove_file_from_state(root / "A.cs")

    assert gone_generic not in generic, (
        f"the deleted file's generic method survived: {generic}"
    )
    assert gone_arity not in arity, (
        f"the deleted file's class generic arity survived: {arity}"
    )
    assert gone_local not in local, (
        f"the deleted file's local function survived: {local}"
    )
    assert gone_ext not in _ext_owners(extension), (
        f"the deleted file's extension method survived: {extension}"
    )

    assert kept_generic in generic, "a sibling's generic method was swept along"
    assert kept_arity in arity, "a sibling's class generic arity was swept along"
    assert kept_local in local, "a sibling's local function was swept along"
    assert kept_ext in _ext_owners(extension), (
        "a sibling's extension method was swept along"
    )


NESTED_DIR_CS = """namespace N
{
    public class Nested<T>
    {
        public U Deep<U>(U value) { return value; }
    }
}
"""


def test_a_sibling_directory_sharing_the_deleted_files_stem_is_kept(
    temp_repo: Path,
) -> None:
    """`proj/A.cs` beside `proj/A/B.cs`: deleting A.cs must not sweep B.cs.

    A directory whose name equals a deleted file's stem produces module qns
    that NEST (`proj.A` and `proj.A.B`), and a class registers no function
    span, so `foreign_qns` cannot protect `proj.A.B.N.Nested` the way it
    protects the method qns under it. A module-prefix-only sweep therefore
    took a live sibling's class arity with the deleted file's.
    """
    root = temp_repo / "proj"
    root.mkdir()
    (root / "A.cs").write_text(A_CS, encoding="utf-8")
    (root / "A").mkdir()
    (root / "A" / "B.cs").write_text(NESTED_DIR_CS, encoding="utf-8")
    updater = _create_graph_updater(root)
    updater.run()
    arity = updater.factory.type_inference.csharp_class_generic_arity

    gone = next((qn for qn in arity if qn.endswith(".K")), None)
    kept = next((qn for qn in arity if qn.endswith(".Nested")), None)
    assert gone, f"fixture guard: A.cs recorded no class arity: {arity}"
    assert kept, f"fixture guard: A/B.cs recorded no class arity: {arity}"
    # The shape the defect needs: the survivor's qn nests under the deleted
    # file's module qn, so a prefix-only filter matches it.
    assert kept.startswith("proj.A."), (
        f"fixture guard: the survivor does not nest under the deleted module: {kept}"
    )

    (root / "A.cs").unlink()
    updater.remove_file_from_state(root / "A.cs")

    assert gone not in arity, f"the deleted file's class arity survived: {arity}"
    assert kept in arity, (
        f"a surviving sibling's class arity was swept by the prefix filter: {arity}"
    )


def test_a_nested_file_under_a_surviving_siblings_module_is_still_pruned(
    temp_repo: Path,
) -> None:
    """The mirror of the test above: delete `proj/A/B.cs`, keep `proj/A.cs`.

    `proj.A.B.N.Nested` sits under BOTH `proj.A.B` (its own file, deleted)
    and `proj.A` (`A.cs`, surviving). Excluding everything under a surviving
    module to protect the previous case spares this class too, which
    reinstates #1769 for every file whose parent directory shares a
    sibling's stem. Ownership is by LONGEST matching module, so the deeper
    `proj.A.B` wins and the class goes.
    """
    root = temp_repo / "proj"
    root.mkdir()
    (root / "A.cs").write_text(A_CS, encoding="utf-8")
    (root / "A").mkdir()
    (root / "A" / "B.cs").write_text(NESTED_DIR_CS, encoding="utf-8")
    updater = _create_graph_updater(root)
    updater.run()
    arity = updater.factory.type_inference.csharp_class_generic_arity

    gone = next((qn for qn in arity if qn.endswith(".Nested")), None)
    kept = next((qn for qn in arity if qn.endswith(".K")), None)
    assert gone, f"fixture guard: A/B.cs recorded no class arity: {arity}"
    assert kept, f"fixture guard: A.cs recorded no class arity: {arity}"
    # The shape the defect needs: the DELETED file's qn nests under a
    # SURVIVING file's module qn, so a survivor-exclusion filter spares it.
    assert gone.startswith("proj.A."), (
        f"fixture guard: the deleted class does not nest under proj.A: {gone}"
    )

    (root / "A" / "B.cs").unlink()
    updater.remove_file_from_state(root / "A" / "B.cs")

    assert gone not in arity, (
        "a deleted file's class arity was spared because a surviving "
        f"sibling's module is also a prefix of it: {arity}"
    )
    assert kept in arity, f"the surviving file's class arity was swept: {arity}"


NAMESPACE_MATCHING_SIBLING_DIR_CS = """namespace Util
{
    public class Helper<T> { }
}
"""

OTHER_NAMESPACE_CS = """namespace N
{
    public class Other<T> { }
}
"""


@pytest.mark.parametrize(
    ("victim", "survivor_qn"),
    [
        ("Core/Util.cs", "proj.Core.Util.Helper"),
        ("Core.cs", "proj.Core.Util.N.Other"),
    ],
    ids=["delete-the-sibling", "delete-the-declarer"],
)
def test_a_namespace_matching_a_sibling_directory_owns_by_record_not_prefix(
    temp_repo: Path, victim: str, survivor_qn: str
) -> None:
    """A class qn embeds its NAMESPACE, so no prefix rule can own it (#1769).

    `proj/Core.cs` declaring `namespace Util` yields `proj.Core.Util.Helper`,
    which sits under the sibling module `proj.Core.Util` (`proj/Core/Util.cs`)
    as well as under its own `proj.Core`. Longest-prefix ownership therefore
    hands the class to the WRONG file and errs in both directions: deleting
    the sibling dropped a live class (the arity map went empty), and deleting
    the real declarer left the dead entry behind.

    Ownership is a record written at ingest, where the declaring module is
    actually known, rather than an inference from the qn's shape.

    Parametrised over both directions because a rule can be right on one and
    wrong on the other -- which is exactly what the two earlier attempts at
    this filter each did. Neither existing fixture in this file has a
    namespace segment that collides with a directory name, so the whole suite
    stayed green while this was broken: green meant untested, not working.
    """
    root = temp_repo / "proj"
    (root / "Core").mkdir(parents=True)
    (root / "Core.cs").write_text(NAMESPACE_MATCHING_SIBLING_DIR_CS, encoding="utf-8")
    (root / "Core" / "Util.cs").write_text(OTHER_NAMESPACE_CS, encoding="utf-8")
    updater = _create_graph_updater(root)
    updater.run()
    arity = updater.factory.type_inference.csharp_class_generic_arity

    assert set(arity) == {"proj.Core.Util.Helper", "proj.Core.Util.N.Other"}, (
        f"fixture guard: both generic classes must be recorded: {arity}"
    )
    # The shape the defect needs: the DECLARER's class qn nests under a
    # sibling module's qn, so a prefix rule attributes it to that sibling.
    owner = updater.factory.type_inference.csharp_class_owner_module
    assert owner["proj.Core.Util.Helper"] == "proj.Core", (
        "fixture guard: Core.cs must own Helper despite the qn sitting under "
        f"proj.Core.Util: {owner}"
    )

    (root / victim).unlink()
    updater.remove_file_from_state(root / victim)

    assert set(arity) == {survivor_qn}, (
        f"deleting {victim} left the wrong class arity behind; a prefix rule "
        f"attributes the namespace-shifted qn to the wrong file: {arity}"
    )


def test_dropping_the_last_owner_of_an_extension_name_removes_the_key(
    temp_repo: Path,
) -> None:
    """The `del` inside the sweep is why its iteration is over `list(...)`.

    `drop_csharp_side_tables` deletes an extension-method NAME once its last
    owner is gone. Deleting from a dict while iterating it directly raises
    `RuntimeError: dictionary changed size during iteration`, so the loop
    iterates a materialised list of the keys.

    SonarCloud flags that `list()` as an unnecessary call on an already
    iterable object (python:S6199). It is a false positive, and this test is
    the evidence: it drives the delete branch, so removing the `list()` turns
    it into a RuntimeError rather than a silent behaviour change.

    Two owners under one name, and only one removed, also pins the other half
    -- the key survives with its remaining owner rather than being dropped
    wholesale.
    """
    engine = _create_graph_updater(temp_repo).factory.type_inference
    engine.csharp_extension_methods = {
        "Twice": [("proj.A.Ext.Twice(int)", "int", "proj.A", 0)],
        "Thrice": [
            ("proj.A.Ext.Thrice(int)", "int", "proj.A", 0),
            ("proj.B.Ext.Thrice(int)", "int", "proj.B", 0),
        ],
    }

    engine.drop_csharp_side_tables(
        function_qns={"proj.A.Ext.Twice(int)", "proj.A.Ext.Thrice(int)"},
        class_qns=set(),
    )

    assert "Twice" not in engine.csharp_extension_methods, (
        "a name whose only owner was removed must be dropped entirely"
    )
    assert engine.csharp_extension_methods["Thrice"] == [
        ("proj.B.Ext.Thrice(int)", "int", "proj.B", 0)
    ], (
        "a name with a surviving owner must keep the key and lose only the "
        f"removed entry: {engine.csharp_extension_methods}"
    )
