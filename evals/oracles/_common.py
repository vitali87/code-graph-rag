from __future__ import annotations

import json
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath

from codebase_rag import constants as cs

from .. import constants as ec
from ..types_defs import (
    DefNode,
    EdgeKey,
    GraphData,
    NameEdge,
    NodeKey,
    OracleEdge,
    OracleNameEdge,
    OracleNodeRef,
    OraclePayload,
    OracleRecord,
)


# The node-backed oracles (Lua, PHP, Ruby, TypeScript/JavaScript) all shell
# out to the same toolchain, so they share one availability probe.
def node_oracle_available(oracle_dir: Path | None = None) -> bool:
    """Whether the node toolchain can RUN this oracle, not merely whether it exists.

    `shutil.which` answers "is this binary on PATH", which is not the question
    a skip guard needs (issue #1639). Node 18 satisfies it and then cannot
    `require()` the ESM-only `@ruby/prism` that `ruby_ast.js` loads, so the
    node-backed oracle tests hard-FAILED on an under-provisioned machine
    instead of skipping: 24 failures and 4 errors on a clean main, in tests
    named as though they were real regressions.

    The probe therefore does the thing that breaks -- `require()` of the
    oracle's own entry script's dependency, from a CommonJS context, which is
    exactly the failing call. A dynamic `import()` would NOT do: it has worked
    since Node 12, so a Node that fails the real oracle passes that probe and
    the guard is worse than none. Measured against a Node-18-shaped shim
    (dynamic import fine, `require()` of an ES module refused), an
    `import()`-based probe reported the toolchain as available.

    `oracle_dir` is required to reach that dependency, because the oracles do
    not share one: ruby loads `@ruby/prism`, lua `luaparse`, php `php-parser`,
    ts `typescript`, and only the first is ESM-only. Called with no directory
    the check degrades to node+npm presence, which is all it can honestly
    assert without knowing which package to load.
    """
    node = shutil.which(ec.NODE_BIN)
    if node is None or shutil.which(ec.NPM_BIN) is None:
        return False
    if oracle_dir is None:
        return True
    package = _oracle_dependency(oracle_dir)
    if package is None:
        # Deps not installed yet, or nothing to load: `ensure_node_deps` runs
        # later and fixes the former. NOT cached, deliberately -- this is "ask
        # again", not "yes". Caching it locked in a provisional answer taken
        # before installation, so on a clean checkout the real probe never ran
        # at all and an incompatible Node reached the oracle anyway.
        return True
    return _node_can_require(node, str(oracle_dir), package)


