from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values
from loguru import logger

from .. import constants as root_cs
from .. import logs as ls
from ..config import settings
from ..types_defs import JsonValue
from . import constants as cs
from .health import (
    memgraph_accepts_anonymous,
    memgraph_anonymous_access,
    memgraph_rejects_credentials,
    qdrant_accepts_anonymous,
    qdrant_accepts_key,
    qdrant_anonymous_access,
    qdrant_base_url,
    qdrant_identifies,
    wait_for_memgraph,
    wait_for_qdrant,
)


def _publishes_on_all_interfaces(mapping: object) -> bool:
    """Whether one `ports` entry publishes without a host address.

    Long-form entries carry an explicit `host_ip`; short-form ones are
    `[host_ip:]host:container`, so a host address is present exactly when the
    mapping has two separators. A mapping bound to 0.0.0.0 or :: is public
    even though it names a host.
    """
    if isinstance(mapping, dict):
        # YAML parses `host_ip: null` (or a bare `host_ip:`) as None, which
        # Compose treats exactly like an omitted host: publish everywhere.
        declared = [
            "" if value is None else str(value).strip()
            for key, value in mapping.items()
            if key == "host_ip"
        ]
        return not declared or declared[0] in ("", "0.0.0.0", "::")  # noqa: S104 - matched against and never bound to
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
    return host_ip in ("0.0.0.0", "::", "*")  # noqa: S104 - matched against and never bound to


def _memgraph_credentials() -> tuple[str, str] | None:
    """The Memgraph login the stack creates and probes with, if configured.

    Stripped and required as a pair, the rule the ingestor applies, so the
    user `cgr daemon up` creates is the one the app then logs in as. Memgraph
    turns no authentication on for a username without a password.
    """
    username = (settings.MEMGRAPH_USERNAME or "").strip()
    password = (settings.MEMGRAPH_PASSWORD or "").strip()
    return (username, password) if username and password else None


def _compose_variable(project_dir: Path, name: str) -> str | None:
    """A compose-file variable as Compose resolves it: its environment first,
    then the .env file beside the compose file. Empty reads as unset."""
    if name in os.environ:
        value = os.environ[name]
    else:
        value = dotenv_values(project_dir / cs.COMPOSE_DOTENV_FILENAME).get(name)
    return value or None


def _bind_host(project_dir: Path) -> str:
    """The address the compose file publishes the stack's ports on, resolved
    as Compose resolves `${CGR_STACK_BIND_HOST:-127.0.0.1}`."""
    return _compose_variable(project_dir, cs.COMPOSE_BIND_HOST_VAR) or cs.LOOPBACK_HOST


def _bundled_qdrant_probe_host(bind: str) -> str:
    """Where this machine reaches the bundled Qdrant."""
    return cs.LOOPBACK_HOST if not bind or bind in cs.WILDCARD_BIND_HOSTS else bind


