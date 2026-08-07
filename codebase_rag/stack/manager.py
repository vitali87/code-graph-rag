from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from loguru import logger

from ..config import settings
from . import constants as cs
from .health import wait_for_memgraph, wait_for_qdrant


def _publishes_on_all_interfaces(mapping: object) -> bool:
    """Whether one `ports` entry publishes without a host address.

    Long-form entries carry an explicit `host_ip`; short-form ones are
    `[host_ip:]host:container`, so a host address is present exactly when the
    mapping has two separators. A mapping bound to 0.0.0.0 or :: is public
    even though it names a host.
    """
    if isinstance(mapping, dict):
        declared = [
            str(value).strip() for key, value in mapping.items() if key == "host_ip"
        ]
        return not declared or declared[0] in ("", "0.0.0.0", "::")
    if not isinstance(mapping, str):
        return False
    text = mapping.strip()
    if text.count(":") < 2:
        return True
    host_ip = text.rsplit(":", 2)[0]
    # Compose wraps an IPv6 host in brackets so its colons are not read as
    # field separators, so `[::]` is the IPv6 wildcard and has to be unwrapped
    # before it can be recognised as one.
    if host_ip.startswith("[") and host_ip.endswith("]"):
        host_ip = host_ip[1:-1]
    return host_ip in ("0.0.0.0", "::", "*")


class StackError(RuntimeError):
    pass


@dataclass
class StackStatus:
    state: cs.StackState
    memgraph_reachable: bool
    qdrant_reachable: bool
    compose_file: Path
    memgraph_endpoint: str
    qdrant_endpoint: str


