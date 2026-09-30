"""Issue #2406: git-tracked source under a default-excluded name is indexed.

The default exclusions match a directory NAME at any depth, and several of
the names commonly hold first-party, committed source: Dart's `bin/main.dart`
entry point, a Go package `pkg/out`, a JS config module `src/env`. Their files
never reached the graph, with no notice naming them. A directory with one of
those ambiguous names is now kept when git tracks files in it; untracked or
ignored build output under the same names is still skipped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.config import CGRIGNORE_FILENAME, load_ignore_patterns
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import git_env
from codebase_rag.utils.path_utils import is_eligible_rel_file
from evals.cgr_graph import _StatefulIngestor

TRACKED = {
    "bin/main.dart": 'import "package:defex/defex.dart";\nvoid main() { print(greet()); }\n',
    "lib/defex.dart": 'String greet() => "hi";\n',
    "pubspec.yaml": "name: defex\n",
    "src/app.js": "import { loadEnv } from './env/index.js';\nloadEnv();\n",
    "src/env/index.js": "export function loadEnv() { return 1; }\n",
    "pkg/out/out.go": "package out\nfunc Print(s string) string { return s }\n",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=git_env(),
    )


@pytest.fixture
def defex(git_repo: Path) -> Path:
    for rel, text in TRACKED.items():
        (git_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (git_repo / rel).write_text(text)
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-q", "-m", "init")
    return git_repo


def _indexed_paths(repo: Path) -> set[str]:
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    patterns = load_ignore_patterns(repo)
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="defex",
        unignore_paths=patterns.unignore or None,
        exclude_paths=patterns.exclude or None,
    ).run(force=True)
    return {
        str(props.get(cs.KEY_PATH))
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.FILE
    }


def test_tracked_source_under_ambiguous_names_is_indexed(defex: Path) -> None:
    indexed = _indexed_paths(defex)

    assert {"bin/main.dart", "src/env/index.js", "pkg/out/out.go"} <= indexed


def test_the_kept_directories_are_named_in_the_log(defex: Path) -> None:
    messages: list[str] = []
    sink = logger.add(messages.append, level="INFO", format="{message}")
    try:
        load_ignore_patterns(defex)
    finally:
        logger.remove(sink)

    kept = [m for m in messages if "bin" in m and "src/env" in m and "pkg/out" in m]
    assert kept, messages


def test_untracked_build_output_under_the_same_names_stays_excluded(
    defex: Path,
) -> None:
    # Negative: `out/` at the root and a nested `tools/bin/` hold only
    # untracked output; the rescue is anchored to the tracked directories.
    for rel in ("out/gen.js", "tools/bin/tool.js", "target/debug/x.js"):
        (defex / rel).parent.mkdir(parents=True, exist_ok=True)
        (defex / rel).write_text("export const X = 1;\n")

    indexed = _indexed_paths(defex)

    assert not {"out/gen.js", "tools/bin/tool.js", "target/debug/x.js"} & indexed
    assert "bin/main.dart" in indexed


def test_a_committed_vendor_directory_stays_excluded(defex: Path) -> None:
    # Negative: only names that commonly hold first-party source are
    # rescued; committed vendored or generated trees are still third-party.
    (defex / "vendor" / "lib.js").parent.mkdir(parents=True)
    (defex / "vendor" / "lib.js").write_text("export const V = 1;\n")
    _git(defex, "add", "-A")
    _git(defex, "commit", "-q", "-m", "vendor")

    assert "vendor/lib.js" not in _indexed_paths(defex)


def test_an_explicit_exclude_still_wins(defex: Path) -> None:
    # Negative: a user who excludes `bin` keeps it excluded.
    (defex / CGRIGNORE_FILENAME).write_text("bin\n")

    assert "bin/main.dart" not in _indexed_paths(defex)


def test_outside_git_the_defaults_apply_as_before(tmp_path: Path) -> None:
    # Negative: with no repository to ask, nothing is rescued.
    repo = tmp_path / "plain"
    for rel, text in TRACKED.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)

    assert load_ignore_patterns(repo).unignore == frozenset()
    assert "bin/main.dart" not in _indexed_paths(repo)


def test_an_untracked_file_beside_tracked_source_stays_excluded(defex: Path) -> None:
    # The rescue is for what git tracks, not for the whole directory: build
    # output written next to `bin/main.dart` is still output (review of
    # PR 2490).
    for rel in ("bin/generated.js", "src/env/local.js"):
        (defex / rel).write_text("export const X = 1;\n")

    indexed = _indexed_paths(defex)

    assert not {"bin/generated.js", "src/env/local.js"} & indexed
    assert {"bin/main.dart", "src/env/index.js"} <= indexed


def test_the_watcher_draws_the_same_line(defex: Path) -> None:
    # The watcher decides one path at a time with the same predicate.
    patterns = load_ignore_patterns(defex)

    def eligible(rel: str) -> bool:
        return is_eligible_rel_file(
            rel, patterns.exclude or None, patterns.unignore or None
        )

    assert eligible("bin/main.dart")
    assert not eligible("bin/generated.js")


def _commit(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")


@pytest.mark.parametrize(
    "tracked", ["vendor/bin/lib.js", "node_modules/pkg/bin/cli.js", "bin/vendor/x.js"]
)
def test_a_tracked_file_under_another_default_exclusion_stays_excluded(
    defex: Path, tracked: str
) -> None:
    # Review of PR 2490: the rescue looked only for an ambiguous name, so a
    # committed `vendor/bin/lib.js` came in through `vendor`, which is
    # excluded whatever git tracks.
    _commit(defex, {tracked: "export const x = 1;\n"})

    indexed = _indexed_paths(defex)

    assert tracked not in indexed
    assert "bin/main.dart" in indexed


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows file names cannot hold * or ?"
)
@pytest.mark.parametrize("name", ["*.js", "?.js", "[a-z].js"])
def test_a_tracked_name_with_glob_characters_rescues_nothing_else(
    defex: Path, name: str
) -> None:
    # Review of PR 2490: the tracked path became a pattern as written, so a
    # tracked `bin/*.js` let every untracked `bin/<name>.js` in too.
    _commit(defex, {f"bin/{name}": "export const x = 1;\n"})
    (defex / "bin" / "q.js").write_text("export const leaked = 1;\n")

    indexed = _indexed_paths(defex)

    assert "bin/q.js" not in indexed
    assert "bin/main.dart" in indexed