def _is_local_address(host: str) -> bool:
    """Whether `host` is an address of this machine.

    Binding a socket to an address succeeds only when one of this machine's
    interfaces has it.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)
    except OSError:
        return False
    for family, _, _, _, address in infos:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.bind((address[0], 0))
        except OSError:
            continue
        return True
    return False


def _addresses_of(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return set()
    return {str(address[0]) for _, _, _, _, address in infos}


def _names_published_qdrant(url: str | None, bind: str, port: int) -> bool:
    """Whether QDRANT_URL points at the port and an address Qdrant is published on.

    A wildcard bind publishes on every address of this machine; any other
    bind publishes on that address alone. A URL without a port means 6333,
    as in qdrant-client.
    """
    try:
        parts = urlsplit(url) if url else None
        url_port = parts.port if parts else None
    except ValueError:
        return False
    host = parts.hostname if parts else None
    if host is None or (url_port or cs.QDRANT_CLIENT_DEFAULT_PORT) != port:
        return False
    if not bind or bind in cs.WILDCARD_BIND_HOSTS:
        return _is_local_address(host)
    return bind in _addresses_of(host)


def _ps_containers(ps_output: str) -> list[JsonValue]:
    """The containers in `docker compose ps` JSON output: an object per
    line, or one array on older Compose."""
    containers: list[JsonValue] = []
    for line in ps_output.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        containers.extend(parsed if isinstance(parsed, list) else [parsed])
    return containers


def _bolt_endpoint(publisher: JsonValue) -> tuple[str, int] | None:
    """(bind address, port) of a `docker compose ps` publisher of Bolt."""
    if not isinstance(publisher, dict):
        return None
    port = publisher.get(cs.COMPOSE_PS_PUBLISHER_PORT_KEY)
    host = publisher.get(cs.COMPOSE_PS_PUBLISHER_HOST_KEY)
    if (
        publisher.get(cs.COMPOSE_PS_PUBLISHER_TARGET_KEY)
        == cs.MEMGRAPH_CONTAINER_BOLT_PORT
        and isinstance(port, int)
        and port
    ):
        return host if isinstance(host, str) else "", port
    return None


def _published_bolt(ps_output: str) -> list[tuple[str, int]]:
    """(bind address, port) of every Bolt publisher in `docker compose ps`
    JSON output: an object per line, or one array on older Compose."""
    endpoints: list[tuple[str, int]] = []
    for container in _ps_containers(ps_output):
        publishers = (
            container.get(cs.COMPOSE_PS_PUBLISHERS_KEY)
            if isinstance(container, dict)
            else None
        )
        for publisher in publishers if isinstance(publishers, list) else []:
            if (endpoint := _bolt_endpoint(publisher)) is not None:
                endpoints.append(endpoint)
    return endpoints


def _names_published(host: str, port: int, bind: str, published: int) -> bool:
    """Whether `host:port` reaches a port published on `bind:published`.

    A wildcard bind publishes on every address of this machine; any other
    bind publishes on that address alone.
    """
    if port != published:
        return False
    if not bind or bind in cs.WILDCARD_BIND_HOSTS:
        return _is_local_address(host)
    return bind in _addresses_of(host)


def _bundled_qdrant_api_key(bind: str, port: int) -> str | None:
    """The configured Qdrant key, if QDRANT_URL names the bundled Qdrant.

    QDRANT_API_KEY belongs to the server QDRANT_URL names, so the bundled
    Qdrant gets it only when that URL points where Compose publishes it. A
    key for another Qdrant, on this machine or elsewhere such as Qdrant
    Cloud, is never copied into the local container or sent to it.
    """
    key = settings.QDRANT_API_KEY
    return (
        key
        if key and _names_published_qdrant(settings.QDRANT_URL, bind, port)
        else None
    )


def _resolved_service(config: JsonValue, service: str) -> dict[str, JsonValue]:
    """One service's definition in `docker compose config` output."""
    services = config.get(cs.COMPOSE_SERVICES_KEY) if isinstance(config, dict) else None
    spec = services.get(service) if isinstance(services, dict) else None
    return spec if isinstance(spec, dict) else {}


def _resolved_environment(config: JsonValue, service: str) -> dict[str, JsonValue]:
    """One service's environment in `docker compose config` output.

    Compose renders `environment` as a mapping there, with a variable it could
    not resolve as null.
    """
    environment = _resolved_service(config, service).get(cs.COMPOSE_ENVIRONMENT_KEY)
    return environment if isinstance(environment, dict) else {}


