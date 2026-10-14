from __future__ import annotations

import importlib
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import mgclient
from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import exceptions as ex
from .. import graph_audit
from ..config import PROVIDER_ENV_KEYS, ModelConfig, settings
from ..graph_dialects import DIALECT_NEO4J
from ..schemas import HealthCheckResult
from ..services.graph_service import MemgraphIngestor
from ..types_defs import ConnectionProtocol, CursorProtocol, ResultRow
from ..utils.endpoints import join_endpoint_path, strip_v1_suffix

# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception type is bound once here.
_MgclientError: type[Exception] = mgclient.Error  # ty: ignore[unresolved-attribute]


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
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
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
    types: list[type[BaseException]] = [_MgclientError]
    if settings.GRAPH_BACKEND == DIALECT_NEO4J:
        # Imported by name, as `services.neo4j_driver` does, so the check
        # reads the same with or without the extra installed.
        try:
            neo4j_exceptions = importlib.import_module(cs.NEO4J_EXCEPTIONS_MODULE)
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
            types += [neo4j_exceptions.DriverError, neo4j_exceptions.AuthError]
    return tuple(types)


def _backend_engine_name() -> str:
    """The engine's display name, for health output."""
    return cs.HEALTH_ENGINE_NAMES.get(settings.GRAPH_BACKEND, settings.GRAPH_BACKEND)


def _backend_endpoint() -> str:
    """The address health output should name, for the configured engine."""
    if settings.GRAPH_BACKEND == DIALECT_NEO4J:
        return settings.NEO4J_URI
    return f"{settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}"


def _ollama_models(base_url: str) -> list[str] | None:
    """The models an Ollama server has pulled, or None if none answered."""
    try:
        with httpx.Client(timeout=settings.OLLAMA_HEALTH_TIMEOUT) as client:
            response = client.get(join_endpoint_path(base_url, cs.OLLAMA_HEALTH_PATH))
    except httpx.HTTPError:
        return None
    if response.status_code != cs.HTTP_OK:
        return None
    try:
        payload = response.json()
    except ValueError:
        # Something other than Ollama is listening on the port.
        return None
    models = payload.get(cs.OLLAMA_TAGS_MODELS) if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    return [
        str(entry.get(cs.OLLAMA_TAGS_NAME))
        for entry in models
        if isinstance(entry, dict) and entry.get(cs.OLLAMA_TAGS_NAME)
    ]


