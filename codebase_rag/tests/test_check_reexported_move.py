"""A caller that still reaches a moved symbol through a re-export is not dangling.

Moving `slug` from `py/util.py` to `py/text.py` and leaving `from py.text
import slug` behind keeps `from py.util import slug` working, so the
program runs unchanged and `dangling_importers` (issue #2516) is empty. Yet
`dangling_callers` listed every caller the edit left alone, since only a
caller in a re-parsed file was ever asked whether it still binds, and
`cgr check --fail-on-found` exited 1 (issue #3248).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta, has_findings, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "rx"

UTIL = 'def slug(s):\n    return s.lower().replace(" ", "-")\n'
APP = "from py.util import slug\n\n\ndef page(title):\n    return slug(title)\n"
FILES = {"py/__init__.py": "", "py/util.py": UTIL, "py/app.py": APP}
REEXPORT = "from py.text import slug  # noqa: F401  re-export\n"


def _index(root: Path, files: dict[str, str]) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return store, updater


def _move(
    root: Path, util_after: str | None, files: dict[str, str] = FILES
) -> StructuralDelta:
    """Index `files`, move `slug` into `py/text.py`, then observe."""
    store, updater = _index(root, files)
    (root / "py/text.py").write_text(UTIL, encoding="utf-8")
    if util_after is None:
        (root / "py/util.py").unlink()
    else:
        (root / "py/util.py").write_text(util_after, encoding="utf-8")
    paths = ["py/util.py", "py/text.py"]
    return observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=root,
    )


def _runs(root: Path) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "from py.app import page; print(page('Hello World'))",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    return (result.stdout or result.stderr).strip().splitlines()[-1]


def _callers(delta: StructuralDelta) -> list[tuple[str, str]]:
    return [
        (d["caller"].rsplit(".", 1)[-1], d["target"].rsplit(".", 2)[-2])
        for d in delta["dangling_callers"]
    ]


def test_a_caller_reaching_a_move_through_a_re_export_is_not_dangling(
    temp_repo: Path,
) -> None:
    delta = _move(temp_repo, REEXPORT)

    assert delta["symbols"]["renamed"] == [
        {
            "old": f"{PROJECT}.py.util.slug",
            "new": f"{PROJECT}.py.text.slug",
            "path": "py/util.py",
        }
    ]
    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []
    assert not has_findings(delta)
    assert _runs(temp_repo) == "hello-world"


@pytest.mark.parametrize(
    "util_after",
    [
        pytest.param(None, id="old-module-deleted"),
        pytest.param("", id="old-module-emptied"),
        pytest.param("from py.text import other  # noqa: F401\n", id="another-name"),
        # The re-export names a module with no `slug`: nothing is bound.
        pytest.param("from py.app import slug  # noqa: F401\n", id="circular-name"),
    ],
)
def test_a_move_the_old_module_no_longer_binds_leaves_its_callers_dangling(
    temp_repo: Path, util_after: str | None
) -> None:
    # Negative: without the re-export `from py.util import slug` fails, and
    # the caller is reported as before, with where the symbol went.
    delta = _move(temp_repo, util_after)

    assert _callers(delta) == [("page", "util")]
    assert delta["dangling_callers"][0]["renamed_to"] == f"{PROJECT}.py.text.slug"
    assert has_findings(delta)
    assert "Error" in _runs(temp_repo)


def test_a_rename_that_keeps_the_old_name_as_an_alias_is_not_dangling(
    temp_repo: Path,
) -> None:
    store, updater = _index(temp_repo, FILES)
    (temp_repo / "py/util.py").write_text(
        UTIL.replace("def slug(s):", "def slugify(s):") + "\n\nslug = slugify\n",
        encoding="utf-8",
    )

    delta = observe(
        store.fetch_all,
        PROJECT,
        ["py/util.py"],
        lambda: updater.reingest(["py/util.py"]),
        repo_root=temp_repo,
    )

    assert delta["dangling_callers"] == []
    assert _runs(temp_repo) == "hello-world"


def test_a_plain_rename_still_leaves_its_callers_dangling(temp_repo: Path) -> None:
    # Negative: `slug` renamed with nothing binding the old name.
    store, updater = _index(temp_repo, FILES)
    (temp_repo / "py/util.py").write_text(
        UTIL.replace("def slug(s):", "def slugify(s):"), encoding="utf-8"
    )

    delta = observe(
        store.fetch_all,
        PROJECT,
        ["py/util.py"],
        lambda: updater.reingest(["py/util.py"]),
        repo_root=temp_repo,
    )

    assert _callers(delta) == [("page", "util")]


def test_a_removed_method_is_dangling_whatever_its_module_binds(
    temp_repo: Path,
) -> None:
    # Negative: a method is reached through its class, never a module name,
    # so a module-level `total` the edit leaves behind binds nothing for it.
    cart = (
        "def total(items):\n    return len(items)\n\n\n"
        "class Cart:\n    def total(self):\n        return 2\n"
    )
    shop = "from py.cart import Cart\n\n\ndef run():\n    return Cart().total()\n"
    store, updater = _index(
        temp_repo, {"py/__init__.py": "", "py/cart.py": cart, "py/shop.py": shop}
    )
    (temp_repo / "py/cart.py").write_text(
        cart.replace("    def total(self):\n        return 2\n", "    pass\n"),
        encoding="utf-8",
    )

    delta = observe(
        store.fetch_all,
        PROJECT,
        ["py/cart.py"],
        lambda: updater.reingest(["py/cart.py"]),
        repo_root=temp_repo,
    )

    assert _callers(delta) == [("run", "Cart")]


def test_a_caller_through_the_module_name_is_not_dangling(temp_repo: Path) -> None:
    # `import py.util as u; u.slug(t)` reads the name off the module, which
    # the re-export still binds.
    app = "import py.util as u\n\n\ndef page(title):\n    return u.slug(title)\n"

    delta = _move(temp_repo, REEXPORT, {**FILES, "py/app.py": app})

    assert delta["dangling_callers"] == []
    assert _runs(temp_repo) == "hello-world"


@pytest.mark.parametrize(
    ("util_after", "dangling"),
    [
        pytest.param('export { slug } from "./text";\n', [], id="re-exported"),
        # Negative: nothing left behind, so `import { slug } from "./util"`
        # binds nothing.
        pytest.param("", [("page", "util")], id="nothing-left"),
    ],
)
def test_a_typescript_move_with_a_re_export(
    temp_repo: Path, util_after: str, dangling: list[tuple[str, str]]
) -> None:
    util = "export function slug(s: string): string {\n  return s.toLowerCase();\n}\n"
    app = (
        'import { slug } from "./util";\n\n'
        "export function page(t: string): string {\n  return slug(t);\n}\n"
    )
    store, updater = _index(temp_repo, {"src/util.ts": util, "src/app.ts": app})
    (temp_repo / "src/text.ts").write_text(util, encoding="utf-8")
    (temp_repo / "src/util.ts").write_text(util_after, encoding="utf-8")
    paths = ["src/util.ts", "src/text.ts"]

    delta = observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=temp_repo,
    )

    assert _callers(delta) == dangling


def test_cgr_check_passes_a_move_with_a_re_export(temp_repo: Path) -> None:
    root = temp_repo
    store, _updater = _index(root, FILES)
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.email=x@x", "-c", "user.name=x", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "mv", "py/util.py", "py/text.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (root / "py/util.py").write_text(REEXPORT, encoding="utf-8")
    parsers, queries = load_parsers()

    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)

    assert delta["dangling_callers"] == []
    assert not has_findings(delta)
