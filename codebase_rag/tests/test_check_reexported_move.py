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


@pytest.mark.parametrize(
    "util_after",
    [
        pytest.param("slug = None\n", id="none"),
        pytest.param('slug: str = "slug"\n', id="annotated-string"),
    ],
)
def test_a_function_replaced_by_a_value_leaves_its_callers_dangling(
    temp_repo: Path, util_after: str
) -> None:
    # Negative (Greptile, PR #3312): the name still imports, but `slug(t)`
    # raises TypeError, so binding it to a literal serves no caller.
    store, updater = _index(temp_repo, FILES)
    (temp_repo / "py/util.py").write_text(util_after, encoding="utf-8")

    delta = observe(
        store.fetch_all,
        PROJECT,
        ["py/util.py"],
        lambda: updater.reingest(["py/util.py"]),
        repo_root=temp_repo,
    )

    assert _callers(delta) == [("page", "util")]
    assert "TypeError" in _runs(temp_repo)


@pytest.mark.parametrize(
    "guard",
    [
        pytest.param(
            "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:", id="bare"
        ),
        pytest.param("import typing\n\nif typing.TYPE_CHECKING:", id="qualified"),
    ],
)
def test_a_re_export_only_type_checkers_see_leaves_its_callers_dangling(
    temp_repo: Path, guard: str
) -> None:
    # Negative (PR #3312 review): an import under `if TYPE_CHECKING:` never
    # runs, so `from py.util import slug` fails and nothing still binds it.
    delta = _move(temp_repo, f"{guard}\n    {REEXPORT}")

    assert _callers(delta) == [("page", "util")]
    assert [(d["importer"], d["name"]) for d in delta["dangling_importers"]] == [
        (f"{PROJECT}.py.app", "slug")
    ]
    assert "ImportError" in _runs(temp_repo)


def test_a_re_export_in_the_else_of_a_type_checking_guard_is_not_dangling(
    temp_repo: Path,
) -> None:
    # The `else` of `if TYPE_CHECKING:` is the branch that runs.
    util = (
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    pass\n"
        f"else:\n    {REEXPORT}"
    )

    delta = _move(temp_repo, util)

    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []
    assert _runs(temp_repo) == "hello-world"


def test_a_re_export_in_the_else_of_a_negated_guard_leaves_its_callers_dangling(
    temp_repo: Path,
) -> None:
    # Negative (CodeRabbit, PR #3312): `if not TYPE_CHECKING:` runs its
    # body, so its `else` is the branch only type checkers read.
    util = (
        "from typing import TYPE_CHECKING\n\nif not TYPE_CHECKING:\n    pass\n"
        f"else:\n    {REEXPORT}"
    )

    delta = _move(temp_repo, util)

    assert _callers(delta) == [("page", "util")]
    assert [(d["importer"], d["name"]) for d in delta["dangling_importers"]] == [
        (f"{PROJECT}.py.app", "slug")
    ]
    assert "ImportError" in _runs(temp_repo)


def test_a_re_export_in_the_body_of_a_negated_guard_is_not_dangling(
    temp_repo: Path,
) -> None:
    util = f"import typing\n\nif not typing.TYPE_CHECKING:\n    {REEXPORT}"

    delta = _move(temp_repo, util)

    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []
    assert _runs(temp_repo) == "hello-world"


def test_a_re_export_of_a_definition_only_type_checkers_see_leaves_its_callers_dangling(
    temp_repo: Path,
) -> None:
    # Negative (Greptile, CodeRabbit, PR #3312): the graph records the `def`
    # under `if TYPE_CHECKING:`, but it never runs, so the re-export raises
    # ImportError and binds nothing for `from py.util import slug`.
    store, updater = _index(temp_repo, FILES)
    (temp_repo / "py/text.py").write_text(
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n\n"
        + "".join(f"    {line}\n" for line in UTIL.splitlines()),
        encoding="utf-8",
    )
    (temp_repo / "py/util.py").write_text(REEXPORT, encoding="utf-8")
    paths = ["py/util.py", "py/text.py"]

    delta = observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=temp_repo,
    )

    assert _callers(delta) == [("page", "util")]
    assert (f"{PROJECT}.py.app", "slug") in [
        (d["importer"], d["name"]) for d in delta["dangling_importers"]
    ]
    assert "ImportError" in _runs(temp_repo)


