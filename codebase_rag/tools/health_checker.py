import os
import subprocess
from dataclasses import dataclass

import mgclient  # type: ignore

from ..config import settings


@dataclass
class HealthCheckResult:
    """Result of a single health check."""

    name: str
    passed: bool
    message: str
    error: str | None = None


class HealthChecker:
    """Verifies all critical dependencies and configurations."""

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
                    name="Docker daemon is running",
                    passed=True,
                    message=f"Running (version {version})",
                )
            else:
                return HealthCheckResult(
                    name="Docker daemon is not running",
                    passed=False,
                    message="Not responding",
                    error=result.stderr.strip() or "Non-zero exit code",
                )
        except FileNotFoundError:
            return HealthCheckResult(
                name="Docker daemon is not running",
                passed=False,
                message="Not installed",
                error="docker command not found in PATH",
            )
        except Exception as e:
            return HealthCheckResult(
                name="Docker daemon is not running",
                passed=False,
                message="Check failed",
                error=str(e),
            )

    def check_memgraph_connection(self) -> HealthCheckResult:
        """Check if Memgraph is accessible and can execute a simple query."""
        conn = None
        cursor = None
        try:
            conn = mgclient.connect(
                host=settings.MEMGRAPH_HOST,
                port=settings.MEMGRAPH_PORT,
            )

            cursor = conn.cursor()
            cursor.execute("RETURN 1 AS test;")
            list(cursor.fetchall())

            return HealthCheckResult(
                name="Memgraph connection successful",
                passed=True,
                message=f"Connected and responsive at {settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}",
            )

        except mgclient.MemgraphError as e:
            return HealthCheckResult(
                name="Memgraph connection failed",
                passed=False,
                message="Connection or query failed",
                error=f"Memgraph error: {str(e)}",
            )
        except Exception as e:
            return HealthCheckResult(
                name="Memgraph connection failed",
                passed=False,
                message="Unexpected failure",
                error=str(e),
            )
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def check_api_key(self, env_name: str, display_name: str) -> HealthCheckResult:
        value = os.getenv(env_name) or getattr(
            settings, env_name.replace("_API_KEY", "_API_KEY"), None
        )
        passed = bool(value)
        return HealthCheckResult(
            name=f"{display_name} API key is set"
            if passed
            else f"{display_name} API key is not set",
            passed=passed,
            message="Configured" if passed else "Not set",
        )

    def check_api_keys(self) -> list[HealthCheckResult]:
        return [
            self.check_api_key("GEMINI_API_KEY", "Gemini"),
            self.check_api_key("OPENAI_API_KEY", "OpenAI"),
            self.check_api_key("ORCHESTRATOR_API_KEY", "Orchestrator"),
            self.check_api_key("CYPHER_API_KEY", "Cypher"),
        ]

    def check_external_tool(
        self, tool_name: str, command: str | None = None
    ) -> HealthCheckResult:
        cmd = command or tool_name
        check_cmd = ["where" if os.name == "nt" else "which", cmd]

        try:
            result = subprocess.run(
                check_cmd,
                capture_output=True,
                timeout=4,
                check=False,
            )
            if result.returncode == 0:
                path = result.stdout.decode().strip().splitlines()[0]
                return HealthCheckResult(
                    name=f"{tool_name} is installed",
                    passed=True,
                    message=f"Installed ({path})",
                )
            else:
                return HealthCheckResult(
                    name=f"{tool_name} is not installed",
                    passed=False,
                    message="Not installed",
                    error=f"'{cmd}' not found in PATH",
                )
        except Exception as e:
            return HealthCheckResult(
                name=f"{tool_name} is not installed",
                passed=False,
                message="Check failed",
                error=str(e),
            )

    def run_all_checks(self) -> list[HealthCheckResult]:
        self.results = []
        self.results.append(self.check_docker())
        self.results.append(self.check_memgraph_connection())
        self.results.extend(self.check_api_keys())
        self.results.append(self.check_external_tool("ripgrep", "rg"))
        self.results.append(self.check_external_tool("cmake"))
        return self.results

    def get_summary(self) -> tuple[int, int]:
        passed = sum(1 for r in self.results if r.passed)
        return passed, len(self.results)
