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
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.parsers.go_frontend.frontend import _parse_payload
from codebase_rag.tests.conftest import get_relationships, run_updater

_LIBA_DIR = Path("/repo/liba").resolve()
_LIBA = {"example.com/liba": frozenset({_LIBA_DIR})}


def _external(
    name: str, module: str | None, line: int, directory: Path | None = None
) -> dict[str, object]:
    site: dict[str, object] = {
        "file": "appb/main.go",
        "line": line,
        "col": 4,
        "name": name,
    }
    if module is not None:
        site["module"] = module
    if directory is not None:
        site["dir"] = str(directory)
    return site


def _externals(entries: list[dict[str, object]]) -> set[str]:
    payload = json.dumps({"calls": [], "externals": entries, "implements": []})
    facts = _parse_payload(payload, in_repo_modules=_LIBA)
    assert facts is not None
    return {key[3] for key in facts.external_sites}


def test_a_sibling_module_callee_is_not_external() -> None:
    # Loaded from the in-repo directory (spelled through `appb/..`), and a
    # fact from an older tool that names the module but no directory.
    assert (
        _externals(
            [
                _external(
                    "Slug", "example.com/liba", 1, _LIBA_DIR.parent / "appb/../liba"
                ),
                _external("Root", "example.com/liba", 2),
            ]
        )
        == set()
    )


def test_a_third_party_callee_stays_external() -> None:
    # Negatives: the standard library (no module), another module whose path
    # only shares a prefix, a separate module nested under the in-repo one's
    # path, a fact from a tool that names no module, and the in-repo
    # module's path loaded from elsewhere (a published copy in the module
    # cache) stay external.
    cached = Path("/gomodcache/example.com/liba@v1.0.0").resolve()
    assert _externals(
        [
            _external("Println", None, 1),
            _external("Thing", "example.com/libabc", 2),
            _external("Greet", "example.com/liba/plugin", 3),
            _external("Old", None, 4),
            _external("Cached", "example.com/liba", 5, cached),
        ]
    ) == {"Println", "Thing", "Greet", "Old", "Cached"}


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


_ROOT_MAIN = (
    'package main\n\nimport (\n\t"fmt"\n\n\t"example.com/root/plugin"\n)\n\n'
    'func Greet() string { return "local" }\n\n'
    "func run() string { return plugin.Greet() }\n\n"
    "func main() { fmt.Println(run(), Greet()) }\n"
)


def test_gotypes_keeps_a_nested_path_dependency_external(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: `example.com/root/plugin` is a module of its own, outside the
    # indexed repository, though its path sits under the indexed module's.
    # Its `Greet` must not bind to the caller's own `Greet`.
    from codebase_rag.parsers.go_frontend import (
        go_frontend_available,
        run_go_frontend,
    )

    if shutil.which("go") is None or not go_frontend_available():
        pytest.skip("go toolchain not available")
    root = temp_repo / "root"
    _write(
        temp_repo,
        "outside/plugin/go.mod",
        "module example.com/root/plugin\n\ngo 1.21\n",
    )
    _write(
        temp_repo,
        "outside/plugin/plugin.go",
        'package plugin\n\nfunc Greet() string { return "dep" }\n',
    )
    _write(
        root,
        "go.mod",
        "module example.com/root\n\ngo 1.21\n\n"
        "require example.com/root/plugin v0.0.0\n\n"
        "replace example.com/root/plugin => ../outside/plugin\n",
    )
    _write(root, "main.go", _ROOT_MAIN)
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
        a.endswith("main.main") and b.endswith("main.Greet") for a, b in calls
    ), calls
    assert not any(
        a.endswith("main.run") and b.endswith("main.Greet") for a, b in calls
    ), calls


def _publish(proxy: Path, module: str, version: str, files: dict[str, str]) -> None:
    # One released version on a `file://` GOPROXY, so the module cache case
    # runs offline.
    versions = proxy / module / "@v"
    versions.mkdir(parents=True, exist_ok=True)
    (versions / "list").write_text(f"{version}\n", encoding="utf-8")
    (versions / f"{version}.info").write_text(
        json.dumps({"Version": version, "Time": "2024-01-01T00:00:00Z"}),
        encoding="utf-8",
    )
    (versions / f"{version}.mod").write_text(files["go.mod"], encoding="utf-8")
    with zipfile.ZipFile(versions / f"{version}.zip", "w") as archive:
        for rel, text in files.items():
            archive.writestr(f"{module}@{version}/{rel}", text)


def test_gotypes_keeps_a_cached_copy_of_an_in_repo_module_external(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: `appb` requires a published `example.com/liba` with no
    # `replace` or `go.work`, so Go builds it from the module cache although
    # the repository holds a `liba` of the same path. The call goes to the
    # cached copy and must not bind to the repository's `Slug`.
    from codebase_rag.parsers.go_frontend import frontend

    go = shutil.which("go")
    if go is None:
        pytest.skip("go toolchain not available")
    # Built first: the environment below points the module cache, which the
    # helper's own build reads, at an offline proxy.
    if frontend._build_tool(go) is None:
        pytest.skip("gotypes frontend could not build in this environment")
    liba_mod = "module example.com/liba\n\ngo 1.21\n"
    proxy = temp_repo / "proxy"
    _publish(
        proxy,
        "example.com/liba",
        "v1.0.0",
        {"go.mod": liba_mod, "strs/strs.go": _LIBA_SRC},
    )
    monkeypatch.setenv("GOPROXY", proxy.as_uri())
    monkeypatch.setenv("GOSUMDB", "off")
    monkeypatch.setenv("GOMODCACHE", str(temp_repo / "modcache"))
    monkeypatch.setenv("GOFLAGS", "-mod=mod -modcacherw")
    monkeypatch.delenv("GOWORK", raising=False)
    root = temp_repo / "gomm"
    _write(root, "liba/go.mod", liba_mod)
    _write(root, "liba/strs/strs.go", _LIBA_SRC)
    _write(
        root,
        "appb/go.mod",
        "module example.com/appb\n\ngo 1.21\n\nrequire example.com/liba v1.0.0\n",
    )
    _write(root, "appb/main.go", _MAIN)
    if not frontend.run_go_frontend(root).call_sites:
        pytest.skip("gotypes frontend could not build in this environment")

    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    ingestor = MagicMock()
    run_updater(root, ingestor, skip_if_missing="go")
    calls = {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(ingestor, "CALLS")
    }
    assert any(
        a.endswith("appb.main.main") and b.endswith("appb.main.run") for a, b in calls
    ), calls
    assert not any(b.endswith("liba.strs.strs.Slug") for _, b in calls), calls
