from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import mgclient  # ty: ignore[unresolved-import]
from loguru import logger

from .. import constants as cs
from .. import graph_audit
from ..config import settings
from ..graph_dialects import DIALECT_NEO4J
from ..schemas import HealthCheckResult
from ..services.graph_service import MemgraphIngestor
from ..types_defs import ConnectionProtocol, CursorProtocol, ResultRow


@contextmanager
def _backend_connection() -> Iterator[ConnectionProtocol]:
    """Open a connection to whichever graph engine is configured.

    Health output must describe the backend actually in use, so this
    reuses the ingestor's own connection factory rather than reaching for
    `mgclient` directly -- otherwise `cgr health` would report on a
    Memgraph server that a Neo4j install never talks to.

    A context manager because the ingestor OWNS the Neo4j connection
    pool: returning a bare connection would close the one session and
    leak a whole driver per health check.
    """
    ingestor = MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    )
    conn = ingestor._create_connection()
    try:
        yield conn
    finally:
        try:
            conn.close()
        finally:
            ingestor.close_driver()


def _connection_error_types() -> tuple[type[BaseException], ...]:
    """Exceptions that mean "the server is not reachable", per engine.

    A Neo4j failure surfaces as `neo4j.exceptions.ServiceUnavailable`,
    which is neither an `mgclient.Error` nor an `OSError`, so without
    this it falls through to the generic "unexpected failure" branch and
    an unreachable server is reported as a mystery rather than as a
    connection problem. Imported lazily because `neo4j` is an optional
    extra.

    Scoped deliberately: this answers "can we reach the server", so it
    covers transport failures and authentication, not every Neo4j error.
    A Cypher failure is a query problem and keeps its own diagnostic.
    """
    # Accumulated rather than returned as differently-shaped tuples: this
    # is a variadic `except` argument, not a fixed-arity value, and the
    # list makes that intent explicit (python:S8495).
    types: list[type[BaseException]] = [mgclient.Error]
    if settings.GRAPH_BACKEND == DIALECT_NEO4J:
        try:
            from neo4j.exceptions import (  # ty: ignore[unresolved-import]
                AuthError,
                DriverError,
            )
        except ImportError:  # pragma: no cover - depends on extras
            pass
        else:
            # `DriverError` is the client-side/transport branch --
            # ServiceUnavailable and SessionExpired live here. `AuthError`
            # is a server error but still means "cannot connect". The rest
            # of `Neo4jError` (a Cypher syntax error, say) is a genuine
            # query failure and must NOT be reported as a connectivity
            # problem, so it deliberately falls through to the generic
            # branch.
            types += [DriverError, AuthError]
    return tuple(types)


def _backend_engine_name() -> str:
    """The engine's display name, for health output."""
    return cs.HEALTH_ENGINE_NAMES.get(settings.GRAPH_BACKEND, settings.GRAPH_BACKEND)


def _backend_endpoint() -> str:
    """The address health output should name, for the configured engine."""
    if settings.GRAPH_BACKEND == DIALECT_NEO4J:
        return settings.NEO4J_URI
    return f"{settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}"