def _published_port(
    config: JsonValue, service: str, target: int
) -> tuple[str, str] | None:
    """The host address and port Compose publishes one container port on.

    Compose renders every `ports` entry in long form there, with the
    interpolated host address and published port. Either is empty when the
    entry leaves it out; the port can also be a range or 0, which Docker
    resolves only when the container starts.
    """
    ports = _resolved_service(config, service).get(cs.COMPOSE_PORTS_KEY)
    for entry in ports if isinstance(ports, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get(cs.COMPOSE_PORT_TARGET_KEY) != target:
            continue
        host_ip = entry.get(cs.COMPOSE_PORT_HOST_IP_KEY)
        published = entry.get(cs.COMPOSE_PORT_PUBLISHED_KEY)
        return (
            host_ip if isinstance(host_ip, str) else "",
            "" if published is None else str(published),
        )
    return None


def _fixed_port(published: str) -> int | None:
    """The port number, unless Docker would pick the port at start."""
    if not (published.isascii() and published.isdecimal()):
        return None
    return int(published) or None


def _with_access(
    access: dict[str, cs.AnonymousAccess], answer: cs.AnonymousAccess
) -> list[str]:
    return [service for service, given in access.items() if given is answer]


def _container_ids(command: list[str], env: dict[str, str]) -> str | None:
    """What `docker compose ps --quiet` prints, or None if it fails."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding=root_cs.ENCODING_UTF8,
            timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class StackError(RuntimeError):
    pass


def _compose_failure(output: str, project_name: str) -> tuple[str, str | None]:
    """Why `docker compose up` failed, briefly, and which service failed.

    A port clash is named with the variable that moves it; otherwise the
    error lines are kept and the progress lines dropped. Output with no
    recognisable error line is reported whole rather than lost.
    """
    failed = re.search(
        cs.COMPOSE_FAILED_SERVICE.format(project=re.escape(project_name)), output
    )
    service = (
        (failed.group("endpoint") or failed.group("container")) if failed else None
    )
    if (clash := cs.COMPOSE_PORT_IN_USE.search(output)) is not None:
        detail = cs.ERR_PORT_IN_USE.format(
            address=clash.group("address"),
            variables=" or ".join(cs.SERVICE_PORT_VARIABLES.get(service or "", ()))
            or "its port",
        )
    else:
        errors = [
            line.strip()
            for line in output.splitlines()
            if cs.COMPOSE_ERROR_LINE.search(line)
        ]
        detail = "\n".join(dict.fromkeys(errors)) or output.strip()
    return detail, service


def _service_images(compose_file: Path) -> dict[str, str]:
    """Each service's `image` in a compose file; empty if it cannot be read."""
    try:
        compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        return {}
    return {
        str(name): image
        for name, spec in services.items()
        if isinstance(spec, dict) and isinstance(image := spec.get("image"), str)
    }


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
        self.memgraph_credentials = _memgraph_credentials()
        self.qdrant_port = qdrant_port
        self._qdrant_bind = _bind_host(self.home)
        self.qdrant_host = _bundled_qdrant_probe_host(self._qdrant_bind)
        self._qdrant_keys: dict[tuple[str, int], str | None] = {}
        # Each service's running containers just before `up -d`, or None when
        # that is unknown; see _started_here.
        self._containers_before_start: dict[str, str] | None = None
        self.project_name = project_name

    @property
    def qdrant_api_key(self) -> str | None:
        # Decided once per endpoint and only when asked: resolving a
        # QDRANT_URL host name can wait on DNS, which a status check should not.
        endpoint = (self._qdrant_bind, self.qdrant_port)
        if endpoint not in self._qdrant_keys:
            self._qdrant_keys[endpoint] = _bundled_qdrant_api_key(*endpoint)
        return self._qdrant_keys[endpoint]

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
            self._warn_if_images_float(target)
        return target

    def _warn_if_images_float(self, compose_file: Path) -> None:
        """Flag a rendered file running images the packaged stack now pins.

        The file is rendered once and never overwritten, so an install made
        before the pins keeps pulling `:latest` (issue #2409). It is the
        user's file, so this names the pinned images rather than rewriting
        it; an image the user pinned themselves, to any digest, is theirs.
        """
        rendered = _service_images(compose_file)
        packaged = _service_images(self.package_compose)
        floating = {
            service: image
            for service, image in rendered.items()
            if cs.IMAGE_DIGEST_MARKER not in image
            and cs.IMAGE_DIGEST_MARKER in packaged.get(service, "")
        }
        if not floating:
            return
        logger.warning(
            cs.WARN_COMPOSE_IMAGES_FLOATING.format(
                path=compose_file,
                pins="; ".join(
                    cs.IMAGE_PIN_PAIR.format(
                        service=service, floating=image, pinned=packaged[service]
                    )
                    for service, image in floating.items()
                ),
            )
        )

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
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
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
            # The file is user-owned, so `ports` can be any YAML value; a
            # scalar (`ports: 8080`) is not a mapping list and iterating it
            # would crash the start and status paths over a file Compose
            # itself would reject.
            ports = spec.get("ports")
            if not isinstance(ports, list):
                continue
            for mapping in ports:
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

    def _auth_variables(self) -> dict[str, dict[str, str]]:
        """Container variables to set per service, from the configured secrets."""
        wanted: dict[str, dict[str, str]] = {}
        if self.memgraph_credentials:
            username, password = self.memgraph_credentials
            wanted[cs.SERVICE_MEMGRAPH] = {
                cs.ENV_MEMGRAPH_USER: username,
                cs.ENV_MEMGRAPH_PASSWORD: password,
            }
        if self.qdrant_api_key:
            wanted[cs.SERVICE_QDRANT] = {cs.ENV_QDRANT_API_KEY: self.qdrant_api_key}
        return wanted

    def _verify_resolved_auth(self, config: dict[str, JsonValue]) -> None:
        """Refuse to start containers with credentials the app does not use.

        Asks Compose what each service would receive rather than reading the
        file, because a value written into it, a file rendered before
        credential support, or an `.env` file beside it (which Compose reads
        for a variable missing from its environment) can each diverge from
        the settings. Values are compared, never logged.
        """
        configured = {
            name: value
            for variables in self._auth_variables().values()
            for name, value in variables.items()
        }
        mismatched = [
            f"{service}: {name}"
            for service, names in cs.STACK_AUTH_VARIABLES.items()
            for name in names
            if _resolved_environment(config, service).get(name) != configured.get(name)
        ]
        if mismatched:
            raise StackError(
                cs.ERR_COMPOSE_AUTH_MISMATCH.format(
                    variables=", ".join(mismatched), path=self.compose_file
                )
            )

    def locate_published_qdrant(self) -> None:
        """Ask Compose where Qdrant is published before checking a running stack.

        Only a configured Qdrant key needs the answer: it decides the key and
        where the running stack's key checks go, which would otherwise probe
        a guess and could check another service in the bundled Qdrant's
        place. Without a compose file or Docker there is no stack of this
        project to locate, and the guess stands.
        """
        if (
            settings.QDRANT_API_KEY
            and self.compose_file.exists()
            and shutil.which(cs.DOCKER_BIN) is not None
        ):
            self._adopt_published_qdrant(self._resolved_config())

    def _adopt_published_qdrant(self, config: dict[str, JsonValue]) -> bool:
        """Probe Qdrant where Compose actually publishes it.

        The bind address and port can come from the environment, the .env
        beside the compose file, files named in COMPOSE_ENV_FILES and more;
        Compose's resolved configuration is the one answer that covers them.
        A port Docker picks at start is refused before starting: the health
        check could only probe the wrong port, and QDRANT_URL cannot follow
        one that changes whenever the container is recreated. Returns whether
        the endpoint changed the Qdrant key decision.
        """
        target = cs.QDRANT_CONTAINER_HTTP_PORT
        published = _published_port(config, cs.SERVICE_QDRANT, target)
        if published is None:
            return False
        key_before = self.qdrant_api_key
        host_ip, host_port = published
        port = _fixed_port(host_port)
        if port is None:
            raise StackError(
                cs.ERR_QDRANT_PORT_NOT_FIXED.format(
                    target=target,
                    published=repr(host_port) if host_port else cs.COMPOSE_PORT_UNSET,
                    path=self.compose_file,
                )
            )
        self._qdrant_bind = host_ip
        self.qdrant_host = _bundled_qdrant_probe_host(host_ip)
        self.qdrant_port = port
        return self.qdrant_api_key != key_before

    def _resolved_config(
        self, failure: str = cs.ERR_AUTH_NOT_VERIFIED
    ) -> dict[str, JsonValue]:
        """The project as `docker compose config` resolves it, or StackError.

        JSON is asked for first because it is unambiguous; plain `config`,
        which prints YAML, covers a Compose release without `--format`. Both
        are read as YAML, a superset of JSON, which also covers a release that
        prints YAML despite the flag. Unchecked credentials could leave the
        stack open or locked to a key the app lacks, so a project neither form
        can render is not started; `failure` words the error for a caller
        that is not starting it.
        """
        detail = ""
        for args in cs.COMPOSE_CONFIG_ATTEMPTS:
            result = subprocess.run(
                self._compose_cmd(*args),
                capture_output=True,
                text=True,
                encoding=root_cs.ENCODING_UTF8,
                timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
                check=False,
                env=self._compose_env(),
            )
            detail = (result.stderr or "").strip()
            if result.returncode != 0:
                continue
            try:
                config = yaml.safe_load(result.stdout or "")
            except yaml.YAMLError:
                continue
            if isinstance(config, dict):
                return config
        raise StackError(failure.format(detail=detail))

    def locate_running_qdrant(self) -> bool:
        """Point the Qdrant probes at this project's Qdrant; whether it runs.

        Only Docker knows who owns a local port. A compose file proves the
        stack was set up, not that it runs, and once it stops any other
        process can answer on its port. So the endpoint comes from Compose's
        resolved configuration, as for `up`, and `docker compose ps`, as for
        telling apart the containers a start created, says whether this
        project's own Qdrant container is running. StackError when Docker or
        Compose cannot tell, or Compose publishes Qdrant on no fixed port.
        """
        if shutil.which(cs.DOCKER_BIN) is None:
            raise StackError(cs.ERR_DOCKER_NOT_INSTALLED)
        config = self._resolved_config(cs.ERR_COMPOSE_CONFIG_FAILED)
        target = cs.QDRANT_CONTAINER_HTTP_PORT
        if _published_port(config, cs.SERVICE_QDRANT, target) is None:
            raise StackError(
                cs.ERR_QDRANT_NOT_PUBLISHED.format(
                    path=self.compose_file, target=target
                )
            )
        self._adopt_published_qdrant(config)
        running = _container_ids(
            self._compose_cmd(*cs.COMPOSE_PS_RUNNING_ARGS, cs.SERVICE_QDRANT),
            self._compose_env(),
        )
        if running is None:
            raise StackError(cs.ERR_COMPOSE_PS_FAILED)
        return bool(running)

    def runs_configured_memgraph(self) -> bool:
        """Whether this project's running Memgraph is the one the app uses.

        Compose's resolved configuration cannot say: the port mapping
        interpolates MEMGRAPH_PORT, so it follows whatever port the caller
        set. Docker's publishers of the running container are where the
        stack's Memgraph actually listens (issue #2878). StackError when
        Compose cannot tell.
        """
        try:
            result = subprocess.run(
                self._compose_cmd(
                    *cs.COMPOSE_PS_RUNNING_JSON_ARGS, cs.SERVICE_MEMGRAPH
                ),
                capture_output=True,
                text=True,
                encoding=root_cs.ENCODING_UTF8,
                timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
                check=False,
                env=self._compose_env(),
            )
        except OSError as e:
            raise StackError(cs.ERR_COMPOSE_PS_FAILED) from e
        if result.returncode != 0:
            raise StackError(cs.ERR_COMPOSE_PS_FAILED)
        return any(
            _names_published(self.memgraph_host, self.memgraph_port, bind, port)
            for bind, port in _published_bolt(result.stdout)
        )

    def raise_if_auth_not_enforced(self) -> None:
        """Refuse a running stack whose authentication differs from the settings.

        Containers take their environment when they are created, so a stack
        that was up before credentials were configured stays open, and one
        started with an earlier Qdrant key keeps it. The health checks cannot
        tell: /readyz needs no key, and a Memgraph with no users accepts any
        login. A Memgraph that rejects the login already fails its probe.
        Carrying on would leave the data open while the settings say it is
        protected, as the start path refuses to do.
        """
        self._raise_if_open(self._services_accepting_anonymous())
        self._raise_if_qdrant_rejects_key()

    def _raise_if_open(self, open_services: list[str]) -> None:
        if open_services:
            raise StackError(
                cs.ERR_STACK_ACCEPTS_ANONYMOUS.format(services=", ".join(open_services))
            )

    def _raise_if_qdrant_rejects_key(self) -> None:
        # The probe is plain http, so like the app it sends the key only when
        # QDRANT_ALLOW_INSECURE_API_KEY allows that.
        api_key = self.qdrant_api_key
        if (
            api_key
            and settings.QDRANT_ALLOW_INSECURE_API_KEY
            and not qdrant_accepts_key(self.qdrant_port, api_key, host=self.qdrant_host)
        ):
            raise StackError(cs.ERR_QDRANT_REJECTS_KEY)

    def _services_accepting_anonymous(self) -> list[str]:
        """The services that accept connections without their configured credentials."""
        open_services: list[str] = []
        if self.memgraph_credentials and memgraph_accepts_anonymous(
            self.memgraph_host, self.memgraph_port
        ):
            open_services.append(cs.SERVICE_MEMGRAPH)
        if self.qdrant_api_key and qdrant_accepts_anonymous(
            self.qdrant_port, host=self.qdrant_host
        ):
            open_services.append(cs.SERVICE_QDRANT)
        return open_services

    def _compose_env(self) -> dict[str, str]:
        """The environment `docker compose up` runs in.

        Settings, which also read the .env file, are the one source of the
        credentials: an inherited MEMGRAPH_PASSWORD or Qdrant key is dropped
        rather than creating a login the app does not know.
        """
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in cs.STACK_AUTH_ENV_VARS
        }
        for variables in self._auth_variables().values():
            env.update(variables)
        return env

    def warn_if_ports_are_public(self) -> None:
        """Warn about public port bindings independently of the start path.

        `ensure_compose_file` only runs when the stack is being started, so a
        stack that is already up would keep its pre-#1012 exposure silent
        (issue #1380). Callers that never render the file go through here.
        """
        if self.compose_file.exists():
            self._warn_if_ports_are_public(self.compose_file)

    def check_docker(self) -> None:
        if shutil.which(cs.DOCKER_BIN) is None:
            raise StackError(cs.ERR_DOCKER_NOT_INSTALLED)
        info = subprocess.run(
            [cs.DOCKER_BIN, "info"],
            capture_output=True,
            text=True,
            encoding=root_cs.ENCODING_UTF8,
            timeout=cs.DEFAULT_STATUS_TIMEOUT_S,
            check=False,
        )
        if info.returncode != 0:
            raise StackError(cs.ERR_DOCKER_DAEMON_DOWN)
        compose = subprocess.run(
            [cs.DOCKER_BIN, cs.DOCKER_COMPOSE_SUBCOMMAND, "version"],
            capture_output=True,
            text=True,
            encoding=root_cs.ENCODING_UTF8,
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
        config = self._resolved_config()
        if self._adopt_published_qdrant(config):
            # The key follows the published endpoint, so Compose resolves the
            # project again with the environment `up` will run in.
            config = self._resolved_config()
        self._verify_resolved_auth(config)
        # A service already running open was not started here, so like a stack
        # found fully up it is refused and left running. One that is running
        # but does not answer yet is told apart later by its container, which
        # is only this start's to stop if `up -d` created or started it.
        self._raise_if_open(self._services_accepting_anonymous())
        self._containers_before_start = self._running_containers()
        logger.info(cs.MSG_STARTING_STACK)
        # `up -d` that fails or is cut short may already have started some
        # containers, and the credential check in wait_healthy never runs.
        try:
            result = subprocess.run(
                self._compose_cmd("up", "-d"),
                capture_output=True,
                text=True,
                encoding=root_cs.ENCODING_UTF8,
                timeout=timeout,
                check=False,
                env=self._compose_env(),
            )
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            self._stop_if_left_open()
            raise
        if result.returncode != 0:
            output = "\n".join(
                part for part in (result.stdout.strip(), result.stderr.strip()) if part
            )
            logger.debug(cs.MSG_COMPOSE_UP_OUTPUT.format(output=output))
            detail, service = _compose_failure(output, self.project_name)
            if service == cs.SERVICE_LAB and self._core_services_running():
                logger.warning(cs.WARN_LAB_NOT_STARTED.format(detail=detail))
                return
            if service is not None:
                detail = cs.ERR_SERVICE_NOT_STARTED.format(
                    service=cs.SERVICE_DISPLAY_NAMES.get(service, service),
                    detail=detail,
                )
            self._stop_if_left_open()
            raise StackError(cs.ERR_STACK_START_FAILED.format(detail=detail))

    def _core_services_running(self) -> bool:
        """Whether Memgraph and Qdrant each have a running container."""
        env = self._compose_env()
        return all(
            _container_ids(self._compose_cmd(*cs.COMPOSE_PS_RUNNING_ARGS, service), env)
            for service in cs.CORE_SERVICES
        )

    def down(self, timeout: float = cs.DEFAULT_DOCKER_TIMEOUT_S) -> None:
        self._stop_containers("down", timeout)

    def stop(
        self, *services: str, timeout: float = cs.DEFAULT_DOCKER_TIMEOUT_S
    ) -> None:
        """Stop the containers of `services`, or all, keeping logs and volumes."""
        self._stop_containers(cs.COMPOSE_STOP_COMMAND, timeout, *services)

    def _stop_containers(self, command: str, timeout: float, *services: str) -> None:
        if not self.compose_file.exists():
            return
        if shutil.which(cs.DOCKER_BIN) is None:
            raise StackError(cs.ERR_DOCKER_NOT_INSTALLED)
        logger.info(cs.MSG_STOPPING_STACK)
        result = subprocess.run(
            self._compose_cmd(command, *services),
            capture_output=True,
            text=True,
            encoding=root_cs.ENCODING_UTF8,
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
        # Every caller has just started the stack, after `up` refused one that
        # was already open. A start whose wait fails or is interrupted never
        # reaches the credential check below, so an open service it started
        # is found and stopped here instead.
        try:
            self._wait_for_services(timeout)
        except (StackError, KeyboardInterrupt):
            self._stop_if_left_open()
            raise
        # Ready is not the same as protected: prove the started containers
        # enforce the credentials rather than rely on Compose having
        # recreated each one whose environment changed. An open service is
        # stopped rather than left running; a Qdrant that rejects the key is
        # protected by another one, so it is refused but left running.
        open_services = self._services_accepting_anonymous()
        if open_services:
            self._stop_open_services(open_services)
        self._raise_if_open(open_services)
        self._raise_if_qdrant_rejects_key()

    def _wait_for_services(self, timeout: float) -> None:
        logger.info(
            cs.MSG_WAITING_FOR_HEALTH.format(
                service=cs.SERVICE_MEMGRAPH,
                host=self.memgraph_host,
                port=self.memgraph_port,
            )
        )
        if not wait_for_memgraph(
            self.memgraph_host,
            self.memgraph_port,
            timeout,
            credentials=self.memgraph_credentials,
        ):
            self._raise_if_memgraph_rejects_credentials()
            raise StackError(
                cs.ERR_STACK_NOT_HEALTHY.format(
                    service=cs.SERVICE_MEMGRAPH, timeout=timeout
                )
            )
        logger.info(
            cs.MSG_WAITING_FOR_HEALTH.format(
                service=cs.SERVICE_QDRANT,
                host=self.qdrant_host,
                port=self.qdrant_port,
            )
        )
        if not wait_for_qdrant(self.qdrant_port, timeout, host=self.qdrant_host):
            raise StackError(
                cs.ERR_STACK_NOT_HEALTHY.format(
                    service=cs.SERVICE_QDRANT, timeout=timeout
                )
            )

    def _stop_if_left_open(self) -> None:
        """Stop the open services a failed or interrupted start left running.

        A container `up -d` started can still be initialising, so a service
        that does not answer yet is asked again until it answers or the check
        times out: one that turns out open is stopped, a protected one that is
        merely slow to start is left running. Either way, the error that ended
        the start is the one reported.
        """
        deadline = time.monotonic() + cs.STARTED_SERVICES_CHECK_TIMEOUT_S
        pending = self._services_with_credentials()
        waiting = False
        while pending:
            access = {service: self._anonymous_access(service) for service in pending}
            if open_services := _with_access(access, cs.AnonymousAccess.ALLOWED):
                logger.warning(
                    cs.WARN_START_LEFT_STACK_OPEN.format(
                        services=", ".join(open_services)
                    )
                )
                self._stop_open_services(open_services)
            pending = _with_access(access, cs.AnonymousAccess.NO_ANSWER)
            if not pending or time.monotonic() >= deadline:
                return
            if not waiting:
                logger.info(
                    cs.MSG_CHECKING_STARTED_SERVICES.format(
                        timeout=cs.STARTED_SERVICES_CHECK_TIMEOUT_S,
                        services=", ".join(pending),
                    )
                )
                waiting = True
            time.sleep(cs.DEFAULT_HEALTH_INTERVAL_S)

    def _services_with_credentials(self) -> list[str]:
        services: list[str] = []
        if self.memgraph_credentials:
            services.append(cs.SERVICE_MEMGRAPH)
        if self.qdrant_api_key:
            services.append(cs.SERVICE_QDRANT)
        return services

    def _anonymous_access(self, service: str) -> cs.AnonymousAccess:
        if service == cs.SERVICE_MEMGRAPH:
            return memgraph_anonymous_access(self.memgraph_host, self.memgraph_port)
        return qdrant_anonymous_access(self.qdrant_port, host=self.qdrant_host)

    def _stop_open_services(self, open_services: list[str]) -> None:
        started, found_running = self._started_here(open_services)
        if found_running:
            logger.warning(
                cs.WARN_OPEN_SERVICES_LEFT_RUNNING.format(
                    services=", ".join(found_running)
                )
            )
        if not started:
            return
        # Whatever stops the stop, the error that led here is the one to report.
        try:
            self.stop(*started)
        except (StackError, subprocess.TimeoutExpired, OSError) as e:
            logger.warning(
                cs.WARN_OPEN_SERVICES_NOT_STOPPED.format(
                    services=", ".join(started), detail=e
                )
            )

    def _started_here(self, services: list[str]) -> tuple[list[str], list[str]]:
        """Split services into those this start started and those it found running.

        A service whose container is the one that was running before `up -d`
        was left as it was, so it is another invocation's to stop. When either
        list of containers is unknown, every service counts as started here,
        so an open one is stopped rather than left running.
        """
        before = self._containers_before_start
        now = self._running_containers() if before else None
        if not before or now is None:
            return services, []
        found_running = [
            service
            for service in services
            if before.get(service) and now.get(service) == before[service]
        ]
        return [s for s in services if s not in found_running], found_running

    def _running_containers(self) -> dict[str, str] | None:
        """The running containers of each service with credentials, as Compose
        lists them, or None when it cannot."""
        containers: dict[str, str] = {}
        for service in self._services_with_credentials():
            ids = _container_ids(
                self._compose_cmd(*cs.COMPOSE_PS_RUNNING_ARGS, service),
                self._compose_env(),
            )
            if ids is None:
                return None
            containers[service] = ids
        return containers

    def _raise_if_memgraph_rejects_credentials(self) -> None:
        """Name a rejected login instead of reporting Memgraph as down.

        Memgraph stores its user in the data volume and never changes an
        existing password from MEMGRAPH_PASSWORD, so after that setting
        changes every probe is refused by a Memgraph that is otherwise fine.
        """
        if self.memgraph_credentials and memgraph_rejects_credentials(
            self.memgraph_host, self.memgraph_port, self.memgraph_credentials
        ):
            raise StackError(cs.ERR_MEMGRAPH_REJECTS_CREDENTIALS)

    def status(self) -> StackStatus:
        memgraph_ok = wait_for_memgraph(
            self.memgraph_host,
            self.memgraph_port,
            timeout=0.1,
            interval=0.0,
            credentials=self.memgraph_credentials,
        )
        qdrant_ok = wait_for_qdrant(
            self.qdrant_port, timeout=0.1, interval=0.0, host=self.qdrant_host
        )
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
            qdrant_endpoint=f"{self.qdrant_host}:{self.qdrant_port}",
        )

    def ensure_running(self) -> StackStatus:
        self.locate_published_qdrant()
        current = self.status()
        if current.state == cs.StackState.RUNNING:
            logger.info(cs.MSG_STACK_ALREADY_RUNNING)
            # The start path warns via ensure_compose_file; a stack that is
            # already up never reaches it, and its long-lived compose file is
            # exactly the profile of a pre-#1012 public binding (issue #1380).
            self.warn_if_ports_are_public()
            self.raise_if_auth_not_enforced()
            return current
        # Starting it again would not help, and waiting for health would only
        # delay the same answer.
        self._raise_if_memgraph_rejects_credentials()
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


def bundled_qdrant_url(home: Path | None = None) -> str | None:
    """Where the Qdrant `cgr daemon up` runs, when it takes the app's writes.

    None unless the stack was set up here (its compose file exists), Docker
    Compose reports this project's Qdrant container running, and what
    answers where Compose publishes it identifies as Qdrant and takes a
    data request without a key. The embeddings carry the indexed code, so
    an endpoint whose owner is not proven gets none of them: with the stack
    stopped, any process can answer on its port (CWE-200). QDRANT_API_KEY is
    never offered: it belongs to the server QDRANT_URL names, and the stack
    only configures its Qdrant with it when QDRANT_URL points there. A Qdrant
    that refuses anonymous requests is logged and left alone, since every
    write to it would fail (issue #2355).
    """
    manager = StackManager(home=home)
    if not manager.compose_file.exists():
        return None
    path = settings.QDRANT_DB_PATH
    try:
        running = manager.locate_running_qdrant()
    except (StackError, subprocess.TimeoutExpired, OSError) as e:
        logger.info(ls.QDRANT_BUNDLED_UNVERIFIED.format(detail=e, path=path))
        return None
    if not running:
        logger.info(ls.QDRANT_BUNDLED_NOT_RUNNING.format(path=path))
        return None
    host, port = manager.qdrant_host, manager.qdrant_port
    url = qdrant_base_url(host, port)
    # The stack's Qdrant holds its own graph's vectors, keyed by that graph's
    # node ids: another Memgraph's sync would mix its vectors in, and its
    # `--clean` would drop the stack graph's collection (issue #2878).
    try:
        same_graph = manager.runs_configured_memgraph()
    except (StackError, subprocess.TimeoutExpired) as e:
        logger.info(ls.QDRANT_BUNDLED_UNVERIFIED.format(detail=e, path=path))
        return None
    if not same_graph:
        memgraph = f"{manager.memgraph_host}:{manager.memgraph_port}"
        logger.info(
            ls.QDRANT_BUNDLED_OTHER_MEMGRAPH.format(
                url=url, memgraph=memgraph, path=path
            )
        )
        return None
    timeout = cs.BUNDLED_QDRANT_PROBE_TIMEOUT_S
    match qdrant_anonymous_access(port, timeout=timeout, host=host):
        case cs.AnonymousAccess.ALLOWED if qdrant_identifies(
            port, timeout=timeout, host=host
        ):
            return url
        case cs.AnonymousAccess.ALLOWED:
            logger.warning(ls.QDRANT_BUNDLED_NOT_QDRANT.format(url=url, path=path))
            return None
        case cs.AnonymousAccess.REFUSED:
            logger.warning(ls.QDRANT_BUNDLED_WANTS_KEY.format(url=url, path=path))
            return None
        case _:
            return None
