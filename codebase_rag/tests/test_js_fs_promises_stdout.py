"""Node's promise-based fs API and process.stdout/stderr are I/O.

The JS/TS registry knew only the callback and sync `fs` functions and
`console.*`, so `readFile` from `node:fs/promises`, `fs.promises.readFile`
and `process.stdout.write` recorded no READS_FROM / WRITES_TO, and no flow
reached them (issue #2775).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_CONFIG = "resource::FILE::/etc/app/config.json"
_STATE = "resource::FILE::/var/app/state.json"
_STDOUT = "resource::STDOUT::<dynamic>"
_STDERR = "resource::STDERR::<dynamic>"

_STORE = """\
import fs from "node:fs";
import { readFile, writeFile, appendFile, rm } from "node:fs/promises";
import * as fsp from "fs/promises";

export function loadSync() { return fs.readFileSync("/etc/app/config.json", "utf8"); }
export async function loadAsync() { return await readFile("/etc/app/config.json", "utf8"); }
export async function loadViaFsPromises() { return await fs.promises.readFile("/etc/app/config.json", "utf8"); }
export async function loadViaNamespace() { return await fsp.readFile("/etc/app/config.json"); }
export async function save(data) { await writeFile("/var/app/state.json", data); }
export async function log(line) { await appendFile("/var/app/state.json", line); }
export async function wipe() { await rm("/var/app/state.json"); }
export async function openForRead() { return await fsp.open("/etc/app/config.json"); }
export async function openForWrite() { return await fsp.open("/var/app/state.json", "w"); }
export function report(line) { process.stdout.write(line + "\\n"); }
export function complain(line) { process.stderr.write(line + "\\n"); }
export async function echoConfig() { process.stdout.write(await readFile("/etc/app/config.json")); }
"""

_COMMONJS = """\
const fs = require("fs");
const { readFile } = require("fs/promises");

async function loadCjs() { return await readFile("/etc/app/config.json"); }
async function loadCjsMember() { return await fs.promises.readFile("/etc/app/config.json"); }

module.exports = { loadCjs, loadCjsMember };
"""

# Look-alikes: a parameter named `process`, a local `readFile` shadowing the
# imported one, and a stream that is not the process's.
_LOOK_ALIKES = """\
import { readFile } from "node:fs/promises";

export function shadowed(process, line) { process.stdout.write(line); }
export async function local(line) {
  const readFile = async (p) => p;
  return await readFile("/etc/app/config.json");
}
export function stream(out, line) { out.stdout.write(line); }
"""


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str, str]]:
    parsers, queries = load_parsers()
    if "javascript" not in parsers:
        pytest.skip("javascript parser not available")
    root = tmp_path_factory.mktemp("io2775") / "svc"
    root.mkdir()
    for name, text in (
        ("store.mjs", _STORE),
        ("legacy.js", _COMMONJS),
        ("lookalike.mjs", _LOOK_ALIKES),
    ):
        (root / name).write_text(text, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="svc",
        capture=resolve_capture(["io"]),
    ).run(force=True)
    return {
        (_short(str(c.args[0][2])), str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) in ("READS_FROM", "WRITES_TO", "FLOWS_TO")
    }


def _short(src: str) -> str:
    # A function by its own name; a resource (a flow's source) as it is.
    return src if src.startswith("resource::") else src.rsplit(".", 1)[-1]


def _io(edges: set[tuple[str, str, str]], caller: str) -> set[tuple[str, str]]:
    return {(rel, dst) for src, rel, dst in edges if src == caller}


@pytest.mark.parametrize(
    "caller",
    ["loadSync", "loadAsync", "loadViaFsPromises", "loadViaNamespace", "openForRead"],
)
def test_promise_reads_read_the_file(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert _io(edges, caller) == {("READS_FROM", _CONFIG)}, edges


@pytest.mark.parametrize("caller", ["save", "log", "wipe", "openForWrite"])
def test_promise_writes_write_the_file(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert _io(edges, caller) == {("WRITES_TO", _STATE)}, edges


@pytest.mark.parametrize("caller", ["loadCjs", "loadCjsMember"])
def test_commonjs_promise_reads(edges: set[tuple[str, str, str]], caller: str) -> None:
    assert _io(edges, caller) == {("READS_FROM", _CONFIG)}, edges


def test_process_streams(edges: set[tuple[str, str, str]]) -> None:
    assert _io(edges, "report") == {("WRITES_TO", _STDOUT)}, edges
    assert _io(edges, "complain") == {("WRITES_TO", _STDERR)}, edges


def test_a_promise_read_flows_to_stdout(edges: set[tuple[str, str, str]]) -> None:
    assert _io(edges, "echoConfig") == {
        ("READS_FROM", _CONFIG),
        ("WRITES_TO", _STDOUT),
    }, edges
    flows = {(src, dst) for src, rel, dst in edges if rel == "FLOWS_TO"}
    assert (_CONFIG, _STDOUT) in flows, flows


@pytest.mark.parametrize("caller", ["shadowed", "local", "stream"])
def test_look_alikes_are_not_io(edges: set[tuple[str, str, str]], caller: str) -> None:
    # Negatives: a parameter named `process`, a local `readFile` and some
    # other object's `stdout` stay local calls.
    assert _io(edges, caller) == set(), edges
