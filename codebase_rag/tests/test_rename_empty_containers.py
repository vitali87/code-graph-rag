from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.editing.rename import rename
from codebase_rag.editing.transaction import EditTransaction, StagedFile, load_history
from codebase_rag.structural_delta import Definition, Snapshot, _symbols
from codebase_rag.tests.test_edit_contract import PROJECT, _real_project
from codebase_rag.types_defs import PropertyDict, ReingestReport, ResultRow


@pytest.mark.parametrize(
    ("body", "descendants"),
    [
        ("    class Inner:\n        pass\n", ["Inner"]),
        (
            "    def run(self):\n        return 1\n\n    class Inner:\n        pass\n",
            ["Inner"],
        ),
        (
            "    class Inner:\n        class Leaf:\n            pass\n",
            ["Inner", "Inner.Leaf"],
        ),
        (
            "    class One:\n        pass\n\n    class Two:\n        pass\n",
            ["One", "Two"],
        ),
    ],
    ids=["empty-outer", "outer-with-method", "deeply-nested", "siblings"],
)
def test_rename_carries_empty_nested_containers(
    temp_repo: Path, body: str, descendants: list[str]
) -> None:
    root = temp_repo / PROJECT
    source = "class Helper:\n" + body + "\nclass HelperX:\n    pass\n"
    store, updater = _real_project(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": source,
            "pkg/app.py": "from pkg.util import Helper\n\ndef run():\n    return Helper()\n",
        },
    )
    old = f"{PROJECT}.pkg.util.Helper"
    new = f"{PROJECT}.pkg.util.Assist"

    report = rename(
        root, store.fetch_all, PROJECT, old, "Assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.verdict is not None
    assert report.verdict.ok, report.verdict.failures
    assert report.verdict.delta is not None
    symbols = report.verdict.delta["symbols"]
    assert symbols["added"] == []
    assert symbols["removed"] == []
    renamed = {(pair["old"], pair["new"]) for pair in symbols["renamed"]}
    for suffix in ["", *(f".{child}" for child in descendants)]:
        assert (old + suffix, new + suffix) in renamed
        assert (cs.NodeLabel.CLASS.value, new + suffix) in store.nodes
        assert (cs.NodeLabel.CLASS.value, old + suffix) not in store.nodes
    assert report.hierarchy == (old,)
    assert (root / "pkg/util.py").read_text() == source.replace(
        "class Helper:", "class Assist:", 1
    )
    assert "return Assist()" in (root / "pkg/app.py").read_text()
    assert len(load_history(root)) == 1
    assert (cs.NodeLabel.CLASS.value, old + "X") in store.nodes


@pytest.mark.parametrize(
    ("before_name", "after_name", "old_suffix", "new_suffix"),
    [
        ("Unrelated", "Replacement", "Unrelated", "Replacement"),
        ("HelperX", "AssistX", "HelperX", "AssistX"),
        ("Inner", "Other", "Helper.Inner", "Assist.Other"),
    ],
    ids=["unrelated", "name-prefix-collision", "child-name-changed"],
)
def test_rename_rejects_an_undeclared_empty_container_replacement(
    temp_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    before_name: str,
    after_name: str,
    old_suffix: str,
    new_suffix: str,
) -> None:
    root = temp_repo / PROJECT
    source = (
        "class Helper:\n    class Inner:\n        pass\n\n"
        "class HelperX:\n    pass\n\nclass Unrelated:\n    pass\n"
    )
    store, updater = _real_project(root, {"pkg/__init__.py": "", "pkg/util.py": source})
    original_stage = EditTransaction.stage

    def stage_with_collateral(
        transaction: EditTransaction,
        rel_path: str | Path,
        content: str | bytes | None,
    ) -> StagedFile:
        if content is not None and str(rel_path) == "pkg/util.py":
            raw = content.encode() if isinstance(content, str) else content
            content = raw.replace(
                f"class {before_name}:".encode(), f"class {after_name}:".encode()
            )
        return original_stage(transaction, rel_path, content)

    monkeypatch.setattr(EditTransaction, "stage", stage_with_collateral)
    report = rename(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Helper",
        "Assist",
        reingest=updater.reingest,
    )

    assert report.verdict is not None
    assert not report.verdict.ok
    assert report.verdict.delta is not None
    symbols = report.verdict.delta["symbols"]
    removed = f"{PROJECT}.pkg.util.{old_suffix}"
    added = f"{PROJECT}.pkg.util.{new_suffix}"
    assert removed in symbols["removed"]
    assert added in symbols["added"]
    assert (removed, added) not in {
        (pair["old"], pair["new"]) for pair in symbols["renamed"]
    }
    assert not report.applied
    assert (root / "pkg/util.py").read_text() == source
    assert load_history(root) == []
    assert (cs.NodeLabel.CLASS.value, removed) in store.nodes
    assert (cs.NodeLabel.CLASS.value, added) not in store.nodes


@pytest.mark.parametrize(
    ("pairs", "target_path", "target_label"),
    [
        ([("old", "new"), ("other", "new")], "a.py", cs.NodeLabel.CLASS),
        ([("old", "new"), ("old", "second")], "a.py", cs.NodeLabel.CLASS),
        ([("old", "new")], "elsewhere.py", cs.NodeLabel.CLASS),
        ([("old", "new")], "a.py", cs.NodeLabel.INTERFACE),
    ],
    ids=["shared-target", "multiple-targets", "wrong-path", "wrong-label"],
)
def test_empty_container_declarations_must_be_unique_and_match_metadata(
    pairs: list[tuple[str, str]], target_path: str, target_label: str
) -> None:
    definition = Definition(
        label=cs.NodeLabel.CLASS,
        qualified_name="old",
        name="old",
        path="a.py",
        start_line=1,
        end_line=2,
        positional_params=None,
        fingerprint="",
        fingerprint_nodes=0,
        branches=frozenset(),
    )
    before = Snapshot(
        paths=frozenset({"a.py"}),
        definitions={
            "old": definition,
            "other": definition._replace(qualified_name="other", name="other"),
        },
        callees={},
        sites=(),
        imports={},
        module_paths={},
    )
    after = before._replace(
        definitions={
            "new": definition._replace(
                qualified_name="new",
                name="new",
                path=target_path,
                label=target_label,
            ),
            "second": definition._replace(qualified_name="second", name="second"),
        }
    )

    symbols = _symbols(before, after, frozenset(pairs))

    assert symbols["renamed"] == []
    assert symbols["removed"] == ["old", "other"]
    assert symbols["added"] == ["new", "second"]


def test_declaration_read_failure_reports_the_committed_rename(
    temp_repo: Path,
) -> None:
    root = temp_repo / PROJECT
    source = "class Helper:\n    class Inner:\n        pass\n"
    store, updater = _real_project(root, {"pkg/util.py": source})
    reingested: list[list[str]] = []

    def fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if query == cq.CYPHER_DELTA_DEFINITIONS:
            raise ConnectionError("declaration lookup unavailable")
        return store.fetch_all(query, params)

    def reingest(paths: list[str]) -> ReingestReport:
        reingested.append(paths)
        return updater.reingest(paths)

    report = rename(
        root,
        fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.util.Helper",
        "Assist",
        reingest=reingest,
    )

    assert report.applied
    assert report.verdict is None
    assert "declaration lookup unavailable" in report.message
    assert (root / "pkg/util.py").read_text() == source.replace("Helper", "Assist")
    assert len(load_history(root)) == 1
    assert reingested == []
