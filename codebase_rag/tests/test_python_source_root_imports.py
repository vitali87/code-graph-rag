from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _run_calls(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    # Build the graph for `files` (repo-relative paths) and return CALLS edges.
    parsers, queries = load_parsers()
    if "python" not in parsers:
        pytest.skip("python parser not available")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="repo",
    ).run()
    out: set[tuple[str, str]] = set()
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] == "CALLS":
            out.add((c.args[0][2], c.args[2][2]))
    return out


def _has(calls: set[tuple[str, str]], caller_suffix: str, callee_suffix: str) -> bool:
    return any(
        a.endswith(caller_suffix) and b.endswith(callee_suffix) for a, b in calls
    )


IMPL = "def _handler(ctx):\n    return 1\n"
CALLER = (
    "from {pkg}.impls import _handler\n\n\ndef use(ctx):\n    return _handler(ctx)\n"
)
DISPATCH = "from {pkg}.impls import _handler\n\nhandlers = {{'a': _handler}}\n"


def test_flat_layout_absolute_import(tmp_path: Path) -> None:
    # Regression: package at the repo root, absolute import (already worked).
    calls = _run_calls(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/impls.py": IMPL,
            "pkg/registry.py": CALLER.format(pkg="pkg"),
        },
    )
    assert _has(calls, "pkg.registry.use", "pkg.impls._handler")


def test_src_layout_absolute_import(tmp_path: Path) -> None:
    # src-layout: the package's import name (pkg) differs from its repo-relative
    # path (packages/a/src/pkg), so the absolute import must resolve to the
    # path-based node, not be dropped as external.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/pkg/__init__.py": "",
            "packages/a/src/pkg/impls.py": IMPL,
            "packages/a/src/pkg/registry.py": CALLER.format(pkg="pkg"),
        },
    )
    assert _has(calls, "src.pkg.registry.use", "src.pkg.impls._handler")


def test_src_layout_dispatch_dict_value(tmp_path: Path) -> None:
    # The dead-code trigger shape: a module-level dispatch dict referencing the
    # imported handler must produce a Module -> CALLS -> handler reference edge.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/pkg/__init__.py": "",
            "packages/a/src/pkg/impls.py": IMPL,
            "packages/a/src/pkg/registry.py": DISPATCH.format(pkg="pkg"),
        },
    )
    assert _has(calls, "src.pkg.registry", "src.pkg.impls._handler")


def test_monorepo_cross_package_absolute_import(tmp_path: Path) -> None:
    # Two packages, each under its own src root; libb absolutely imports liba.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/liba/__init__.py": "",
            "packages/a/src/liba/impls.py": IMPL,
            "packages/b/src/libb/__init__.py": "",
            "packages/b/src/libb/registry.py": CALLER.format(pkg="liba"),
        },
    )
    assert _has(calls, "libb.registry.use", "liba.impls._handler")


def test_package_dir_remap_absolute_import(tmp_path: Path) -> None:
    # setuptools package-dir remap: import name (mypkg) maps to a directory with
    # a DIFFERENT name (lib/), declared in pyproject.toml.
    calls = _run_calls(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\n"
                'name = "mypkg"\n'
                "[tool.setuptools.package-dir]\n"
                'mypkg = "lib"\n'
            ),
            "lib/__init__.py": "",
            "lib/impls.py": IMPL,
            "lib/registry.py": CALLER.format(pkg="mypkg"),
        },
    )
    assert _has(calls, "lib.registry.use", "lib.impls._handler")


def test_namespace_package_under_src(tmp_path: Path) -> None:
    # PEP 420 namespace package: no __init__.py anywhere, package under a src
    # root. The import must still map to the path-based nodes.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/nspkg/impls.py": IMPL,
            "packages/a/src/nspkg/registry.py": CALLER.format(pkg="nspkg"),
        },
    )
    assert _has(calls, "nspkg.registry.use", "nspkg.impls._handler")