class HealthChecker:
    __slots__ = ("results",)

    def __init__(self):
        self.results: list[HealthCheckResult] = []

    def check_docker(self) -> HealthCheckResult:
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                encoding=cs.ENCODING_UTF8,
                timeout=5,
                check=False,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                return HealthCheckResult(
                    name=cs.HEALTH_CHECK_DOCKER_RUNNING,
                    passed=True,
                    message=cs.HEALTH_CHECK_DOCKER_RUNNING_MSG.format(version=version),
                )
            else:
                return HealthCheckResult(
                    name=cs.HEALTH_CHECK_DOCKER_NOT_RUNNING,
                    passed=False,
                    message=cs.HEALTH_CHECK_DOCKER_NOT_RESPONDING_MSG,
                    error=result.stderr.strip() or cs.HEALTH_CHECK_DOCKER_EXIT_CODE,
                )
        except FileNotFoundError:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_DOCKER_NOT_RUNNING,
                passed=False,
                message=cs.HEALTH_CHECK_DOCKER_NOT_INSTALLED_MSG,
                error=cs.HEALTH_CHECK_DOCKER_NOT_IN_PATH,
            )
        except subprocess.TimeoutExpired:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_DOCKER_NOT_RUNNING,
                passed=False,
                message=cs.HEALTH_CHECK_DOCKER_TIMEOUT_MSG,
                error=cs.HEALTH_CHECK_DOCKER_TIMEOUT_ERROR,
            )
        except Exception as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_DOCKER_NOT_RUNNING,
                passed=False,
                message=cs.HEALTH_CHECK_DOCKER_FAILED_MSG,
                error=str(e),
            )

    def check_memgraph_connection(self) -> HealthCheckResult:
        conn = None
        cursor = None
        try:
            with _backend_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(cs.HEALTH_CHECK_MEMGRAPH_QUERY)
                list(cursor.fetchall())

            return HealthCheckResult(
                name=cs.HEALTH_CHECK_GRAPH_SUCCESSFUL.format(
                    engine=_backend_engine_name()
                ),
                passed=True,
                message=cs.HEALTH_CHECK_MEMGRAPH_CONNECTED_MSG.format(
                    endpoint=_backend_endpoint(),
                ),
            )

        except _connection_error_types() as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_GRAPH_FAILED.format(engine=_backend_engine_name()),
                passed=False,
                message=cs.HEALTH_CHECK_MEMGRAPH_CONNECTION_FAILED_MSG,
                error=cs.HEALTH_CHECK_GRAPH_ERROR.format(
                    engine=_backend_engine_name(), error=str(e)
                ),
            )
        except Exception as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_GRAPH_FAILED.format(engine=_backend_engine_name()),
                passed=False,
                message=cs.HEALTH_CHECK_MEMGRAPH_UNEXPECTED_FAILURE_MSG,
                error=str(e),
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception as e:
                    logger.warning(f"Failed to close Memgraph cursor: {e}")
            if conn is not None:
                try:
                    conn.close()
                except Exception as e:
                    logger.warning(f"Failed to close Memgraph connection: {e}")

    def check_api_key(self, env_name: str, display_name: str) -> HealthCheckResult:
        value = os.getenv(env_name) or getattr(settings, env_name, None)
        passed = bool(value)
        error_msg = (
            None
            if passed
            else cs.HEALTH_CHECK_API_KEY_MISSING_MSG.format(env_name=env_name)
        )
        return HealthCheckResult(
            name=(
                cs.HEALTH_CHECK_API_KEY_SET.format(display_name=display_name)
                if passed
                else cs.HEALTH_CHECK_API_KEY_NOT_SET.format(display_name=display_name)
            ),
            passed=passed,
            message=cs.HEALTH_CHECK_API_KEY_CONFIGURED
            if passed
            else cs.HEALTH_CHECK_API_KEY_NOT_CONFIGURED,
            error=error_msg,
        )

    def check_api_keys(self) -> list[HealthCheckResult]:
        return [
            self.check_api_key(env_name, display_name)
            for env_name, display_name in cs.HEALTH_CHECK_TOOLS
        ]

    def check_external_tool(
        self, tool_name: str, command: str | None = None
    ) -> HealthCheckResult:
        cmd = command or tool_name
        check_cmd = [
            cs.SHELL_CMD_WHERE if os.name == "nt" else cs.SHELL_CMD_WHICH,
            cmd,
        ]

        try:
            result = subprocess.run(
                check_cmd,
                capture_output=True,
                text=True,
                encoding=cs.ENCODING_UTF8,
                timeout=4,
                check=False,
            )
            if result.returncode == 0:
                path = result.stdout.strip().splitlines()[0]
                return HealthCheckResult(
                    name=cs.HEALTH_CHECK_TOOL_INSTALLED.format(tool_name=tool_name),
                    passed=True,
                    message=cs.HEALTH_CHECK_TOOL_INSTALLED_MSG.format(path=path),
                )
            else:
                return HealthCheckResult(
                    name=cs.HEALTH_CHECK_TOOL_NOT_INSTALLED.format(tool_name=tool_name),
                    passed=False,
                    message=cs.HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG.format(cmd=cmd),
                    error=cs.HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG.format(cmd=cmd),
                )
        except subprocess.TimeoutExpired:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_TOOL_NOT_INSTALLED.format(tool_name=tool_name),
                passed=False,
                message=cs.HEALTH_CHECK_TOOL_TIMEOUT_MSG,
                error=cs.HEALTH_CHECK_TOOL_TIMEOUT_ERROR.format(cmd=cmd),
            )
        except Exception as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_TOOL_NOT_INSTALLED.format(tool_name=tool_name),
                passed=False,
                message=cs.HEALTH_CHECK_TOOL_FAILED_MSG,
                error=str(e),
            )

    def check_graph_integrity(self) -> list[HealthCheckResult]:
        """Structural audit of the live graph (issue #646).

        Returns no results when Memgraph is unreachable: connectivity is
        already reported by check_memgraph_connection.
        """
        # Enter the context here, not inside the audit's try: an
        # unreachable server must yield no results (connectivity is
        # already reported by check_memgraph_connection), not an
        # "audit queries failed" result.
        connection = _backend_connection()
        try:
            conn = connection.__enter__()
        except Exception:
            return []

        # Bound before the try: `conn.cursor()` can raise, and the
        # cleanup below would then mask the real error with an
        # UnboundLocalError.
        cursor: CursorProtocol | None = None
        try:
            cursor = conn.cursor()

            def fetch_all(query: str) -> list[ResultRow]:
                cursor.execute(query)
                # mgclient.Column is not subscriptable; the name is an
                # attribute.
                columns = [column.name for column in cursor.description or []]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]

            violations = graph_audit.collect_live_violations(fetch_all)
        except Exception as e:
            return [
                HealthCheckResult(
                    name=cs.HEALTH_CHECK_GRAPH_INTEGRITY_FAILED,
                    passed=False,
                    message=cs.HEALTH_CHECK_GRAPH_INTEGRITY_ERROR_MSG,
                    error=str(e),
                )
            ]
        finally:
            # Cleanup is deliberately OUTSIDE the audit's `except`: closing
            # the cursor, the connection and (on Neo4j) the driver can
            # raise, and a cleanup failure must not report a completed
            # audit as failed. Logged rather than swallowed silently.
            closers = [lambda: connection.__exit__(None, None, None)]
            if cursor is not None:
                closers.insert(0, cursor.close)
            for close in closers:
                try:
                    close()
                except Exception as cleanup_error:
                    logger.warning(f"Graph audit cleanup failed: {cleanup_error}")

        if not violations:
            return [
                HealthCheckResult(
                    name=cs.HEALTH_CHECK_GRAPH_INTEGRITY_OK,
                    passed=True,
                    message=cs.HEALTH_CHECK_GRAPH_INTEGRITY_OK_MSG,
                )
            ]
        return [
            HealthCheckResult(
                name=cs.HEALTH_CHECK_GRAPH_INTEGRITY_FAILED,
                passed=False,
                message=cs.HEALTH_CHECK_GRAPH_INTEGRITY_VIOLATIONS_MSG.format(
                    count=len(violations)
                ),
                error=cs.HEALTH_CHECK_GRAPH_INTEGRITY_SEPARATOR.join(
                    v.detail for v in violations
                ),
            )
        ]

    def run_all_checks(self) -> list[HealthCheckResult]:
        self.results = []
        self.results.append(self.check_docker())
        self.results.append(self.check_memgraph_connection())
        self.results.extend(self.check_graph_integrity())
        self.results.extend(self.check_api_keys())
        for tool_name, cmd in cs.HEALTH_CHECK_EXTERNAL_TOOLS:
            self.results.append(self.check_external_tool(tool_name, cmd))
        return self.results

    def get_summary(self) -> tuple[int, int]:
        passed = sum(1 for r in self.results if r.passed)
        return passed, len(self.results)
