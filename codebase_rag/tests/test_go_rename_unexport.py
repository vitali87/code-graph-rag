"""A Go rename that unexports a name another package uses is refused.

In Go an identifier's case is its visibility: `Slug` is exported, `slug` is
package-private. `cgr rename strs.Slug slug` rewrote the call in package
`main` to `strs.slug(...)`, which can never compile (`undefined: strs.slug`),
and reported `applied`, `verdict.ok` (issue #2813).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, RenameReport, rename
from codebase_rag.tests.conftest import _MockIngestor
from codebase_rag.tests.test_rename_op import _index, _write

_GO_MOD = "module example.com/goren\n\ngo 1.21\n"
_STRS = "package strs\n\nfunc Slug(s string) string { return s }\n"
_MAIN = (
    "package main\n\n"
    'import (\n\t"fmt"\n\n\t"example.com/goren/strs"\n)\n\n'
    'func main() { fmt.Println(strs.Slug("Hello World")) }\n'
)


def _repo(root: Path, files: dict[str, str]) -> Path:
    _write(root, "go.mod", _GO_MOD)
    for rel, text in files.items():
        _write(root, rel, text)
    return root


def _rename(root: Path, qn_tail: str, new_name: str) -> RenameReport:
    graph = _index(root, _MockIngestor())
    return rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.{qn_tail}",
        new_name,
        allow_heuristic=True,
    )


@pytest.mark.parametrize(
    "files",
    [
        {"strs/strs.go": _STRS, "cmd/main.go": _MAIN},
        # An external test package in the definition's own directory is a
        # different package too: it sees only exported names.
        {
            "strs/strs.go": _STRS,
            "strs/strs_test.go": (
                "package strs_test\n\n"
                'import (\n\t"testing"\n\n\t"example.com/goren/strs"\n)\n\n'
                'func TestSlug(t *testing.T) { _ = strs.Slug("a") }\n'
            ),
        },
    ],
    ids=["other_directory", "external_test_package"],
)
def test_unexporting_a_name_used_by_another_package_is_refused(
    temp_repo: Path, files: dict[str, str]
) -> None:
    root = _repo(temp_repo, files)
    before = {rel: (root / rel).read_text() for rel in files}

    with pytest.raises(RenameRefused) as refused:
        _rename(root, "strs.strs.Slug", "slug")

    assert "slug" in str(refused.value)
    assert all(s.path != "strs/strs.go" for s in refused.value.ambiguous)
    assert refused.value.ambiguous, refused.value
    assert {rel: (root / rel).read_text() for rel in files} == before


def test_an_unexported_method_used_by_another_package_is_refused(
    temp_repo: Path,
) -> None:
    root = _repo(
        temp_repo,
        {
            "shape/shape.go": (
                "package shape\n\ntype Box struct{ W int }\n\n"
                "func (b Box) Area() int { return b.W * b.W }\n"
            ),
            "cmd/main.go": (
                'package main\n\nimport "example.com/goren/shape"\n\n'
                "func main() { b := shape.Box{W: 2}; _ = b.Area() }\n"
            ),
        },
    )
    with pytest.raises(RenameRefused):
        _rename(root, "shape.shape.Box.Area", "area")


@pytest.mark.parametrize(
    ("files", "qn_tail", "new_name", "rewritten"),
    [
        # Negatives: every caller is in the defining package (a same-package
        # test file included), the new name stays exported, or the rename
        # exports a package-private name.
        (
            {
                "strs/strs.go": _STRS
                + "\nfunc Title(s string) string { return Slug(s) }\n",
                "strs/strs_internal_test.go": (
                    'package strs\n\nimport "testing"\n\n'
                    'func TestSlug(t *testing.T) { _ = Slug("a") }\n'
                ),
            },
            "strs.strs.Slug",
            "slug",
            ("strs/strs.go", "return slug(s)"),
        ),
        (
            {"strs/strs.go": _STRS, "cmd/main.go": _MAIN},
            "strs.strs.Slug",
            "MakeSlug",
            ("cmd/main.go", "strs.MakeSlug("),
        ),
        (
            {
                "strs/strs.go": (
                    "package strs\n\nfunc slug(s string) string { return s }\n\n"
                    "func Title(s string) string { return slug(s) }\n"
                )
            },
            "strs.strs.slug",
            "Slug",
            ("strs/strs.go", "return Slug(s)"),
        ),
    ],
    ids=["same_package", "stays_exported", "exports"],
)
def test_a_rename_go_accepts_still_applies(
    temp_repo: Path,
    files: dict[str, str],
    qn_tail: str,
    new_name: str,
    rewritten: tuple[str, str],
) -> None:
    root = _repo(temp_repo, files)
    report = _rename(root, qn_tail, new_name)
    assert report.applied, report.message
    rel, text = rewritten
    assert text in (root / rel).read_text()


@pytest.mark.skipif(shutil.which("go") is None, reason="needs the Go toolchain")
def test_the_refused_issue_repo_still_builds(temp_repo: Path) -> None:
    root = _repo(temp_repo, {"strs/strs.go": _STRS, "cmd/main.go": _MAIN})
    with pytest.raises(RenameRefused):
        _rename(root, "strs.strs.Slug", "slug")
    go = subprocess.run(
        ["go", "build", "./..."],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    assert go.returncode == 0, go.stderr