def test_same_named_packages_disambiguate_by_submodule(tmp_path: Path) -> None:
    # Two source roots each expose a top-level `common`, with different
    # submodules. The import of common.only_in_a must bind to package a's copy.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/common/__init__.py": "",
            "packages/a/src/common/only_in_a.py": IMPL,
            "packages/b/src/common/__init__.py": "",
            "packages/b/src/common/only_in_b.py": IMPL,
            "packages/b/src/app/__init__.py": "",
            "packages/b/src/app/registry.py": (
                "from common.only_in_a import _handler\n\n\n"
                "def use(ctx):\n"
                "    return _handler(ctx)\n"
            ),
        },
    )
    assert _has(calls, "app.registry.use", "a.src.common.only_in_a._handler")


def test_package_dir_default_key_remap(tmp_path: Path) -> None:
    # setuptools' default remap `"" = "lib"` relocates the whole package
    # namespace; a namespace package under it (no __init__.py, dir not named
    # src) has no other discovery signal and must still resolve.
    calls = _run_calls(
        tmp_path,
        {
            "pyproject.toml": (
                '[project]\nname = "proj"\n[tool.setuptools.package-dir]\n"" = "lib"\n'
            ),
            "lib/nspkg/impls.py": IMPL,
            "lib/nspkg/registry.py": CALLER.format(pkg="nspkg"),
        },
    )
    assert _has(calls, "nspkg.registry.use", "lib.nspkg.impls._handler")


def test_single_module_under_src_resolves_to_module(tmp_path: Path) -> None:
    # A single-module file directly under src (src/mymod.py) is importable as
    # `mymod`; the import must resolve to the module's own path QN, not to the
    # containing src directory.
    calls = _run_calls(
        tmp_path,
        {
            "packages/a/src/mymod.py": IMPL,
            "packages/a/src/consumer.py": (
                "from mymod import _handler\n\n\n"
                "def use(ctx):\n"
                "    return _handler(ctx)\n"
            ),
        },
    )
    assert _has(calls, "src.consumer.use", "src.mymod._handler")


def test_package_dir_escaping_repo_does_not_crash(tmp_path: Path) -> None:
    # A package-dir pointing outside the repo (`..`) must be skipped, not crash
    # discovery with a relative_to ValueError; sibling code still resolves.
    (tmp_path.parent / "outside").mkdir(exist_ok=True)
    calls = _run_calls(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\n"
                'name = "proj"\n'
                "[tool.setuptools.package-dir]\n"
                'escapee = "../outside"\n'
            ),
            "pkg/__init__.py": "",
            "pkg/impls.py": IMPL,
            "pkg/registry.py": CALLER.format(pkg="pkg"),
        },
    )
    assert _has(calls, "pkg.registry.use", "pkg.impls._handler")


def test_single_module_root_maps_to_module_path(tmp_path: Path) -> None:
    # The discovery map must key a single-module file to the module's OWN dotted
    # path (…src.mymod), not its containing directory (…src), or resolution
    # returns a directory QN and downstream lookups depend on trie luck.
    from codebase_rag.parsers.python_source_roots import (
        discover_python_source_roots,
        resolve_via_source_roots,
    )

    (tmp_path / "packages/a/src").mkdir(parents=True)
    (tmp_path / "packages/a/src/mymod.py").write_text(IMPL, encoding="utf-8")
    roots = discover_python_source_roots(tmp_path)
    assert resolve_via_source_roots(tmp_path, roots, "mymod") == "packages.a.src.mymod"


def test_package_dir_dotted_key_remap(tmp_path: Path) -> None:
    # package-dir keys can name a dotted subpackage ("acme.widgets" =
    # "lib/widgets"); the import's top-level segment (acme) has no entry of its
    # own, so resolution must match the longest dotted prefix, not the top name.
    calls = _run_calls(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\n"
                'name = "acme-widgets"\n'
                "[tool.setuptools.package-dir]\n"
                '"acme.widgets" = "lib/widgets"\n'
            ),
            "lib/widgets/__init__.py": "",
            "lib/widgets/impl.py": IMPL,
            "app.py": (
                "from acme.widgets.impl import _handler\n\n\n"
                "def use(ctx):\n"
                "    return _handler(ctx)\n"
            ),
        },
    )
    assert _has(calls, "app.use", "lib.widgets.impl._handler")
