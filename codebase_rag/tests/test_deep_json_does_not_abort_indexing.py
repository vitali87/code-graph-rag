"""A deeply nested repository JSON file is skipped, not fatal (#2261).

The stdlib decoder raises `RecursionError` on deep nesting, which the
manifest readers' `ValueError` handling did not catch: one hostile
`package.json` or `tsconfig.json` aborted the whole index run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.graph_updater import (
    _EMPTY_DELOMBOK_STATE,
    GraphUpdater,
    _load_delombok_state,
    _load_dir_mtimes,
    _load_exclusion_state,
    _load_hash_cache,
    _load_project_stamps,
)
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.json_io import loads_json
from evals.cgr_graph import _StatefulIngestor

_DEPTH = 100_000
_DEEP = '{"name":"x","a":' + "[" * _DEPTH + "]" * _DEPTH + "}"


def test_too_deep_json_reads_as_malformed() -> None:
    with pytest.raises(json.JSONDecodeError, match=ex.JSON_TOO_DEEP):
        loads_json(_DEEP)


def test_ordinary_json_still_decodes() -> None:
    assert loads_json('{"a": [1, {"b": null}]}') == {"a": [1, {"b": None}]}


@pytest.mark.parametrize(
    "manifest",
    [
        cs.DEP_FILE_PACKAGE_JSON,
        "tsconfig.json",
        "jsconfig.json",
        f"web/{cs.DEP_FILE_PACKAGE_JSON}",
        "web/tsconfig.json",
    ],
)
def test_one_deep_manifest_does_not_stop_the_run(
    temp_repo: Path, manifest: str, _fail_on_swallowed_pass_errors: list[str]
) -> None:
    repo = temp_repo / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (repo / "web").mkdir()
    (repo / "web" / "index.ts").write_text(
        "import { x } from './y';\nexport const a = 1;\n", encoding="utf-8"
    )
    (repo / manifest).write_text(_DEEP, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="repo",
    ).run(force=True)

    names = {props.get(cs.KEY_QUALIFIED_NAME) for props in store.nodes.values()}
    assert "repo.app.main.f" in names
    # The dependency pass reports an unreadable package.json as a skipped
    # file, which is the outcome wanted here; nothing else may have failed.
    failures = _fail_on_swallowed_pass_errors
    assert all(
        f.startswith("Error parsing package.json") and manifest in f.replace("\\", "/")
        for f in failures
    ), failures
    failures.clear()


def test_deep_state_files_read_as_absent(temp_repo: Path) -> None:
    cache = temp_repo / cs.HASH_CACHE_FILENAME
    cache.write_text(_DEEP, encoding="utf-8")
    stamp = temp_repo / cs.EXCLUSION_STATE_FILENAME
    stamp.write_text(_DEEP, encoding="utf-8")

    assert _load_hash_cache(cache) == {}
    assert _load_dir_mtimes(cache) == {}
    assert _load_exclusion_state(stamp) is None
    assert _load_project_stamps(stamp) == {}
    assert _load_delombok_state(stamp) == _EMPTY_DELOMBOK_STATE