def test_a_definition_in_the_running_branch_of_a_guard_is_not_dangling(
    temp_repo: Path,
) -> None:
    # The `else` of `if TYPE_CHECKING:` runs, so its `def` binds the name.
    store, updater = _index(temp_repo, FILES)
    (temp_repo / "py/text.py").write_text(
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    pass\nelse:\n\n"
        + "".join(f"    {line}\n" for line in UTIL.splitlines()),
        encoding="utf-8",
    )
    (temp_repo / "py/util.py").write_text(REEXPORT, encoding="utf-8")
    paths = ["py/util.py", "py/text.py"]

    delta = observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=temp_repo,
    )

    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []
    assert _runs(temp_repo) == "hello-world"


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


TS_SLUG = "export function slug(s: string): string {\n  return s.toLowerCase();\n}\n"


def _move_ts(root: Path, util_after: str, text: str = TS_SLUG) -> StructuralDelta:
    """Index a TS `slug` and its caller, move `slug` into `src/text.ts`."""
    app = (
        'import { slug } from "./util";\n\n'
        "export function page(t: string): string {\n  return slug(t);\n}\n"
    )
    store, updater = _index(root, {"src/util.ts": TS_SLUG, "src/app.ts": app})
    (root / "src/text.ts").write_text(text, encoding="utf-8")
    (root / "src/util.ts").write_text(util_after, encoding="utf-8")
    paths = ["src/util.ts", "src/text.ts"]
    return observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=root,
    )


@pytest.mark.parametrize(
    ("util_after", "dangling"),
    [
        pytest.param('export { slug } from "./text";\n', [], id="re-exported"),
        # Negative (Codex, PR #3312): an import the module does not export
        # leaves `import { slug } from "./util"` failing.
        pytest.param(
            'import { slug } from "./text";\n\n'
            'export function other(): string {\n  return slug("A");\n}\n',
            [("page", "util")],
            id="imported-only",
        ),
        # Negative: exported, but under another name than the caller's.
        pytest.param(
            'import { slug } from "./text";\n\nexport { slug as other };\n',
            [("page", "util")],
            id="exported-as-another-name",
        ),
        # Negative: nothing left behind, so `import { slug } from "./util"`
        # binds nothing.
        pytest.param("", [("page", "util")], id="nothing-left"),
        # Negative (Greptile, PR #3312): a type-only export is erased from
        # the emitted JavaScript, so no caller can import `slug` through it.
        pytest.param(
            'export type { slug } from "./text";\n',
            [("page", "util")],
            id="type-only-re-export",
        ),
        pytest.param(
            'export { type slug } from "./text";\n',
            [("page", "util")],
            id="type-only-re-export-specifier",
        ),
        pytest.param(
            'import { slug } from "./text";\n\nexport type { slug };\n',
            [("page", "util")],
            id="type-only-export",
        ),
        pytest.param(
            'import { slug } from "./text";\n\nexport { type slug };\n',
            [("page", "util")],
            id="type-only-export-specifier",
        ),
        pytest.param(
            'import type { slug } from "./text";\n\nexport { slug };\n',
            [("page", "util")],
            id="type-only-import",
        ),
        # A type-only specifier beside it leaves the value one exported.
        pytest.param(
            'export { type Other, slug } from "./text";\n',
            [],
            id="value-specifier-beside-a-type-only-one",
        ),
    ],
)
def test_a_typescript_move_with_a_re_export(
    temp_repo: Path, util_after: str, dangling: list[tuple[str, str]]
) -> None:
    delta = _move_ts(temp_repo, util_after)

    assert _callers(delta) == dangling


