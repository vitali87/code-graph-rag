"""A Go method that satisfies an in-project interface is not renamed alone.

Go interfaces are satisfied implicitly: `Box` implements `Sizer` because it
has every method `Sizer` declares, by name. Renaming `Box.Size` alone left
`Sizer.Size`, `Bag.Size` and the calls through the interface as they were,
so `Box` no longer implemented `Sizer` and the package stopped compiling,
while the rename reported `ok` (issue #3253). The interface's methods are
not graph nodes, so the rename refuses instead of renaming half the set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.tests.test_edit_contract import PROJECT, _real_project

_GO = shutil.which("go")
needs_go = pytest.mark.skipif(_GO is None, reason="go is not installed")

_GOMOD = "module example.com/gr\n\ngo 1.21\n"
_MAIN = """package main

import "fmt"

type Sizer interface{ Size() int }

type Box struct{ n int }

func (b Box) Size() int { return b.n }

func (b Box) Extra() int { return 1 }

type Bag struct{ items []int }

func (g Bag) Size() int { return len(g.items) }

func total(xs ...Sizer) int {
	t := 0
	for _, x := range xs {
		t += x.Size()
	}
	return t
}

func main() { fmt.Println(total(Box{n: 2}, Bag{items: []int{1}}) + Box{}.Extra()) }
"""


def _go_build(root: Path) -> None:
    assert _GO is not None
    result = subprocess.run(
        [_GO, "build", "./..."],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
        env={
            **os.environ,
            "GOTOOLCHAIN": "local",
            "GOCACHE": str(root.parent / "gocache"),
        },
    )
    assert result.returncode == 0, result.stderr


def _rename(root: Path, files: dict[str, str], qn: str, new: str) -> None:
    store, updater = _real_project(root, files)
    report = rename(root, store.fetch_all, PROJECT, qn, new, reingest=updater.reingest)
    assert report.applied, report.message


@needs_go
def test_a_method_satisfying_an_in_project_interface_refuses(temp_repo: Path) -> None:
    root = temp_repo / PROJECT
    store, updater = _real_project(root, {"go.mod": _GOMOD, "main.go": _MAIN})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.main.Box.Size",
            "Len",
            reingest=updater.reingest,
        )

    message = str(refused.value)
    assert f"{PROJECT}.main.Sizer (main.go:5)" in message, message
    assert (root / "main.go").read_text() == _MAIN
    _go_build(root)


@needs_go
def test_a_method_no_interface_declares_is_renamed(temp_repo: Path) -> None:
    # Negative: `Extra` is in no interface's method set, so it renames as
    # before, and the program still builds.
    root = temp_repo / PROJECT

    _rename(
        root,
        {"go.mod": _GOMOD, "main.go": _MAIN},
        f"{PROJECT}.main.Box.Extra",
        "Spare",
    )

    assert "func (b Box) Spare() int" in (root / "main.go").read_text()
    _go_build(root)


@needs_go
def test_a_type_missing_an_interface_method_does_not_satisfy_it(
    temp_repo: Path,
) -> None:
    # Negative: `Shape` also declares `Area`, which `Box` lacks, so `Box`
    # does not implement `Shape` and its `Size` is free to rename.
    source = (
        'package main\n\nimport "fmt"\n\n'
        "type Shape interface {\n\tSize() int\n\tArea() int\n}\n\n"
        "type Box struct{ n int }\n\nfunc (b Box) Size() int { return b.n }\n\n"
        "func main() { fmt.Println(Box{n: 2}.Size()) }\n"
    )
    root = temp_repo / PROJECT

    _rename(
        root, {"go.mod": _GOMOD, "main.go": source}, f"{PROJECT}.main.Box.Size", "Len"
    )

    text = (root / "main.go").read_text()
    assert "func (b Box) Len() int" in text and "Box{n: 2}.Len()" in text, text
    _go_build(root)


def test_a_proven_implements_edge_refuses_without_the_full_method_set(
    temp_repo: Path,
) -> None:
    # `Box` gets `Len` from its embedded `Inner`, so its own methods are not
    # all of `Sizer`'s; go/types still proves `Box IMPLEMENTS Sizer`, and an
    # edge the compiler proved refuses the rename as surely as a name match.
    source = (
        "package main\n\ntype Sizer interface {\n\tSize() int\n\tLen() int\n}\n\n"
        "type Inner struct{}\n\nfunc (Inner) Len() int { return 0 }\n\n"
        "type Box struct{ Inner }\n\nfunc (Box) Size() int { return 1 }\n\n"
        "var _ Sizer = Box{}\n\nfunc main() {}\n"
    )
    root = temp_repo / PROJECT
    store, updater = _real_project(root, {"go.mod": _GOMOD, "main.go": source})
    # What the go/types frontend records; its tool may not build here.
    store.ensure_relationship_batch(
        (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.main.Box"),
        cs.RelationshipType.IMPLEMENTS.value,
        (cs.NodeLabel.INTERFACE, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.main.Sizer"),
    )

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.main.Box.Size",
            "Count",
            reingest=updater.reingest,
        )

    assert f"{PROJECT}.main.Sizer (main.go:3)" in str(refused.value)


@needs_go
def test_a_grouped_interface_is_read_from_its_own_declaration(
    temp_repo: Path,
) -> None:
    # `Sizer` sits in a grouped `type ( ... )` block, and a function later in
    # the file declares a local `Sizer` without `Size`: the method set is
    # read from the declaration the graph node spans, never a namesake.
    source = (
        'package main\n\nimport "fmt"\n\ntype (\n\tSizer interface{ Size() int }\n'
        "\tBox   struct{ n int }\n)\n\nfunc (b Box) Size() int { return b.n }\n\n"
        "func local() int {\n\ttype Sizer interface{ Other() int }\n"
        "\tvar s Sizer\n\t_ = s\n\treturn 0\n}\n\n"
        "func main() { var s Sizer = Box{n: 1}; fmt.Println(s.Size() + local()) }\n"
    )
    root = temp_repo / PROJECT
    store, updater = _real_project(root, {"go.mod": _GOMOD, "main.go": source})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.main.Box.Size",
            "Len",
            reingest=updater.reingest,
        )

    assert f"{PROJECT}.main.Sizer (main.go:" in str(refused.value)
    _go_build(root)


def test_a_python_method_is_never_held_to_a_go_interface(temp_repo: Path) -> None:
    # Negative: Go's implicit interfaces bind Go types only; a Python class
    # whose method shares the Go interface's method name renames freely.
    root = temp_repo / PROJECT

    _rename(
        root,
        {
            "go.mod": _GOMOD,
            "main.go": "package main\n\ntype Sizer interface{ Size() int }\n\nfunc main() {}\n",
            "box.py": "class Box:\n    def Size(self):\n        return 1\n\n\nprint(Box().Size())\n",
        },
        f"{PROJECT}.box.Box.Size",
        "Len",
    )

    assert "def Len(self)" in (root / "box.py").read_text()


@needs_go
def test_interfaces_declared_on_one_line_keep_their_own_method_sets(
    temp_repo: Path,
) -> None:
    # Two declarations share a line, so only the name tells them apart:
    # `Sizer` (which `Box` satisfies) is read as `Sizer`, never as `Other`.
    source = (
        "package main\n\n"
        "type Sizer interface{ Size() int }; type Other interface{ Size() int; Extra() int }\n\n"
        "type Box struct{}\n\nfunc (Box) Size() int { return 1 }\n\n"
        "var _ Sizer = Box{}\n\nfunc main() {}\n"
    )
    root = temp_repo / PROJECT
    store, updater = _real_project(root, {"go.mod": _GOMOD, "main.go": source})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.main.Box.Size",
            "Len",
            reingest=updater.reingest,
        )

    message = str(refused.value)
    assert f"{PROJECT}.main.Sizer (main.go:3)" in message, message
    assert f"{PROJECT}.main.Other" not in message, message
    _go_build(root)
