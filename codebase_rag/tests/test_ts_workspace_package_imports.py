# A monorepo application imports its first-party TypeScript packages by the
# NAME in their package manifest (`@acme/sdk/admin`), never by a relative path,
# so no path arithmetic can resolve them and every call through such an import
# was dropped (issue #945). The manifest name maps to the package directory the
# same way a go.mod module directive maps to its anchor (issue #941), and the
# `exports` map (or `main`) says which file a subpath names; when that target is
# a build artefact the indexed source beside it is what the graph holds.
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.js_ts.module_paths import (
    discover_js_workspace_packages,
    resolve_js_workspace_import,
)

ADMIN_SOURCE = "export class AdminClient {\n  getUsers() {\n    return [];\n  }\n}\n"
CALLER_SOURCE = (
    "import {{ AdminClient }} from '{specifier}';\n\n"
    "export function listUsers() {{\n"
    "  const api = new AdminClient();\n"
    "  return api.getUsers();\n"
    "}}\n"
)


def _manifest(name: str, **fields: object) -> str:
    return json.dumps({"name": name, "version": "0.0.0", **fields})


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "typescript" not in parsers:
        pytest.skip("typescript parser not available")
    root = tmp_path / "repo"
    root.mkdir()
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(ingestor=mock, repo_path=root, parsers=parsers, queries=queries).run()
    return {
        (c.args[0][2], str(c.args[1]), c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
    }


def _calls(rels: set[tuple[str, str, str]], caller_suffix: str) -> set[str]:
    return {b for a, r, b in rels if r == "CALLS" and a.endswith(caller_suffix)}


class TestWorkspaceImportResolution:
    def test_exports_wildcard_through_build_output(self, tmp_path: Path) -> None:
        # The generated-SDK shape: `exports` points at the compiled `dist/`
        # tree, while the file the graph indexed is the TypeScript source it
        # was built from.
        files = {
            "packages/sdk/package.json": _manifest(
                "@acme/sdk", exports={"./*": {"import": "./dist/src/*.js"}}
            ),
            "packages/sdk/src/admin.ts": ADMIN_SOURCE,
            "app/web/src/main.ts": CALLER_SOURCE.format(specifier="@acme/sdk/admin"),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert "repo.packages.sdk.src.admin.AdminClient.getUsers" in calls, calls

    def test_package_root_import_through_main_field(self, tmp_path: Path) -> None:
        # No exports map: the root specifier names the package itself, whose
        # entry point is the `main` build artefact over its indexed source.
        files = {
            "packages/sdk/package.json": _manifest(
                "@acme/sdk", main="./dist/src/index.js"
            ),
            "packages/sdk/src/index.ts": ADMIN_SOURCE,
            "app/web/src/main.ts": CALLER_SOURCE.format(specifier="@acme/sdk"),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert "repo.packages.sdk.src.index.AdminClient.getUsers" in calls, calls

    def test_subpath_without_exports_map(self, tmp_path: Path) -> None:
        files = {
            "packages/sdk/package.json": _manifest("@acme/sdk"),
            "packages/sdk/src/admin.ts": ADMIN_SOURCE,
            "app/web/src/main.ts": CALLER_SOURCE.format(specifier="@acme/sdk/admin"),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert "repo.packages.sdk.src.admin.AdminClient.getUsers" in calls, calls

    def test_import_through_reexporting_index(self, tmp_path: Path) -> None:
        # The generated packages export their symbols from an index barrel, so
        # the mapping has to land on the barrel and then follow its re-export.
        files = {
            "packages/sdk/package.json": _manifest(
                "@acme/sdk", exports={"./*": "./dist/*.js"}
            ),
            "packages/sdk/openapi/admin/sdk.gen.ts": ADMIN_SOURCE,
            "packages/sdk/openapi/admin/index.ts": (
                "export { AdminClient } from './sdk.gen.ts';\n"
            ),
            "app/web/src/main.ts": CALLER_SOURCE.format(
                specifier="@acme/sdk/openapi/admin"
            ),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert (
            "repo.packages.sdk.openapi.admin.sdk.gen.AdminClient.getUsers" in calls
        ), calls

    def test_nested_package_shadows_enclosing_one(self, tmp_path: Path) -> None:
        # Two manifests whose names share a prefix: the longer name owns the
        # import, exactly as the longest go.mod module path does.
        files = {
            "packages/sdk/package.json": _manifest("@acme/sdk"),
            "packages/sdk/src/admin.ts": (
                "export class AdminClient {\n"
                "  getUsers() {\n    return ['outer'];\n  }\n"
                "}\n"
            ),
            "packages/sdk/inner/package.json": _manifest("@acme/sdk-inner"),
            "packages/sdk/inner/src/admin.ts": ADMIN_SOURCE,
            "app/web/src/main.ts": CALLER_SOURCE.format(
                specifier="@acme/sdk-inner/admin"
            ),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert "repo.packages.sdk.inner.src.admin.AdminClient.getUsers" in calls, calls
        assert "repo.packages.sdk.src.admin.AdminClient.getUsers" not in calls, calls

    def test_third_party_package_stays_external(self, tmp_path: Path) -> None:
        # No first-party manifest claims this name, so it must stay external
        # rather than be rebound to a same-named local symbol.
        files = {
            "packages/sdk/package.json": _manifest("@acme/sdk"),
            "packages/sdk/src/admin.ts": ADMIN_SOURCE,
            "app/web/src/other.ts": (
                "export class AdminClient {\n"
                "  getUsers() {\n    return ['local'];\n  }\n"
                "}\n"
            ),
            "app/web/src/main.ts": CALLER_SOURCE.format(specifier="third-party-sdk"),
        }
        calls = _calls(_run_rels(tmp_path, files), "main.listUsers")
        assert "repo.packages.sdk.src.admin.AdminClient.getUsers" not in calls, calls


class TestWorkspaceDiscovery:
    def test_node_modules_manifests_are_ignored(self, tmp_path: Path) -> None:
        # An installed copy of a workspace package carries the SAME name; the
        # first-party source tree must win, never the dependency snapshot.
        (tmp_path / "packages/sdk").mkdir(parents=True)
        (tmp_path / "packages/sdk/package.json").write_text(
            _manifest("@acme/sdk"), encoding="utf-8"
        )
        (tmp_path / "node_modules/@acme/sdk").mkdir(parents=True)
        (tmp_path / "node_modules/@acme/sdk/package.json").write_text(
            _manifest("@acme/sdk"), encoding="utf-8"
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert [d.relative_to(tmp_path).as_posix() for _n, d in packages] == [
            "packages/sdk"
        ]

    def test_unnamed_and_unreadable_manifests_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a/package.json").write_text("{}", encoding="utf-8")
        (tmp_path / "b").mkdir()
        (tmp_path / "b/package.json").write_text("{not json", encoding="utf-8")
        assert discover_js_workspace_packages(tmp_path) == []

    def test_longest_name_first(self, tmp_path: Path) -> None:
        for rel, name in (("a", "@acme/sdk"), ("b", "@acme/sdk-inner")):
            (tmp_path / rel).mkdir()
            (tmp_path / rel / "package.json").write_text(
                _manifest(name), encoding="utf-8"
            )
        assert [n for n, _d in discover_js_workspace_packages(tmp_path)] == [
            "@acme/sdk-inner",
            "@acme/sdk",
        ]

    def test_duplicate_names_order_by_directory(self, tmp_path: Path) -> None:
        # Two copies of one package (a vendored fork, an example tree) must
        # resolve the same way on every run and platform, not in whatever
        # order the filesystem enumerated them.
        for rel in ("apps/copyB", "apps/copyA"):
            (tmp_path / rel).mkdir(parents=True)
            (tmp_path / rel / "package.json").write_text(
                _manifest("@acme/sdk"), encoding="utf-8"
            )
        assert [
            d.relative_to(tmp_path).as_posix()
            for _n, d in discover_js_workspace_packages(tmp_path)
        ] == ["apps/copyA", "apps/copyB"]


class TestWorkspaceResolver:
    @staticmethod
    def _package(tmp_path: Path, manifest: str, sources: dict[str, str]) -> Path:
        pkg = tmp_path / "packages/sdk"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(manifest, encoding="utf-8")
        for rel, content in sources.items():
            p = pkg / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return pkg

    def test_exports_pattern_prefers_longest_static_prefix(
        self, tmp_path: Path
    ) -> None:
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={
                    "./*": "./dist/*.js",
                    "./openapi/*": "./dist/gen/openapi/*/index.js",
                },
            ),
            {
                "gen/openapi/admin/index.ts": ADMIN_SOURCE,
                "openapi/admin.ts": ADMIN_SOURCE,
            },
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/openapi/admin", tmp_path)
            == "packages/sdk/gen/openapi/admin/index"
        )

    def test_built_artefact_never_wins_over_the_source(self, tmp_path: Path) -> None:
        # A checked-in build output is not indexed (dist is an ignored
        # directory), so resolving to it would mint a module qn the graph
        # never holds and drop the call all over again.
        pkg = self._package(
            tmp_path,
            _manifest("@acme/sdk", exports={"./*": "./dist/*.js"}),
            {"src/admin.ts": ADMIN_SOURCE},
        )
        (pkg / "dist").mkdir()
        (pkg / "dist/admin.js").write_text("module.exports = {};\n", encoding="utf-8")
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path)
            == "packages/sdk/src/admin"
        )

    def test_unmatched_subpath_resolves_to_nothing(self, tmp_path: Path) -> None:
        self._package(
            tmp_path,
            _manifest("@acme/sdk", exports={"./*": "./dist/*.js"}),
            {"src/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/missing", tmp_path) is None
        )

    def test_parent_escaping_target_is_refused(self, tmp_path: Path) -> None:
        # `../shared/index.js` leaves the package; treating the `..` as part
        # of the name would bind the import to an unrelated file INSIDE it.
        self._package(
            tmp_path,
            _manifest("@acme/sdk", main="../shared/index.js"),
            {"shared/index.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert resolve_js_workspace_import(packages, "@acme/sdk", tmp_path) is None

    def test_dot_prefixed_directory_survives(self, tmp_path: Path) -> None:
        # `./.generated/index.js` names a hidden directory, not `generated`.
        self._package(
            tmp_path,
            _manifest("@acme/sdk", main="./.generated/index.js"),
            {".generated/index.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk", tmp_path)
            == "packages/sdk/.generated/index"
        )

    def test_exact_key_beats_pattern_key(self, tmp_path: Path) -> None:
        # Node resolves an exact `exports` key before any `*` pattern.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={"./a/b*": "./src/wrong.ts", "./a/b": "./src/right.ts"},
            ),
            {"src/wrong.ts": ADMIN_SOURCE, "src/right.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/a/b", tmp_path)
            == "packages/sdk/src/right"
        )

    def test_root_condition_map_resolves_the_package_root(self, tmp_path: Path) -> None:
        # `{"import": .., "require": ..}` with no "." key is a CONDITION map
        # declaring the package root, not a set of subpaths.
        # The entry is NOT at a conventional index path, so only reading the
        # condition map can resolve it.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={"import": "./dist/entry.js", "require": "./dist/entry.cjs"},
            ),
            {"src/entry.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk", tmp_path)
            == "packages/sdk/src/entry"
        )

    def test_root_condition_map_matches_only_the_root(self, tmp_path: Path) -> None:
        # The condition map declares the root, and declaring `exports` at all
        # means the package exposes nothing else, so a subpath resolves to
        # nothing rather than to a file the package does not export.
        self._package(
            tmp_path,
            _manifest("@acme/sdk", exports={"import": "./dist/index.js"}),
            {"src/index.ts": ADMIN_SOURCE, "src/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk", tmp_path)
            == "packages/sdk/src/index"
        )
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path) is None
        )

    def test_unresolvable_exact_key_does_not_fall_through_to_a_pattern(
        self, tmp_path: Path
    ) -> None:
        # An exact key wins exclusively: when it names a file this repo does
        # not hold, the subpath is unresolved rather than rebound to the
        # module a lower-precedence wildcard names.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={"./admin": "./src/missing.ts", "./*": "./src/wildcard.ts"},
            ),
            {"src/wildcard.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path) is None
        )

    def test_exports_map_supersedes_the_legacy_entry_fields(
        self, tmp_path: Path
    ) -> None:
        # A package that declares `exports` is described by it alone; Node
        # ignores `main`/`module`/`types` entirely, so a root export naming a
        # file this repo does not hold must not fall back to them.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={".": "./src/missing.ts"},
                main="./src/legacy.ts",
            ),
            {"src/legacy.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert resolve_js_workspace_import(packages, "@acme/sdk", tmp_path) is None

    def test_absolute_target_is_refused(self, tmp_path: Path) -> None:
        # An absolute target escapes the package the same way `../` does.
        self._package(
            tmp_path,
            _manifest("@acme/sdk", main="/etc/passwd.ts"),
            {"src/index.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk", tmp_path)
            == "packages/sdk/src/index"
        )

    def test_most_specific_wildcard_wins_exclusively(self, tmp_path: Path) -> None:
        # Node picks the single best-matching pattern; trying a broader one
        # after it would bind the import to a module the package maps
        # elsewhere.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={
                    "./openapi/*": "./src/generated/*.ts",
                    "./*": "./src/legacy/*.ts",
                },
            ),
            {"src/legacy/openapi/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/openapi/admin", tmp_path)
            is None
        )

    def test_import_condition_wins_over_require(self, tmp_path: Path) -> None:
        # Both condition targets exist as sources here, so the choice must be
        # the ESM one rather than whichever the mapping happened to list.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={
                    "./admin": {
                        "require": "./src/cjs/admin.ts",
                        "import": "./src/esm/admin.ts",
                    }
                },
            ),
            {"src/cjs/admin.ts": ADMIN_SOURCE, "src/esm/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path)
            == "packages/sdk/src/esm/admin"
        )

    def test_subpath_only_exports_do_not_expose_the_root(self, tmp_path: Path) -> None:
        # An `exports` map is exhaustive: a package that lists only subpaths
        # does not expose its root, so the root request resolves to nothing
        # rather than falling through to a legacy entry or an index guess.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={"./features": "./src/features.ts"},
                main="./src/index.ts",
            ),
            {"src/features.ts": ADMIN_SOURCE, "src/index.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert resolve_js_workspace_import(packages, "@acme/sdk", tmp_path) is None

    def test_root_only_exports_do_not_expose_a_subpath(self, tmp_path: Path) -> None:
        self._package(
            tmp_path,
            _manifest("@acme/sdk", exports={"import": "./src/index.ts"}),
            {"src/index.ts": ADMIN_SOURCE, "src/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path) is None
        )

    def test_require_selects_the_require_condition(self, tmp_path: Path) -> None:
        # A dual-package layout maps `import` and `require` to different
        # sources; a CommonJS `require()` must reach the CommonJS one.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={
                    "./admin": {
                        "import": "./src/esm/admin.ts",
                        "require": "./src/cjs/admin.ts",
                    }
                },
            ),
            {"src/esm/admin.ts": ADMIN_SOURCE, "src/cjs/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(
                packages, "@acme/sdk/admin", tmp_path, require=True
            )
            == "packages/sdk/src/cjs/admin"
        )
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path)
            == "packages/sdk/src/esm/admin"
        )

    def test_conditions_never_cross_the_module_system(
        self, tmp_path: Path
    ) -> None:
        # Only the CommonJS source exists here. An ESM import must resolve to
        # nothing rather than bind to the CommonJS module it does not use,
        # and the reverse holds for a require.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={
                    "./admin": {
                        "import": "./src/esm/admin.ts",
                        "require": "./src/cjs/admin.ts",
                    }
                },
            ),
            {"src/cjs/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path) is None
        )
        assert (
            resolve_js_workspace_import(
                packages, "@acme/sdk/admin", tmp_path, require=True
            )
            == "packages/sdk/src/cjs/admin"
        )

    def test_null_export_blocks_the_subpath(self, tmp_path: Path) -> None:
        # `null` is how a manifest forbids a subpath; guessing a source file
        # for it would resolve an import the package refuses to serve.
        self._package(
            tmp_path,
            _manifest(
                "@acme/sdk",
                exports={"./public": "./src/public.ts", "./internal/*": None},
            ),
            {"src/public.ts": ADMIN_SOURCE, "src/internal/secret.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/internal/secret", tmp_path)
            is None
        )

    def test_matched_export_does_not_fall_back_to_a_guess(self, tmp_path: Path) -> None:
        # The manifest says this subpath is built from src/generated; a
        # same-named legacy file elsewhere must not stand in for it.
        self._package(
            tmp_path,
            _manifest("@acme/sdk", exports={"./admin": "./dist/generated/admin.js"}),
            {"src/generated/admin.ts": ADMIN_SOURCE, "src/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk/admin", tmp_path)
            != "packages/sdk/src/admin"
        )

    def test_wrong_case_specifier_does_not_resolve(self, tmp_path: Path) -> None:
        # On a case-insensitive filesystem the probe succeeds for the wrong
        # spelling; returning it would name a module the graph never holds.
        self._package(
            tmp_path,
            _manifest("@acme/sdk"),
            {"src/AdminClient.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        resolved = resolve_js_workspace_import(
            packages, "@acme/sdk/adminclient", tmp_path
        )
        assert resolved in (None, "packages/sdk/src/AdminClient"), resolved

    def test_name_prefix_that_is_not_a_path_boundary_is_not_a_match(
        self, tmp_path: Path
    ) -> None:
        # `@acme/sdk-extras` is a different package from `@acme/sdk`; matching
        # on the bare string prefix would steal its imports.
        self._package(
            tmp_path,
            _manifest("@acme/sdk"),
            {"src/admin.ts": ADMIN_SOURCE},
        )
        packages = discover_js_workspace_packages(tmp_path)
        assert (
            resolve_js_workspace_import(packages, "@acme/sdk-extras/admin", tmp_path)
            is None
        )
