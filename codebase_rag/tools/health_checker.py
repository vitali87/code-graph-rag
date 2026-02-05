from __future__ import annotations

import os
import subprocess

import mgclient  # ty: ignore[unresolved-import]
from loguru import logger

from .. import constants as cs
from ..config import settings
from ..schemas import HealthCheckResult


class HealthChecker:
    def __init__(self):
        self.results: list[HealthCheckResult] = []

    def check_docker(self) -> HealthCheckResult:
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
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
            conn = mgclient.connect(
                host=settings.MEMGRAPH_HOST,
                port=settings.MEMGRAPH_PORT,
            )

            cursor = conn.cursor()
            cursor.execute(cs.HEALTH_CHECK_MEMGRAPH_QUERY)
            list(cursor.fetchall())

            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MEMGRAPH_SUCCESSFUL,
                passed=True,
                message=cs.HEALTH_CHECK_MEMGRAPH_CONNECTED_MSG.format(
                    host=settings.MEMGRAPH_HOST,
                    port=settings.MEMGRAPH_PORT,
                ),
            )

        except mgclient.MemgraphError as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MEMGRAPH_FAILED,
                passed=False,
                message=cs.HEALTH_CHECK_MEMGRAPH_CONNECTION_FAILED_MSG,
                error=cs.HEALTH_CHECK_MEMGRAPH_ERROR.format(error=str(e)),
            )
        except Exception as e:
            return HealthCheckResult(
                name=cs.HEALTH_CHECK_MEMGRAPH_FAILED,
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
                timeout=4,
                check=False,
            )
            if result.returncode == 0:
                lines = result.stdout.strip().splitlines()
                if not lines:
                    return HealthCheckResult(
                        name=cs.HEALTH_CHECK_TOOL_NOT_INSTALLED.format(
                            tool_name=tool_name
                        ),
                        passed=False,
                        message=f"Could not verify {tool_name} installation",
                        error="No output from tool check command",
                    )
                path = lines[0]
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

    def run_all_checks(self) -> list[HealthCheckResult]:
        self.results = []
        self.results.append(self.check_docker())
        self.results.append(self.check_memgraph_connection())
        self.results.extend(self.check_api_keys())
        for tool_name, cmd in cs.HEALTH_CHECK_EXTERNAL_TOOLS:
            self.results.append(self.check_external_tool(tool_name, cmd))
        return self.results

    def get_summary(self) -> tuple[int, int]:
        passed = sum(1 for r in self.results if r.passed)
        return passed, len(self.results)