class StackManager:
    def __init__(
        self,
        home: Path | None = None,
        package_compose: Path | None = None,
        memgraph_host: str | None = None,
        memgraph_port: int | None = None,
        qdrant_port: int = 6333,
        project_name: str = cs.COMPOSE_PROJECT_NAME,
    ) -> None:
        self.home = (home or settings.CGR_HOME).expanduser()
        self.package_compose = (
            package_compose
            or (Path(__file__).resolve().parent / cs.PACKAGE_COMPOSE_RELATIVE).resolve()
        )
        self.memgraph_host = memgraph_host or settings.MEMGRAPH_HOST
        self.memgraph_port = memgraph_port or settings.MEMGRAPH_PORT
        self.qdrant_port = qdrant_port
        self.project_name = project_name

    @property
    def compose_file(self) -> Path:
        return self.home / cs.COMPOSE_FILENAME

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)

    def ensure_compose_file(self) -> Path:
        self.ensure_home()
        target = self.compose_file
        if not target.exists():
            if not self.package_compose.exists():
                raise StackError(
                    cs.ERR_COMPOSE_FILE_MISSING.format(path=self.package_compose)
                )
            logger.info(cs.MSG_RENDERING_COMPOSE.format(path=target))
            shutil.copyfile(self.package_compose, target)
        else:
            self._warn_if_ports_are_public(target)
        return target

    @staticmethod
    def _public_port_mappings(compose_file: Path) -> list[str]:
        """Published ports a compose file leaves bound to every interface.

        Decided from the parsed `services.*.ports` entries, not from whether
        some token appears in the text: a mapping is public exactly when it
        carries no host address, so one corrected service, or the bind-host
        name sitting in a comment, cannot vouch for the rest of the file
        (issue #1012).
        """
        try:
            compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return []
        if not isinstance(compose, dict):
            return []
        services = compose.get("services")
        if not isinstance(services, dict):
            return []
        public: list[str] = []
        for service, spec in services.items():
            if not isinstance(spec, dict):
                continue
            for mapping in spec.get("ports") or []:
                if _publishes_on_all_interfaces(mapping):
                    public.append(f"{service}: {mapping}")
        return public

    @classmethod
    def _warn_if_ports_are_public(cls, compose_file: Path) -> None:
        """Flag a compose file that still publishes on every interface.

        The file is rendered once and never overwritten, so an existing install
        keeps publishing the unauthenticated Memgraph and Qdrant endpoints on
        every interface. It is the user's file and may carry their edits, so
        this reports the exposure and names the remedy rather than clobbering
        it.
        """
        public = cls._public_port_mappings(compose_file)
        if not public:
            return
        logger.warning(
            cs.WARN_COMPOSE_PORTS_PUBLIC.format(
                path=compose_file, mappings=", ".join(public)
            )
        )

    def check_docker(self) -> None:
        if shutil.which(cs.DOCKER_BIN) is None:
            raise StackError(cs.ERR_DOCKER_NOT_INSTALLED)
        info = subprocess.run(
            [cs.DOCKER_BIN, "info"],
            capture_output=True,
            text=True,
            timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
            check=False,
        )
        if info.returncode != 0:
            raise StackError(cs.ERR_DOCKER_DAEMON_DOWN)
        compose = subprocess.run(
            [cs.DOCKER_BIN, cs.DOCKER_COMPOSE_SUBCOMMAND, "version"],
            capture_output=True,
            text=True,
            timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
            check=False,
        )
        if compose.returncode != 0:
            raise StackError(cs.ERR_COMPOSE_NOT_AVAILABLE)

    def _compose_cmd(self, *args: str) -> list[str]:
        return [
            cs.DOCKER_BIN,
            cs.DOCKER_COMPOSE_SUBCOMMAND,
            "-p",
            self.project_name,
            "-f",
            str(self.compose_file),
            *args,
        ]

    def up(self, timeout: float = cs.DEFAULT_DOCKER_TIMEOUT_S) -> None:
        self.check_docker()
        self.ensure_compose_file()
        logger.info(cs.MSG_STARTING_STACK)
        result = subprocess.run(
            self._compose_cmd("up", "-d"),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise StackError(
                cs.ERR_STACK_START_FAILED.format(
                    detail=result.stderr.strip() or result.stdout.strip()
                )
            )

    def down(self, timeout: float = cs.DEFAULT_DOCKER_TIMEOUT_S) -> None:
        if not self.compose_file.exists():
            return
        if shutil.which(cs.DOCKER_BIN) is None:
            raise StackError(cs.ERR_DOCKER_NOT_INSTALLED)
        logger.info(cs.MSG_STOPPING_STACK)
        result = subprocess.run(
            self._compose_cmd("down"),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            raise StackError(
                cs.ERR_STACK_STOP_FAILED.format(
                    detail=result.stderr.strip() or result.stdout.strip()
                )
            )

    def logs(
        self,
        service: str | None = None,
        follow: bool = False,
        tail: int | None = 200,
    ) -> int:
        if not self.compose_file.exists():
            raise StackError(cs.ERR_COMPOSE_FILE_MISSING.format(path=self.compose_file))
        args: list[str] = ["logs"]
        if follow:
            args.append("-f")
        if tail is not None:
            args.extend(["--tail", str(tail)])
        if service:
            args.append(service)
        completed = subprocess.run(self._compose_cmd(*args), check=False)
        return completed.returncode

    def restart(self) -> None:
        logger.info(cs.MSG_RESTARTING_STACK)
        self.down()
        self.up()

    def wait_healthy(
        self,
        timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    ) -> None:
        logger.info(
            cs.MSG_WAITING_FOR_HEALTH.format(
                service=cs.SERVICE_MEMGRAPH,
                host=self.memgraph_host,
                port=self.memgraph_port,
            )
        )
        if not wait_for_memgraph(self.memgraph_host, self.memgraph_port, timeout):
            raise StackError(
                cs.ERR_STACK_NOT_HEALTHY.format(
                    service=cs.SERVICE_MEMGRAPH, timeout=timeout
                )
            )
        logger.info(
            cs.MSG_WAITING_FOR_HEALTH.format(
                service=cs.SERVICE_QDRANT,
                host=cs.LOOPBACK_HOST,
                port=self.qdrant_port,
            )
        )
        if not wait_for_qdrant(self.qdrant_port, timeout):
            raise StackError(
                cs.ERR_STACK_NOT_HEALTHY.format(
                    service=cs.SERVICE_QDRANT, timeout=timeout
                )
            )

    def status(self) -> StackStatus:
        memgraph_ok = wait_for_memgraph(
            self.memgraph_host, self.memgraph_port, timeout=0.1, interval=0.0
        )
        qdrant_ok = wait_for_qdrant(self.qdrant_port, timeout=0.1, interval=0.0)
        match (memgraph_ok, qdrant_ok):
            case (True, True):
                state = cs.StackState.RUNNING
            case (False, False):
                state = cs.StackState.STOPPED
            case _:
                state = cs.StackState.PARTIAL
        return StackStatus(
            state=state,
            memgraph_reachable=memgraph_ok,
            qdrant_reachable=qdrant_ok,
            compose_file=self.compose_file,
            memgraph_endpoint=f"{self.memgraph_host}:{self.memgraph_port}",
            qdrant_endpoint=f"{cs.LOOPBACK_HOST}:{self.qdrant_port}",
        )

    def ensure_running(self) -> StackStatus:
        current = self.status()
        if current.state == cs.StackState.RUNNING:
            logger.info(cs.MSG_STACK_ALREADY_RUNNING)
            return current
        self.up()
        self.wait_healthy()
        final = self.status()
        logger.info(
            cs.MSG_STACK_HEALTHY.format(
                memgraph=final.memgraph_endpoint,
                qdrant=final.qdrant_endpoint,
            )
        )
        return final


def ensure_running() -> StackStatus:
    return StackManager().ensure_running()


def daemon_up() -> StackStatus:
    mgr = StackManager()
    mgr.up()
    mgr.wait_healthy()
    return mgr.status()


def daemon_down() -> None:
    StackManager().down()


def daemon_status() -> StackStatus:
    return StackManager().status()


def daemon_logs(service: str | None = None, follow: bool = False) -> int:
    return StackManager().logs(service=service, follow=follow)


def daemon_restart() -> StackStatus:
    mgr = StackManager()
    mgr.restart()
    mgr.wait_healthy()
    return mgr.status()
