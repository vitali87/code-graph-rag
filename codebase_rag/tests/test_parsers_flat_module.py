"""`emit_flat_module` and `flat_module_qn`: the Module node the ast-grep and
document tiers write, named flat from its path, and the Package, Folder or
Project that CONTAINS_MODULE ties it to."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.flat_module import emit_flat_module, flat_module_qn
from codebase_rag.tests.conftest import get_nodes, get_relationships
from codebase_rag.types_defs import PropertyDict, RelEndpointSpec
from evals.recorded_graph import RecordingIngestor


@pytest.mark.parametrize(
    ("relative", "distinguish", "expected"),
    [
        ("scripts/build.sh", False, "proj.scripts.build"),
        # No `__init__`/`mod` special case on this path.
        ("pkg/__init__.sh", False, "proj.pkg.__init__"),
        ("docs/guide.md", True, "proj.docs.guide_md"),
        ("README.md", True, "proj.README_md"),
        # Nothing to keep: no extension to fold into the name.
        ("Makefile", True, "proj.Makefile"),
    ],
)
def test_the_qn_is_the_path_with_the_suffix_dropped_or_folded(
    relative: str, distinguish: bool, expected: str
) -> None:
    assert flat_module_qn("proj", Path(relative), distinguish) == expected


def test_two_suffixes_of_one_stem_share_a_qn_unless_told_apart() -> None:
    sh, bash = Path("a/b.sh"), Path("a/b.bash")
    assert flat_module_qn("proj", sh) == flat_module_qn("proj", bash)
    assert flat_module_qn("proj", sh, True) != flat_module_qn("proj", bash, True)


def _emit(
    repo: Path,
    relative: str,
    structural_elements: dict[Path, str | None],
    distinguish_suffix: bool = False,
    extra_properties: PropertyDict | None = None,
) -> tuple[RecordingIngestor, str]:
    ingestor = RecordingIngestor()
    file_path = repo / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("echo hi\n", encoding="utf-8")
    qn = emit_flat_module(
        ingestor,
        repo,
        "proj",
        file_path,
        structural_elements,
        distinguish_suffix,
        extra_properties,
    )
    return ingestor, qn


def test_the_module_node_carries_its_identity_and_extra_properties(
    temp_repo: Path,
) -> None:
    ingestor, qn = _emit(
        temp_repo,
        "docs/guide.md",
        {},
        distinguish_suffix=True,
        extra_properties={"title": "Guide"},
    )
    assert qn == "proj.docs.guide_md"
    (module,) = get_nodes(ingestor, cs.NodeLabel.MODULE)
    assert module.properties == {
        cs.KEY_QUALIFIED_NAME: "proj.docs.guide_md",
        cs.KEY_NAME: "guide.md",
        cs.KEY_PATH: "docs/guide.md",
        cs.KEY_ABSOLUTE_PATH: (temp_repo / "docs/guide.md").resolve().as_posix(),
        "title": "Guide",
    }


def _container(ingestor: RecordingIngestor, qn: str) -> RelEndpointSpec:
    (rel,) = get_relationships(ingestor, cs.RelationshipType.CONTAINS_MODULE)
    assert rel.to_spec == (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, qn)
    return rel.from_spec


def test_a_module_in_a_package_is_contained_by_the_package(temp_repo: Path) -> None:
    ingestor, qn = _emit(temp_repo, "pkg/run.sh", {Path("pkg"): "proj.pkg"})
    assert _container(ingestor, qn) == (
        cs.NodeLabel.PACKAGE,
        cs.KEY_QUALIFIED_NAME,
        "proj.pkg",
    )


def test_a_module_in_a_plain_directory_is_contained_by_its_folder(
    temp_repo: Path,
) -> None:
    # A directory recorded without a package qn is a Folder, keyed by its
    # absolute path so same-layout projects in one graph stay apart.
    ingestor, qn = _emit(temp_repo, "tools/run.sh", {Path("tools"): None})
    assert _container(ingestor, qn) == (
        cs.NodeLabel.FOLDER,
        cs.KEY_ABSOLUTE_PATH,
        (temp_repo / "tools").resolve().as_posix(),
    )


def test_a_module_at_the_root_is_contained_by_the_project(temp_repo: Path) -> None:
    ingestor, qn = _emit(temp_repo, "run.sh", {})
    assert qn == "proj.run"
    assert _container(ingestor, qn) == (cs.NodeLabel.PROJECT, cs.KEY_NAME, "proj")
