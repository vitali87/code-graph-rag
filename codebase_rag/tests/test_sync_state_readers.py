"""The hash cache and folder-time readers on files they cannot use.

A sync reads both files before anything else. One it cannot use must read
as no cache, so the run rebuilds what it would otherwise have trusted, never
end the run: a cache that is not UTF-8, or that holds an integer Python will
not convert, raised out of `_load_hash_cache`, and a folder time too large
for a float out of `_load_dir_mtimes`. A name holding a byte that is not
UTF-8, which a cache written before #3722 can spell as a `\\udcXX` escape,
reached the graph's writes and ended the sync; the readers drop such
entries and keep the rest. A well-named file whose hash is unusable keeps
its name, marked unreadable, so the next pass re-reads it as a known file
(docs/reports/PHASE2_FRAME.md, section 6).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.graph_updater import (
    GraphUpdater,
    _load_dir_mtimes,
    _load_hash_cache,
)
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

# Past `sys.get_int_max_str_digits()`'s default, so `int()` refuses it.
TOO_MANY_DIGITS = "1" * 5000
# An integer no float holds: `float()` overflows on it.
TOO_LARGE_FOR_A_FLOAT = "1" + "0" * 400
OLD_NAME = "pkg/bad\udcff.py"
OLD_DIR = "bad\udcfe"


def _warnings(run: object) -> list[str]:
    warned: list[str] = []
    sink = logger.add(lambda message: warned.append(str(message)), level="WARNING")
    try:
        assert callable(run)
        run()
    finally:
        logger.remove(sink)
    return warned


class TestHashCache:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b'\xff\xfe{"a.py": "x"}', id="not-utf-8"),
            pytest.param(
                f'{{"a.py": "x", "n": {TOO_MANY_DIGITS}}}'.encode(),
                id="integer-past-the-digit-limit",
            ),
        ],
    )
    def test_a_cache_it_cannot_decode_reads_as_none_with_a_warning(
        self, tmp_path: Path, payload: bytes
    ) -> None:
        cache = tmp_path / cs.HASH_CACHE_FILENAME
        cache.write_bytes(payload)
        read: list[dict[str, str]] = []

        warned = _warnings(lambda: read.append(_load_hash_cache(cache)))

        assert read == [{}]
        assert len(warned) == 1
        assert str(cache) in warned[0]

    def test_a_name_with_a_byte_that_is_not_utf8_is_dropped_and_a_bad_hash_kept_unread(
        self, tmp_path: Path
    ) -> None:
        # A file whose name is fine but whose hash is not keeps its name,
        # marked as a file the last run could not read: dropping it would hide
        # an edit to it from the in-sync check and parse it as a new file,
        # leaving its old definitions in the graph.
        cache = tmp_path / cs.HASH_CACHE_FILENAME
        cache.write_text(
            json.dumps(
                {
                    "a.py": "1" * 32,
                    OLD_NAME: "2" * 32,
                    "b.py": "bad\udcff",
                    "c.py": None,
                }
            ),
            encoding=cs.ENCODING_UTF8,
        )

        assert _load_hash_cache(cache) == {
            "a.py": "1" * 32,
            "b.py": cs.HASH_CACHE_UNREADABLE,
            "c.py": cs.HASH_CACHE_UNREADABLE,
        }

    def test_a_sync_over_a_cache_holding_such_a_name_completes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (repo / "pkg" / "b.py").write_text("x = 1\n", encoding="utf-8")
        graph = tmp_path / "graph.sqlite"
        parsers, queries = load_parsers()

        def sync() -> GraphUpdater:
            with MemgraphIngestor(
                host="unused",
                port=0,
                dialect=get_dialect(DIALECT_SQLITE),
                sqlite_path=str(graph),
            ) as ingestor:
                ingestor.ensure_constraints()
                updater = GraphUpdater(
                    ingestor, repo, parsers, queries, skip_embeddings=True
                )
                updater.run()
                return updater

        sync()
        cache = state_file(repo, cs.HASH_CACHE_FILENAME)
        recorded = json.loads(cache.read_text(encoding=cs.ENCODING_UTF8))
        stamp = cache.stat()
        cache.write_text(
            json.dumps({**recorded, OLD_NAME: "0" * 32}, indent=2),
            encoding=cs.ENCODING_UTF8,
        )
        os.utime(cache, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

        updater = sync()

        # Nothing changed on disk but the dropped name, so the run is in sync.
        assert updater.skipped_because_in_sync is True
        assert json.loads(cache.read_text(encoding=cs.ENCODING_UTF8)) == {
            **recorded,
            OLD_NAME: "0" * 32,
        }

    def test_an_edit_to_a_file_whose_cached_hash_is_unusable_is_synced(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        edited = repo / "b.py"
        edited.write_text("def old():\n    return 1\n", encoding="utf-8")
        graph = tmp_path / "graph.sqlite"
        parsers, queries = load_parsers()

        def sync() -> GraphUpdater:
            with MemgraphIngestor(
                host="unused",
                port=0,
                dialect=get_dialect(DIALECT_SQLITE),
                sqlite_path=str(graph),
            ) as ingestor:
                ingestor.ensure_constraints()
                updater = GraphUpdater(
                    ingestor, repo, parsers, queries, skip_embeddings=True
                )
                updater.run()
                return updater

        sync()
        cache = state_file(repo, cs.HASH_CACHE_FILENAME)
        recorded = json.loads(cache.read_text(encoding=cs.ENCODING_UTF8))
        stamp = cache.stat()
        cache.write_text(
            json.dumps({**recorded, "b.py": "bad\udcff"}, indent=2),
            encoding=cs.ENCODING_UTF8,
        )
        os.utime(cache, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        # Edited in place: the folder's own time does not move, so only the
        # file's cache entry can tell the in-sync check about the edit.
        folder = repo.stat()
        edited.write_text("def new():\n    return 2\n", encoding="utf-8")
        os.utime(repo, ns=(folder.st_atime_ns, folder.st_mtime_ns))
        future = stamp.st_mtime_ns + 10_000_000_000
        os.utime(edited, ns=(future, future))

        updater = sync()

        assert updater.skipped_because_in_sync is False
        assert (
            json.loads(cache.read_text(encoding=cs.ENCODING_UTF8))["b.py"]
            == hashlib.md5(edited.read_bytes()).hexdigest()
        )


class TestFolderTimes:
    def test_a_time_too_large_for_a_float_reads_as_none(self, tmp_path: Path) -> None:
        cache = tmp_path / cs.DIR_MTIMES_FILENAME
        cache.write_text(
            f'{{".": 1.5, "src": {TOO_LARGE_FOR_A_FLOAT}}}', encoding="utf-8"
        )

        assert _load_dir_mtimes(cache) == {}

    def test_a_folder_named_with_a_byte_that_is_not_utf8_is_dropped(
        self, tmp_path: Path
    ) -> None:
        cache = tmp_path / cs.DIR_MTIMES_FILENAME
        cache.write_text(
            json.dumps({".": 1.5, OLD_DIR: 2.5, "src": 3.5}), encoding="utf-8"
        )

        assert _load_dir_mtimes(cache) == {".": 1.5, "src": 3.5}
