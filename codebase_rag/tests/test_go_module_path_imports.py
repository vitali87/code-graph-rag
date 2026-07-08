from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

GO_MOD = "module github.com/acme/mytool\n\ngo 1.22\n"
GREET_ALPHA = 'package util\n\nfunc Greet() string {\n    return "alpha"\n}\n'
GREET_BETA = 'package util\n\nfunc Greet() string {\n    return "beta"\n}\n'


def _run_rels(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str, str]]:
    # (H) Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
    parsers, queries = load_parsers()
    if "go" not in parsers:
        pytest.skip("go parser not available")
    root = tmp_path / "mytool"
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


def test_import_binds_to_imported_package_not_same_named_sibling(
    tmp_path: Path,
) -> None:
    # (H) Two packages named `util` both define Greet; the caller imports beta's.
    # (H) The raw go.mod-prefixed import path must map to the project qn, or the
    # (H) trie fallback picks a sibling alphabetically (alpha) and misbinds.
    files = {
        "go.mod": GO_MOD,
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/beta/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
        "alpha/util/util.go": GREET_ALPHA,
        "beta/util/util.go": GREET_BETA,
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.beta.util.util.Greet" in calls
    assert "mytool.alpha.util.util.Greet" not in calls


def test_aliased_import_binds_to_imported_package(tmp_path: Path) -> None:
    # (H) An aliased import (`import u ".../beta/util"`) severs even the accidental
    # (H) package-name suffix match, so only the module-path mapping can bind it.
    files = {
        "go.mod": GO_MOD,
        "main.go": (
            "package main\n\n"
            'import u "github.com/acme/mytool/beta/util"\n\n'
            "func main() {\n"
            "    u.Greet()\n"
            "}\n"
        ),
        "alpha/util/util.go": GREET_ALPHA,
        "beta/util/util.go": GREET_BETA,
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.beta.util.util.Greet" in calls
    assert "mytool.alpha.util.util.Greet" not in calls


def test_package_file_named_differently_resolves_precisely(tmp_path: Path) -> None:
    # (H) A Go package spans files whose names are irrelevant to the language
    # (H) (util/helpers.go, package util); the member lookup must search the
    # (H) package's file modules instead of requiring file == dir name.
    files = {
        "go.mod": GO_MOD,
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/beta/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
        "alpha/util/util.go": GREET_ALPHA,
        "beta/util/helpers.go": GREET_BETA,
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.beta.util.helpers.Greet" in calls
    assert "mytool.alpha.util.util.Greet" not in calls


def test_nested_gomod_module_maps_to_its_directory(tmp_path: Path) -> None:
    # (H) A monorepo submodule (services/api/go.mod, module github.com/acme/api)
    # (H) anchors its module path at services/api, not the repo root.
    files = {
        "go.mod": GO_MOD,
        "services/api/go.mod": "module github.com/acme/api\n\ngo 1.22\n",
        "services/api/main.go": (
            "package main\n\n"
            'import "github.com/acme/api/handlers"\n\n'
            "func main() {\n"
            "    handlers.Handle()\n"
            "}\n"
        ),
        "services/api/handlers/handlers.go": (
            'package handlers\n\nfunc Handle() string {\n    return "ok"\n}\n'
        ),
        "handlers/handlers.go": (
            'package handlers\n\nfunc Handle() string {\n    return "decoy"\n}\n'
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "services.api.main.main")
    assert "mytool.services.api.handlers.handlers.Handle" in calls
    assert "mytool.handlers.handlers.Handle" not in calls


def test_module_directive_with_trailing_comment_still_maps(tmp_path: Path) -> None:
    # (H) go.mod allows a trailing comment on the module directive, including the
    # (H) official same-line `// Deprecated:` form; the directive parse must strip
    # (H) it or every import in the module stays raw and unresolved.
    files = {
        "go.mod": (
            "module github.com/acme/mytool // Deprecated: use mytool/v2\n\ngo 1.22\n"
        ),
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/beta/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
        "alpha/util/util.go": GREET_ALPHA,
        "beta/util/util.go": GREET_BETA,
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.beta.util.util.Greet" in calls
    assert "mytool.alpha.util.util.Greet" not in calls


def test_flat_single_package_regression(tmp_path: Path) -> None:
    # (H) The unambiguous shape that already resolved (via the trie) must keep
    # (H) resolving through the precise import path.
    files = {
        "go.mod": GO_MOD,
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
        "util/util.go": 'package util\n\nfunc Greet() string {\n    return "hi"\n}\n',
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.util.util.Greet" in calls


def test_module_root_package_member_resolves(tmp_path: Path) -> None:
    # (H) Importing the module PATH itself binds the module's root package, whose
    # (H) mapped qn is exactly the project name (no dot suffix); the member lookup
    # (H) must accept it, not demand a project-dot prefix.
    files = {
        "go.mod": GO_MOD,
        "version.go": (
            'package mytool\n\nfunc Version() string {\n    return "1"\n}\n'
        ),
        "cmd/main.go": (
            "package main\n\n"
            'import tool "github.com/acme/mytool"\n\n'
            "func main() {\n"
            "    tool.Version()\n"
            "}\n"
        ),
        "internal/meta/meta.go": (
            'package meta\n\nfunc Version() string {\n    return "decoy"\n}\n'
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "cmd.main.main")
    assert "mytool.version.Version" in calls
    assert "mytool.internal.meta.meta.Version" not in calls


def test_invalid_utf8_gomod_does_not_crash(tmp_path: Path) -> None:
    # (H) read_text raises UnicodeDecodeError (a ValueError, not OSError) on
    # (H) invalid UTF-8; discovery must skip such a go.mod, not crash indexing.
    from codebase_rag.parsers.go import discover_go_module_paths

    (tmp_path / "go.mod").write_bytes(b"module github.com/acme/mytool\xff\xfe\n")
    assert discover_go_module_paths(tmp_path) == []


def test_external_import_does_not_misbind_to_local_same_name(tmp_path: Path) -> None:
    # (H) assert.Equal comes from an EXTERNAL module (not under any local go.mod
    # (H) module path); the unindexed call must be dropped, not rebound by bare
    # (H) name to an unrelated first-party Equal.
    files = {
        "go.mod": GO_MOD,
        "main_test.go": (
            "package main\n\n"
            'import "github.com/stretchr/testify/assert"\n\n'
            "func TestX(t *T) {\n"
            "    assert.Equal(t, 1, 1)\n"
            "}\n"
        ),
        "compare/compare.go": (
            "package compare\n\nfunc Equal(a int, b int) bool {\n    return a == b\n}\n"
        ),
    }
    rels = _run_rels(tmp_path, files)
    assert not any(
        r == "CALLS" and b.endswith("compare.compare.Equal") for _, r, b in rels
    )


def test_local_import_edge_targets_project_qn(tmp_path: Path) -> None:
    # (H) The IMPORTS edge for a first-party Go import must target a
    # (H) project-prefixed qn of a REAL module node, not the dangling raw
    # (H) module path and not the package directory (which has no Module
    # (H) node); deferred verification resolves the dir qn to its file.
    files = {
        "go.mod": GO_MOD,
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
        "util/util.go": 'package util\n\nfunc Greet() string {\n    return "hi"\n}\n',
    }
    rels = _run_rels(tmp_path, files)
    import_targets = {b for a, r, b in rels if r == "IMPORTS" and a.endswith("main")}
    assert "mytool.util.util" in import_targets
    assert not any(t.startswith("github.com/") for t in import_targets)