@pytest.mark.parametrize(
    "util_after",
    [
        pytest.param(
            'import { slug } from "./text";\n\nexport { slug };\n', id="same-name"
        ),
        pytest.param(
            'import { slug } from "./text";\n\nexport { /* kept */ slug as slug };\n',
            id="aliased-to-itself",
        ),
        pytest.param(
            'import { slug as s } from "./text";\n\nexport { s as slug };\n',
            id="alias",
        ),
        # Not an import: the module's own declaration, exported as `slug`.
        pytest.param(
            "function slugify(s: string): string {\n  return s;\n}\n\n"
            "export { slugify as slug };\n",
            id="own-declaration",
        ),
    ],
)
def test_a_typescript_import_the_old_module_exports_is_not_dangling(
    temp_repo: Path, util_after: str
) -> None:
    # CodeRabbit, PR #3312: an `export { ... }` without `from` that names
    # the import shares it as `export { slug } from "./text"` does.
    delta = _move_ts(temp_repo, util_after)

    assert delta["dangling_callers"] == []
    assert delta["dangling_importers"] == []


_TS_PLAIN_SLUG = "function slug(s: string): string {\n  return s.toLowerCase();\n}\n"


@pytest.mark.parametrize(
    ("text", "dangling"),
    [
        # Negative (Greptile, PR #3312): `text.ts` declares `slug` without
        # exporting it, so `export { slug } from "./text"` is an error and
        # binds nothing for `import { slug } from "./util"`.
        pytest.param(_TS_PLAIN_SLUG, [("page", "util")], id="not-exported"),
        # Negative: a default export binds `default`, not `slug`.
        pytest.param(
            f"export default {_TS_PLAIN_SLUG}", [("page", "util")], id="default-only"
        ),
        pytest.param(TS_SLUG, [], id="exported-declaration"),
        pytest.param(
            "export const slug = (s: string): string => s.toLowerCase();\n",
            [],
            id="exported-const",
        ),
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nexport {{ slug }};\n", [], id="exported-by-a-clause"
        ),
        pytest.param(
            _TS_PLAIN_SLUG.replace("slug", "slugify", 1)
            + "\nexport { slugify as slug };\n",
            [],
            id="exported-under-its-name",
        ),
    ],
)
def test_a_typescript_re_export_binds_only_what_its_target_exports(
    temp_repo: Path, text: str, dangling: list[tuple[str, str]]
) -> None:
    delta = _move_ts(temp_repo, 'export { slug } from "./text";\n', text)

    assert _callers(delta) == dangling


@pytest.mark.parametrize(
    ("text", "dangling"),
    [
        pytest.param(f"export default {_TS_PLAIN_SLUG}", [], id="named-function"),
        # Every `export default` exports `default`, named or not.
        pytest.param(
            "export default function (s: string): string {\n  return s;\n}\n",
            [],
            id="anonymous-function",
        ),
        pytest.param(
            "export default async function (s: string): Promise<string> {\n"
            "  return s;\n}\n",
            [],
            id="anonymous-async-function",
        ),
        pytest.param(
            "export default (s: string): string => s.toLowerCase();\n",
            [],
            id="arrow-function",
        ),
        pytest.param(f"{_TS_PLAIN_SLUG}\nexport default slug;\n", [], id="identifier"),
        pytest.param(
            "class Slugger {}\n\nexport default new Slugger();\n",
            [],
            id="constructed-value",
        ),
        # Negative: a literal is importable, but no call runs on it.
        pytest.param(
            "export default { lower: true };\n", [("page", "util")], id="object-literal"
        ),
        # Negative: an interface is erased from the emitted JavaScript.
        pytest.param(
            "export default interface Slug {\n  s: string;\n}\n",
            [("page", "util")],
            id="interface",
        ),
    ],
)
def test_a_typescript_re_export_of_a_default_export(
    temp_repo: Path, text: str, dangling: list[tuple[str, str]]
) -> None:
    delta = _move_ts(temp_repo, 'export { default as slug } from "./text";\n', text)

    assert _callers(delta) == dangling


