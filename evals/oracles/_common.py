from __future__ import annotations

import json
import shutil
import subprocess
import time
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
class NodeOracleUnavailable(RuntimeError):
    """This node toolchain cannot run the oracle, learned after installing.

    A real exception rather than `pytest.skip`, which the oracle runner used
    to raise directly. That made every caller a pytest caller: the four
    standalone eval scripts (`evals/ts_l1.py`, `php_l1.py`, `lua_l1.py`,
    `inheritance.py`) would have died on pytest's private control-flow
    exception instead of reporting an unavailable toolchain.

    `__test__ = False` and the `outcome` attribute let pytest translate this
    into a SKIP at the call sites, so the test-suite behaviour is unchanged:
    an unavailable toolchain must not surface as an ERROR (a misdiagnosis) or
    as an empty payload (which grades every node as missing), which is the
    defect issue #1639 is about.
    """

    __test__ = False


def node_oracle_skip_reason(oracle_dir: Path | None = None) -> str | None:
    """Why the node oracle cannot run here, or None when it can (issue #1639).

    None also covers "cannot tell yet": see rule 2 in `node_oracle_available`.
    The fixed strings the call sites used to print ("node/npm toolchain not
    available") were wrong in the case that matters -- node IS available on the
    reporting machine, and the oracle dies on ERR_REQUIRE_ESM -- so the reason
    carries the captured stderr instead.
    """
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return ec.NODE_SKIP_NO_BINARY.format(binary=ec.NODE_BIN)
    if shutil.which(ec.NPM_BIN) is None:
        return ec.NODE_SKIP_NO_BINARY.format(binary=ec.NPM_BIN)
    if oracle_dir is None:
        return None
    # Rule 2: the MARKER, not the directory. npm creates node_modules before
    # populating it, so a bare directory means "maybe mid-install", and
    # probing it measures a half-written tree.
    if not _node_deps_ready(oracle_dir):
        return None
    package = _oracle_dependency(oracle_dir)
    if package is None:
        return None
    stderr = _node_require_stderr(node, str(oracle_dir), package)
    if stderr is None:
        return None
    return ec.NODE_SKIP_CANNOT_REQUIRE.format(
        package=package, stderr=stderr.strip()[: ec.SKIP_REASON_STDERR_CHARS]
    )


def node_oracle_available(oracle_dir: Path | None = None) -> bool:
    """Whether the node toolchain can RUN this oracle, not merely whether it exists.

    `shutil.which` answers "is this binary on PATH", which is not the question
    a skip guard needs (issue #1639): Node 18 satisfies it and then cannot
    `require()` the ESM-only `@ruby/prism`, so the oracle tests hard-FAILED
    instead of skipping. The probe therefore performs the call that breaks --
    `require()` of the oracle's own dependency from a CommonJS context. A
    dynamic `import()` will not do: it has worked since Node 12, so it passes
    on the very Node that fails the oracle.

    THE INVARIANT, which three separate bugs here were each a variant of:

    1. The only value ever CACHED is a positive verdict, "this node can
       require package X in dir Y". A failed probe is never cached, so any
       later call re-probes. A negative cached before the state it depends on
       has settled poisons every later answer.
    2. Before the completion marker exists the guard answers "unknown, install
       first" (True), never "unavailable". A bare `node_modules` can exist
       while npm is mid-install or after a failed one, and probing then
       measures a half-written tree.
    3. The post-install re-check probes FRESH, because only then is the state
       it depends on settled.

    Each rule exists because breaking it produced a real defect: a stale True
    from before installation, a negative cached from a marker-less tree, and a
    verdict reused after the tree changed underneath it.
    """
    return node_oracle_skip_reason(oracle_dir) is None


_REQUIRE_OK: set[tuple[str, str, str]] = set()


def _node_require_stderr(node: str, oracle_dir: str, package: str) -> str | None:
    """None when the require succeeds, else the stderr explaining why not.

    Rule 1: only the SUCCESS is remembered. A failure is re-probed every time,
    because the thing it depends on -- a fully installed dependency tree -- can
    become true between two calls in one process, and a cached "no" from before
    that point is wrong forever after.
    """
    key = (node, oracle_dir, package)
    if key in _REQUIRE_OK:
        return None
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
    except (subprocess.TimeoutExpired, OSError) as e:
        return str(e)
    if probe.returncode == 0:
        _REQUIRE_OK.add(key)
        return None
    return probe.stderr or probe.stdout


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
    except subprocess.CalledProcessError as e:
        # A failed `npm install` is an unavailable toolchain, not a test error:
        # its stderr is the most useful thing a developer can be shown here.
        raise NodeOracleUnavailable(
            ec.NODE_SKIP_INSTALL_FAILED.format(
                stderr=((e.stderr or e.stdout or "").strip())[
                    : ec.SKIP_REASON_STDERR_CHARS
                ]
            )
        ) from e
    finally:
        lock.rmdir()


def run_node_oracle_payload(
    oracle_dir: Path, script: Path, args: tuple[str, ...]
) -> OraclePayload:
    ensure_node_deps(oracle_dir)
    node = shutil.which(ec.NODE_BIN)
    if node is None:
        return OraclePayload(nodes=[], edges=[], name_edges=[])
    # Re-check AFTER installing. On a clean checkout the guard ran before
    # node_modules existed, so it could not probe and answered "available" to
    # avoid mistaking "not fetched" for "toolchain broken". That answer was
    # provisional, and this is the first moment it can be settled: without
    # this an incompatible runtime reaches the oracle and dies with
    # ERR_REQUIRE_ESM, turning an unavailable toolchain into an evaluation
    # FAILURE, which is the whole defect issue #1639 is about.
    # Rule 3: probe fresh here. This is the first moment the dependency tree is
    # settled, so it is the first moment the question can be answered at all;
    # rule 1 guarantees no earlier failure was cached to short-circuit it.
    reason = node_oracle_skip_reason(oracle_dir)
    if reason is not None:
        raise NodeOracleUnavailable(reason)
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
