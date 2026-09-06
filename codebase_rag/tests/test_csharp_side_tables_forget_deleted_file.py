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
