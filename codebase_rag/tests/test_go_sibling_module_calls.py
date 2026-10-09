"""A call into a sibling in-repo Go module is first-party, not external.

The go/types tool runs once per module and calls every function outside the
module it analyses external. In a multi-module repo (`replace => ../liba` or
`go.work`) a call from `appb` into `liba` was therefore an external site,
which suppresses the tree-sitter resolution that binds it `exact`: `run`
lost its CALLS edge to `liba`'s `Slug`, and `Slug` looked unused (issue
#2809).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.go_frontend.frontend import _parse_payload
from codebase_rag.tests.conftest import get_relationships, run_updater

_LIBA = frozenset({"example.com/liba"})


def _external(name: str, pkg: str | None, line: int) -> dict[str, object]:
    site: dict[str, object] = {
        "file": "appb/main.go",
        "line": line,
        "col": 4,
        "name": name,
    }
    if pkg is not None:
        site["pkg"] = pkg
    return site


def _externals(entries: list[dict[str, object]]) -> set[str]:
    payload = json.dumps({"calls": [], "externals": entries, "implements": []})
    facts = _parse_payload(payload, in_repo_modules=_LIBA)
    assert facts is not None
    return {key[3] for key in facts.external_sites}


def test_a_sibling_module_callee_is_not_external() -> None:
    assert (
        _externals(
            [
                _external("Slug", "example.com/liba/strs", 1),
                _external("Root", "example.com/liba", 2),
            ]
        )
        == set()
    )


def test_a_third_party_callee_stays_external() -> None:
    # Negatives: the standard library, another module whose path only shares
    # a prefix, and a fact from a tool that names no package stay external.
    assert _externals(
        [
            _external("Println", "fmt", 1),
            _external("Thing", "example.com/libabc/x", 2),
            _external("Old", None, 3),
        ]
    ) == {"Println", "Thing", "Old"}


_LIBA_SRC = (
    'package strs\n\nimport "strings"\n\n'
    "func Slug(s string) string { return strings.ToLower(s) }\n"
)
_MAIN = (
    'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/liba/strs"\n)\n\n'
    'func run() string { return strs.Slug("Hello World") }\n\n'
    "func main() { fmt.Println(run()) }\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("wiring", ["replace", "go.work"])
def test_gotypes_keeps_the_cross_module_call(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch, wiring: str
) -> None:
    from codebase_rag.parsers.go_frontend import (
        go_frontend_available,
        run_go_frontend,
    )

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    root = temp_repo / "gomm"
    _write(root, "liba/go.mod", "module example.com/liba\n\ngo 1.21\n")
    _write(root, "liba/strs/strs.go", _LIBA_SRC)
    appb_mod = "module example.com/appb\n\ngo 1.21\n\nrequire example.com/liba v0.0.0\n"
    if wiring == "replace":
        appb_mod += "\nreplace example.com/liba => ../liba\n"
    else:
        _write(root, "go.work", "go 1.21\n\nuse (\n\t./liba\n\t./appb\n)\n")
    _write(root, "appb/go.mod", appb_mod)
    _write(root, "appb/main.go", _MAIN)
    if not run_go_frontend(root).call_sites:
        pytest.skip("gotypes frontend could not build in this environment")

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="go")
    calls = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(ingestor, "CALLS")
    }
    assert any(
        a.endswith("appb.main.run") and b.endswith("liba.strs.strs.Slug")
        for a, b in calls
    ), calls
    # Negative: the standard library stays external.
    assert not any(b.endswith("Println") for _, b in calls), calls
