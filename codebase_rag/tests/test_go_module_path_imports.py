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
    # Build the graph for `files` and return (caller_qn, rel_type, callee_qn).
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
    # Two packages named `util` both define Greet; the caller imports beta's.
    # The raw go.mod-prefixed import path must map to the project qn, or the
    # trie fallback picks a sibling alphabetically (alpha) and misbinds.
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
    # An aliased import (`import u ".../beta/util"`) severs even the accidental
    # package-name suffix match, so only the module-path mapping can bind it.
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
    # A Go package spans files whose names are irrelevant to the language
    # (util/helpers.go, package util); the member lookup must search the
    # package's file modules instead of requiring file == dir name.
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
    # A monorepo submodule (services/api/go.mod, module github.com/acme/api)
    # anchors its module path at services/api, not the repo root.
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
    # go.mod allows a trailing comment on the module directive, including the
    # official same-line `// Deprecated:` form; the directive parse must strip
    # it or every import in the module stays raw and unresolved.
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
    # The unambiguous shape that already resolved (via the trie) must keep
    # resolving through the precise import path.
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
    # Importing the module PATH itself binds the module's root package, whose
    # mapped qn is exactly the project name (no dot suffix); the member lookup
    # must accept it, not demand a project-dot prefix.
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
    # read_text raises UnicodeDecodeError (a ValueError, not OSError) on
    # invalid UTF-8; discovery must skip such a go.mod, not crash indexing.
    from codebase_rag.parsers.go import discover_go_module_paths

    (tmp_path / "go.mod").write_bytes(b"module github.com/acme/mytool\xff\xfe\n")
    assert discover_go_module_paths(tmp_path) == []


def test_external_import_does_not_misbind_to_local_same_name(tmp_path: Path) -> None:
    # assert.Equal comes from an EXTERNAL module (not under any local go.mod
    # module path); the unindexed call must be dropped, not rebound by bare
    # name to an unrelated first-party Equal.
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
    # The IMPORTS edge for a first-party Go import must target a
    # project-prefixed qn of a REAL module node, not the dangling raw
    # module path and not the package directory (which has no Module
    # node); deferred verification resolves the dir qn to its file.
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


