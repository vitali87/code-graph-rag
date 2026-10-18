"""A fresh updater keeps an unreadable same-stem survivor's Module (#2232).

`util.h` cannot be read when `util.c` is added beside it, so it is not
re-parsed and its graph subtree stays. A reused updater keeps the header's
claim on `proj.util` (#2223); a fresh one holds no in-memory claim, and the
graph seed skipped every flux-stem survivor, so `util.c` took the bare qn and
its MERGE moved the header's Module onto `util.c`.
"""

from __future__ import annotations

import builtins
import json
import os
import time
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_HEADER = "static inline int helper(void) { return 2; }\nint util(void);\n"
_MAIN = '#include "util.h"\nint main(void) { return util() + helper(); }\n'
_ADDED = '#include "util.h"\nint util(void) { return 1; }\n'


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.C not in parsers:
        pytest.skip("c parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _modules(store: _StatefulIngestor) -> dict[str, str]:
    return {
        str(props.get(cs.KEY_QUALIFIED_NAME)): str(props.get(cs.KEY_PATH))
        for (label, _uid), props in store.nodes.items()
        if label == cs.NodeLabel.MODULE.value
    }


# `opens`: the survivor passes the open check and fails the read that decides
# whether it is parsed, so its seeded claim must come back before `util.c` is
# parsed rather than be kept from the start.
@pytest.mark.parametrize("opens", [False, True], ids=["unopenable", "opens"])
def test_a_fresh_updater_keeps_an_unreadable_survivors_module(
    temp_repo: Path, opens: bool
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    (root / "util.h").write_text(_HEADER, encoding="utf-8")
    (root / "main.c").write_text(_MAIN, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    before = _modules(store)
    header_qn = next((qn for qn, path in before.items() if path == "util.h"), None)
    assert header_qn is not None, before

    (root / "util.c").write_text(_ADDED, encoding="utf-8")
    header = root / "util.h"
    os.utime(header, (time.time() + 5, time.time() + 5))
    real_open, real_read_bytes = builtins.open, Path.read_bytes

    def denied(path: object) -> None:
        if Path(str(path)) == header:
            raise PermissionError(13, "Permission denied", str(path))

    def open_denied(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        denied(file)
        return real_open(file, *args, **kwargs)

    def read_bytes_denied(self: Path) -> bytes:
        denied(self)
        return real_read_bytes(self)

    with (
        patch("codebase_rag.graph_updater._opens_for_reading", return_value=True)
        if opens
        else nullcontext(),
        patch("builtins.open", open_denied),
        patch.object(Path, "read_bytes", read_bytes_denied),
    ):
        _index(store, root, force=False)

    after = _modules(store)
    assert after.get(header_qn) == "util.h", after
    assert "util.c" in after.values(), after


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "reused"])
def test_an_unchanged_survivor_whose_reparse_read_fails_keeps_its_module(
    temp_repo: Path, fresh: bool
) -> None:
    # An UNCHANGED survivor skips the hash pass on its mtime and is read only
    # when the flux-stem re-parse queues it, after the claims were restored;
    # a failure there left it unparsed with its subtree in place and no
    # claim, so `util.c` took the bare qn. It must keep its claim and be
    # marked for retry (CodeRabbit, PR #2248).
    root = temp_repo / "proj"
    root.mkdir()
    (root / "util.h").write_text(_HEADER, encoding="utf-8")
    (root / "main.c").write_text(_MAIN, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.C not in parsers:
        pytest.skip("c parser not available")
    store = _StatefulIngestor()

    def updater() -> GraphUpdater:
        return GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="proj",
        )

    first = updater()
    first.run(force=True)
    before = _modules(store)
    header_qn = next((qn for qn, path in before.items() if path == "util.h"), None)
    assert header_qn is not None, before

    (root / "util.c").write_text(_ADDED, encoding="utf-8")
    header = root / "util.h"
    real_read_bytes = Path.read_bytes

    def read_bytes_denied(self: Path) -> bytes:
        if self == header:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    with patch.object(Path, "read_bytes", read_bytes_denied):
        (updater() if fresh else first).run(force=False)

    after = _modules(store)
    assert after.get(header_qn) == "util.h", after
    assert "util.c" in after.values(), after
    cache = json.loads(
        state_file(root, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
    )
    assert cache.get("util.h") == cs.HASH_CACHE_UNREADABLE, cache


def test_an_unchanged_unopenable_survivor_is_marked_for_retry(
    temp_repo: Path,
) -> None:
    # Unopenable, the survivor keeps its seeded claim and records none to
    # restore; its failed re-parse read must still mark it for retry, or its
    # unchanged hash lets the next run skip it for good (CodeRabbit, PR
    # #2248).
    root = temp_repo / "proj"
    root.mkdir()
    (root / "util.h").write_text(_HEADER, encoding="utf-8")
    (root / "main.c").write_text(_MAIN, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    (root / "util.c").write_text(_ADDED, encoding="utf-8")
    header = root / "util.h"
    real_open, real_read_bytes = builtins.open, Path.read_bytes

    def denied(path: object) -> None:
        if Path(str(path)) == header:
            raise PermissionError(13, "Permission denied", str(path))

    def open_denied(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        denied(file)
        return real_open(file, *args, **kwargs)

    def read_bytes_denied(self: Path) -> bytes:
        denied(self)
        return real_read_bytes(self)

    with (
        patch("builtins.open", open_denied),
        patch.object(Path, "read_bytes", read_bytes_denied),
    ):
        _index(store, root, force=False)

    cache = json.loads(
        state_file(root, cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
    )
    assert cache.get("util.h") == cs.HASH_CACHE_UNREADABLE, cache