@lru_cache(maxsize=16)
def _node_can_require(node: str, oracle_dir: str, package: str) -> bool:
    """Whether this node can `require()` this package from this directory.

    Cached on the full triple, and only ever on a REAL verdict: the answer to
    this question cannot change within a process, whereas "are the deps
    installed" can and must not be memoised alongside it.
    """
    try:
        probe = subprocess.run(
            [node, ec.NODE_EVAL_FLAG, ec.NODE_REQUIRE_PROBE.format(package=package)],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            cwd=oracle_dir,
            timeout=ec.NODE_PROBE_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return probe.returncode == 0


def _oracle_dependency(oracle_dir: Path) -> str | None:
    """The package this oracle actually `require()`s, read from its own script.

    Read, never guessed: the four node oracles do not share a dependency
    (ruby `@ruby/prism`, lua `luaparse`, php `php-parser`, ts `typescript`),
    and only the first is ESM-only. Taking the alphabetically first entry of
    `package.json` picks the right one today ONLY because each manifest
    happens to hold exactly one dependency; add a second and the probe
    silently validates a package the oracle never loads.

    The entry script's own `require()` is the authoritative source, because it
    is literally the call that breaks. Bare builtins (`fs`, `path`) are
    skipped: they load on every Node and would make the probe vacuous.

    None when deps are not installed yet or no third-party require is found,
    which the caller treats as "ask again later" rather than as a verdict.
    """
    if not (oracle_dir / ec.NODE_MODULES_DIRNAME).is_dir():
        return None
    for script in sorted(oracle_dir.glob(ec.NODE_ORACLE_SCRIPT_GLOB)):
        try:
            source = script.read_text(encoding=cs.ENCODING_UTF8)
        except OSError:
            continue
        for name in ec.NODE_REQUIRE_PATTERN.findall(source):
            if name not in ec.NODE_BUILTIN_MODULES and not name.startswith("."):
                return name
    return None


def _node_deps_ready(oracle_dir: Path) -> bool:
    # Both must hold: the marker proves npm completed, node_modules proves a
    # later cache cleanup did not delete the installed tree under the marker.
    return (oracle_dir / ec.NODE_DEPS_MARKER).exists() and (
        oracle_dir / ec.NODE_MODULES_DIRNAME
    ).is_dir()


def ensure_node_deps(oracle_dir: Path) -> None:
    # The marker (written only after npm exits 0) is the completion signal;
    # node_modules is not, because npm creates it before populating it and a
    # concurrent pytest-xdist worker would run the oracle against a
    # half-installed tree. The mkdir lock is atomic on every platform.
    # ponytail: a stale lock (installer killed mid-run) is waited out for
    # TRIES*POLL seconds and then skipped, letting the node run surface the
    # real error; clean the lock dir manually if that ever happens.
    marker = oracle_dir / ec.NODE_DEPS_MARKER
    if _node_deps_ready(oracle_dir):
        return
    npm = shutil.which(ec.NPM_BIN)
    if npm is None:
        return
    lock = oracle_dir / ec.NODE_DEPS_LOCK
    for _ in range(ec.NODE_DEPS_LOCK_TRIES):
        try:
            lock.mkdir()
            break
        except FileExistsError:
            time.sleep(ec.NODE_DEPS_LOCK_POLL_SECONDS)
            if _node_deps_ready(oracle_dir):
                return
    else:
        return
    try:
        if not _node_deps_ready(oracle_dir):
            marker.unlink(missing_ok=True)
            subprocess.run(
                [npm, ec.NPM_INSTALL, *ec.NPM_FLAGS],
                cwd=str(oracle_dir),
                capture_output=True,
                text=True,
                # Same reason as the oracle run below: npm's output is UTF-8,
                # and a locale decode would corrupt any non-ASCII path in the
                # error text raised on a failed install.
                encoding=cs.ENCODING_UTF8,
                check=True,
            )
            marker.touch()
    finally:
        lock.rmdir()


def run_node_oracle_payload(
    oracle_dir: Path, script: Path, args: tuple[str, ...]
) -> OraclePayload:
    ensure_node_deps(oracle_dir)
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    proc = subprocess.run(
        [node, str(script), *args],
        capture_output=True,
        text=True,
        # Node writes UTF-8 JSON. `text=True` alone decodes with the LOCALE
        # encoding, which is cp1252 on a Windows runner, so any non-ASCII
        # identifier or path comes back mangled and the payload disagrees with
        # the source. Reading a name like "Café" then fails far from here, as a
        # lookup miss rather than as a decode error.
        encoding=cs.ENCODING_UTF8,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            ec.NODE_ORACLE_FAILED.format(
                script=script.name, code=proc.returncode, stderr=proc.stderr
            )
        )
    payload: OraclePayload = json.loads(proc.stdout or "{}")
    return payload


def is_ignored(rel_file: str) -> bool:
    # Mirror cgr's directory-component ignore (path_utils.should_skip_path)
    # so an oracle grades the same file set cgr indexes.
    dir_parts = PurePosixPath(rel_file).parent.parts
    return not cs.IGNORE_PATTERNS.isdisjoint(dir_parts)


def records_to_nodes(records: list[OracleRecord]) -> dict[NodeKey, DefNode]:
    nodes: dict[NodeKey, DefNode] = {}
    for rec in records:
        rel_file = rec[ec.ORACLE_KEY_FILE]
        if is_ignored(rel_file):
            continue
        line = int(rec[ec.ORACLE_KEY_LINE])
        key = NodeKey(rec[ec.ORACLE_KEY_KIND], rel_file, line)
        end_line = int(rec.get(ec.ORACLE_KEY_END_LINE, line))
        nodes[key] = DefNode(key, rec[ec.ORACLE_KEY_NAME], end_line)
    return nodes


def _ref_to_key(ref: OracleNodeRef) -> NodeKey:
    return NodeKey(
        ref[ec.ORACLE_KEY_KIND],
        ref[ec.ORACLE_KEY_FILE],
        int(ref[ec.ORACLE_KEY_LINE]),
    )


def records_to_edges(edges: list[OracleEdge]) -> set[EdgeKey]:
    out: set[EdgeKey] = set()
    for edge in edges:
        parent = edge[ec.ORACLE_KEY_PARENT]
        child = edge[ec.ORACLE_KEY_CHILD]
        if is_ignored(parent[ec.ORACLE_KEY_FILE]) or is_ignored(
            child[ec.ORACLE_KEY_FILE]
        ):
            continue
        out.add(
            EdgeKey(edge[ec.ORACLE_KEY_REL], _ref_to_key(parent), _ref_to_key(child))
        )
    return out


def records_to_name_edges(name_edges: list[OracleNameEdge]) -> set[NameEdge]:
    out: set[NameEdge] = set()
    for edge in name_edges:
        source = edge[ec.ORACLE_KEY_SOURCE]
        if is_ignored(source[ec.ORACLE_KEY_FILE]):
            continue
        out.add(
            NameEdge(
                edge[ec.ORACLE_KEY_REL],
                _ref_to_key(source),
                edge[ec.ORACLE_KEY_TARGET_NAME],
            )
        )
    return out


def payload_to_graph(payload: OraclePayload) -> GraphData:
    return GraphData(
        nodes=records_to_nodes(payload.get(ec.ORACLE_KEY_NODES, [])),
        edges=records_to_edges(payload.get(ec.ORACLE_KEY_EDGES, [])),
        name_edges=records_to_name_edges(payload.get(ec.ORACLE_KEY_NAME_EDGES, [])),
    )