_DANGLES = [("page", "util")]


@pytest.mark.parametrize(
    ("text", "dangling"),
    [
        # Negative (Greptile, PR #3312): a wrapper keeps the value inside it,
        # so a literal under one still serves no caller.
        pytest.param(
            "export default ({ lower: true });\n", _DANGLES, id="parenthesized"
        ),
        pytest.param(
            "export default ((({ lower: true })));\n", _DANGLES, id="nested-parens"
        ),
        pytest.param(
            "export default ({ lower: true } /* options */);\n",
            _DANGLES,
            id="parenthesized-with-a-comment",
        ),
        pytest.param(
            "export default { lower: true } as const;\n", _DANGLES, id="as-const"
        ),
        pytest.param(
            "export default { lower: true } satisfies object;\n",
            _DANGLES,
            id="satisfies",
        ),
        pytest.param(
            "export default <object>{ lower: true };\n",
            _DANGLES,
            id="type-assertion",
        ),
        pytest.param("export default (0, { lower: true });\n", _DANGLES, id="sequence"),
        pytest.param('export default ["slug"];\n', _DANGLES, id="array"),
        pytest.param("export default `slug`;\n", _DANGLES, id="template"),
        pytest.param("export default -1;\n", _DANGLES, id="unary"),
        pytest.param('export default "sl" + "ug";\n', _DANGLES, id="binary"),
        # Negative: the name under the wrapper is one the module never binds.
        pytest.param(
            "export default (missing as unknown);\n", _DANGLES, id="unbound-name"
        ),
        # A name under a wrapper is the module's binding of it.
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nexport default (slug as typeof slug);\n",
            [],
            id="as-name",
        ),
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nexport default slug!;\n", [], id="non-null-name"
        ),
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nexport default ({{ lower: true }}, slug);\n",
            [],
            id="sequence-ending-in-a-name",
        ),
        pytest.param(
            "export default ((s: string): string => s) satisfies "
            "(s: string) => string;\n",
            [],
            id="satisfies-arrow",
        ),
        # Either operand may be the function, so the value is exported.
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nconst on = true;\nexport default on ? slug : slug;\n",
            [],
            id="conditional",
        ),
        pytest.param(
            f"{_TS_PLAIN_SLUG}\nexport default null ?? slug;\n", [], id="nullish"
        ),
    ],
)
def test_a_default_export_is_read_through_its_wrappers(
    temp_repo: Path, text: str, dangling: list[tuple[str, str]]
) -> None:
    delta = _move_ts(temp_repo, 'export { default as slug } from "./text";\n', text)

    assert _callers(delta) == dangling


def test_a_re_export_of_an_anonymous_default_class_is_not_dangling(
    temp_repo: Path,
) -> None:
    # Greptile, PR #3312: the graph holds `export default class { }` as the
    # module's `default`, which `export { default as Widget }` re-exports.
    # A call of one of its methods is reported as for any moved class.
    widget = 'export class Widget {\n  name(): string {\n    return "w";\n  }\n}\n'
    app = (
        'import { Widget } from "./util";\n\n'
        "export function page(): Widget {\n  return new Widget();\n}\n"
    )
    store, updater = _index(temp_repo, {"src/util.ts": widget, "src/app.ts": app})
    (temp_repo / "src/text.ts").write_text(
        widget.replace("export class Widget", "export default class"),
        encoding="utf-8",
    )
    (temp_repo / "src/util.ts").write_text(
        'export { default as Widget } from "./text";\n', encoding="utf-8"
    )
    paths = ["src/util.ts", "src/text.ts"]

    delta = observe(
        store.fetch_all,
        PROJECT,
        paths,
        lambda: updater.reingest(paths),
        repo_root=temp_repo,
    )

    assert delta["dangling_callers"] == []


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