def _ollama_has_model(pulled: list[str], model_id: str) -> bool:
    # An untagged name is what Ollama resolves to `:latest`; a tagged one
    # names exactly one pull.
    wanted = {model_id}
    if cs.OLLAMA_TAG_SEPARATOR not in model_id:
        wanted.add(f"{model_id}{cs.OLLAMA_TAG_SEPARATOR}{cs.OLLAMA_LATEST_TAG}")
    return any(name in wanted for name in pulled)


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

    def check_model_role(self, role: cs.ModelRole) -> HealthCheckResult:
        """Whether the runtime would accept this role's model credentials.

        Judged by the same call `cgr start` makes before it runs
        (`ModelConfig.validate_api_key`), so this check and the start-up
        gate cannot disagree: a local model needs no key, a provider's
        own key variable counts where the runtime accepts it, and a
        missing key is named by the variable the runtime reads
        (issue #1910).
        """
        role_name = cs.HEALTH_MODEL_ROLE_NAMES.get(role.value, role.value)
        try:
            config = (
                settings.active_orchestrator_config
                if role == cs.ModelRole.ORCHESTRATOR
                else settings.active_cypher_config
            )
        except ValueError as e:
            # A half-configured role is refused by `cgr start` too; doctor
            # reports it as a failed check instead of crashing on it.
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MODEL_MISCONFIGURED.format(role=role_name),
                passed=False,
                message=cs.HEALTH_CHECK_MODEL_MISCONFIGURED_MSG,
                error=str(e),
            )
        label = {
            "role": role_name,
            "provider": config.provider,
            "model": config.model_id,
        }
        try:
            config.validate_api_key(role)
        except ex.UnknownProviderError as e:
            # No key fixes a provider nothing serves (issue #2897).
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MODEL_MISCONFIGURED.format(role=role_name),
                passed=False,
                message=cs.HEALTH_CHECK_MODEL_MISCONFIGURED_MSG,
                error=str(e),
            )
        except ValueError:
            role_var = cs.HEALTH_MODEL_ROLE_KEY_VARIABLE.format(role=role.value.upper())
            # The gate accepts a provider-owned variable for some providers;
            # read its map rather than restating the rule here.
            provider_var = PROVIDER_ENV_KEYS.get(config.provider.lower())
            error = (
                cs.HEALTH_CHECK_MODEL_KEY_MISSING_EITHER.format(
                    env_name=role_var, provider_env=provider_var
                )
                if provider_var
                else cs.HEALTH_CHECK_MODEL_KEY_MISSING_ERROR.format(env_name=role_var)
            )
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MODEL_NOT_READY.format(**label),
                passed=False,
                message=cs.HEALTH_CHECK_MODEL_KEY_MISSING_MSG,
                error=error,
            )
        if config.provider.lower() == cs.Provider.OLLAMA:
            return self._check_ollama_model(config, role, label)
        # Key-based providers are not called over the network here: what
        # passed is the credentials, and the name says so (#2423).
        return HealthCheckResult(
            name=cs.HEALTH_CHECK_MODEL_CREDENTIALS.format(**label),
            passed=True,
            message=cs.HEALTH_CHECK_MODEL_OK_MSG.format(provider=config.provider),
        )

    def _check_ollama_model(
        self, config: ModelConfig, role: cs.ModelRole, label: dict[str, str]
    ) -> HealthCheckResult:
        """Ready only when Ollama answers and has the model (#2423).

        Ollama needs no key, so the credential rule alone passed with no
        server running and no model pulled.
        """
        base_url = strip_v1_suffix(config.endpoint or settings.ollama_endpoint)
        pulled = _ollama_models(base_url)
        if pulled is None:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MODEL_UNREACHABLE.format(**label),
                passed=False,
                message=cs.HEALTH_CHECK_OLLAMA_UNREACHABLE_MSG,
                error=cs.HEALTH_CHECK_OLLAMA_UNREACHABLE_ERROR.format(
                    url=base_url, role=role.value.upper()
                ),
            )
        if not _ollama_has_model(pulled, config.model_id):
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MODEL_NOT_PULLED.format(**label),
                passed=False,
                message=cs.HEALTH_CHECK_OLLAMA_NOT_PULLED_MSG,
                error=cs.HEALTH_CHECK_OLLAMA_NOT_PULLED_ERROR.format(
                    model=config.model_id
                ),
            )
        return HealthCheckResult(
            name=cs.HEALTH_CHECK_MODEL_READY.format(**label),
            passed=True,
            message=cs.HEALTH_CHECK_OLLAMA_READY_MSG.format(url=base_url),
        )

    def check_model_roles(self) -> list[HealthCheckResult]:
        return [self.check_model_role(role) for role in cs.ModelRole]

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
            interrupted = sorted(
                str(row["project"])
                for row in fetch_all(cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS)
                if row.get("project")
            )
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

        results = [
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
            if violations
            else HealthCheckResult(
                name=cs.HEALTH_CHECK_GRAPH_INTEGRITY_OK,
                passed=True,
                message=cs.HEALTH_CHECK_GRAPH_INTEGRITY_OK_MSG,
            )
        ]
        if interrupted:
            results.append(
                HealthCheckResult(
                    name=cs.HEALTH_CHECK_INTERRUPTED_SYNC,
                    passed=False,
                    message=cs.HEALTH_CHECK_INTERRUPTED_SYNC_MSG.format(
                        count=len(interrupted)
                    ),
                    error=cs.HEALTH_CHECK_GRAPH_INTEGRITY_SEPARATOR.join(
                        cs.HEALTH_CHECK_INTERRUPTED_SYNC_DETAIL.format(project=project)
                        for project in interrupted
                    ),
                )
            )
        return results

    def run_all_checks(self) -> list[HealthCheckResult]:
        self.results = []
        self.results.append(self.check_docker())
        self.results.append(self.check_memgraph_connection())
        self.results.extend(self.check_graph_integrity())
        self.results.extend(self.check_model_roles())
        for tool_name, cmd in cs.HEALTH_CHECK_EXTERNAL_TOOLS:
            self.results.append(self.check_external_tool(tool_name, cmd))
        return self.results

    def get_summary(self) -> tuple[int, int]:
        passed = sum(1 for r in self.results if r.passed)
        return passed, len(self.results)