def test_duplicate_module_directive_resolves_to_dir_with_package(
    tmp_path: Path,
) -> None:
    # Two go.mod files declare the SAME module path: a dependency-pinning
    # stub (holding only a placeholder main.go) and the real code tree. The
    # import must resolve into the tree that contains the imported package,
    # regardless of discovery order.
    files = {
        "go.mod": GO_MOD,
        "a_stub/go.mod": "module github.com/acme/mytool/gen\n\ngo 1.22\n",
        "a_stub/main.go": "package main\n\nfunc main() {}\n",
        # An unrelated same-named package: if the import misresolves to the
        # stub, the name fallback rebinds here instead of the imported one.
        "alpha/util/util.go": GREET_ALPHA,
        "gen/go.mod": "module github.com/acme/mytool/gen\n\ngo 1.22\n",
        "gen/util/util.go": GREET_BETA,
        "main.go": (
            "package main\n\n"
            'import "github.com/acme/mytool/gen/util"\n\n'
            "func main() {\n"
            "    util.Greet()\n"
            "}\n"
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.gen.util.util.Greet" in calls, calls
    assert "mytool.alpha.util.util.Greet" not in calls, calls


def test_duplicate_module_paths_resolver_prefers_existing_package_dir(
    tmp_path: Path,
) -> None:
    # Unit-level determinism: the stub mapping sorts first, but only the real
    # tree contains the imported package directory.
    from codebase_rag.parsers.go import (
        discover_go_module_paths,
        resolve_go_import_path,
    )

    (tmp_path / "a_stub").mkdir()
    (tmp_path / "a_stub" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "real" / "util").mkdir(parents=True)
    (tmp_path / "real" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "real" / "util" / "util.go").write_text(GREET_BETA, encoding="utf-8")
    mappings = discover_go_module_paths(tmp_path)
    resolved = resolve_go_import_path(mappings, "github.com/acme/mytool/gen/util")
    assert resolved == "real.util", (mappings, resolved)


def test_duplicate_module_root_import_resolver_unit(tmp_path: Path) -> None:
    # The import names the module PATH itself. Both duplicate anchors exist
    # on disk, so directory existence cannot discriminate; the stub holds
    # only `package main`, which cannot be imported, and must lose to the
    # anchor with an importable root package.
    from codebase_rag.parsers.go import (
        discover_go_module_paths,
        resolve_go_import_path,
    )

    (tmp_path / "a_stub").mkdir()
    (tmp_path / "a_stub" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "a_stub" / "main.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8"
    )
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "gen" / "gen.go").write_text(
        'package gen\n\nfunc Version() string {\n\treturn "1"\n}\n',
        encoding="utf-8",
    )
    mappings = discover_go_module_paths(tmp_path)
    resolved = resolve_go_import_path(mappings, "github.com/acme/mytool/gen")
    assert resolved == "gen", (mappings, resolved)


def test_duplicate_module_root_import_prefers_importable_package(
    tmp_path: Path,
) -> None:
    # Pipeline-level regression of the same shape.
    files = {
        "go.mod": GO_MOD,
        "a_stub/go.mod": "module github.com/acme/mytool/gen\n\ngo 1.22\n",
        "a_stub/main.go": "package main\n\nfunc main() {}\n",
        "gen/go.mod": "module github.com/acme/mytool/gen\n\ngo 1.22\n",
        "gen/gen.go": 'package gen\n\nfunc Version() string {\n\treturn "1"\n}\n',
        # Decoy for the name fallback if the import misresolves.
        "meta/meta.go": 'package meta\n\nfunc Version() string {\n\treturn "x"\n}\n',
        "main.go": (
            "package main\n\n"
            'import gen "github.com/acme/mytool/gen"\n\n'
            "func main() {\n"
            "    gen.Version()\n"
            "}\n"
        ),
    }
    calls = _calls(_run_rels(tmp_path, files), "main.main")
    assert "mytool.gen.gen.Version" in calls, calls
    assert "mytool.meta.meta.Version" not in calls, calls


def test_block_comment_package_text_does_not_qualify_stub(tmp_path: Path) -> None:
    # The stub's only file is `package main` preceded by a block comment
    # containing the words `package docs`; comment text is not a clause.
    from codebase_rag.parsers.go import (
        discover_go_module_paths,
        resolve_go_import_path,
    )

    (tmp_path / "a_stub").mkdir()
    (tmp_path / "a_stub" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "a_stub" / "main.go").write_text(
        "/*\npackage docs describes why this stub exists.\n*/\n"
        "package main\n\nfunc main() {}\n",
        encoding="utf-8",
    )
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "gen" / "gen.go").write_text(
        'package gen\n\nfunc Version() string {\n\treturn "1"\n}\n',
        encoding="utf-8",
    )
    mappings = discover_go_module_paths(tmp_path)
    resolved = resolve_go_import_path(mappings, "github.com/acme/mytool/gen")
    assert resolved == "gen", (mappings, resolved)


def _write_duplicate_module_pair(
    tmp_path: Path, stub_main_go: str, real_gen_go: str
) -> None:
    (tmp_path / "a_stub").mkdir()
    (tmp_path / "a_stub" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "a_stub" / "main.go").write_text(stub_main_go, encoding="utf-8")
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "go.mod").write_text(
        "module github.com/acme/mytool/gen\n", encoding="utf-8"
    )
    (tmp_path / "gen" / "gen.go").write_text(real_gen_go, encoding="utf-8")


def test_semicolon_package_clause_still_counts_as_main(tmp_path: Path) -> None:
    # `package main;` is a valid clause form; the trailing semicolon must not
    # make the stub look importable.
    from codebase_rag.parsers.go import (
        discover_go_module_paths,
        resolve_go_import_path,
    )

    _write_duplicate_module_pair(
        tmp_path,
        "package main;\n\nfunc main() {}\n",
        'package gen\n\nfunc Version() string {\n\treturn "1"\n}\n',
    )
    mappings = discover_go_module_paths(tmp_path)
    resolved = resolve_go_import_path(mappings, "github.com/acme/mytool/gen")
    assert resolved == "gen", (mappings, resolved)


def test_inline_block_comment_keeps_clause_tokens_apart(tmp_path: Path) -> None:
    # `package/*doc*/gen` must still read as a `gen` clause: removing the
    # comment may not glue the tokens together, or the real tree looks
    # unimportable and the stub wins.
    from codebase_rag.parsers.go import (
        discover_go_module_paths,
        resolve_go_import_path,
    )

    _write_duplicate_module_pair(
        tmp_path,
        "package main\n\nfunc main() {}\n",
        'package/*doc*/gen\n\nfunc Version() string {\n\treturn "1"\n}\n',
    )
    mappings = discover_go_module_paths(tmp_path)
    resolved = resolve_go_import_path(mappings, "github.com/acme/mytool/gen")
    assert resolved == "gen", (mappings, resolved)
