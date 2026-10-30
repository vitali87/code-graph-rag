"""The `cgr trace` command group: runtime call-trace ingestion."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from .. import cli_help as ch
from . import constants as tcs

cli = typer.Typer(
    help=ch.CMD_TRACE_GROUP,
    short_help=ch.CMD_TRACE_GROUP,
    epilog=ch.EPILOG_TRACE,
    no_args_is_help=True,
    add_completion=False,
)

# `git rev-parse HEAD` answers at once; the bound only keeps a hung git from
# holding up the conversion for a warning.
_GIT_HEAD_TIMEOUT_S = 30

_InputFile = Annotated[Path, typer.Argument(exists=True, dir_okay=False)]
_RepoPath = Annotated[
    Path | None,
    typer.Option(
        "--repo-path", exists=True, file_okay=False, help=ch.HELP_TRACE_REPO_PATH
    ),
]
_Output = Annotated[
    Path | None,
    typer.Option("--output", "-o", dir_okay=False, help=ch.HELP_TRACE_OUTPUT),
]
_Workload = Annotated[
    str | None, typer.Option("--workload", help=ch.HELP_TRACE_WORKLOAD)
]
_Language = Annotated[
    str | None, typer.Option("--language", help=ch.HELP_TRACE_LANGUAGE)
]
_PathMap = Annotated[
    list[str] | None, typer.Option("--path-map", help=ch.HELP_TRACE_PATH_MAP)
]
_BuildId = Annotated[
    str | None, typer.Option("--build-id", help=ch.HELP_TRACE_BUILD_ID)
]
_Service = Annotated[str | None, typer.Option("--service", help=ch.HELP_TRACE_SERVICE)]
_Label = Annotated[str | None, typer.Option("--label", help=ch.HELP_TRACE_LABEL)]
_Commit = Annotated[str | None, typer.Option("--commit", help=ch.HELP_TRACE_COMMIT)]


@cli.command(
    "ingest",
    help=ch.CMD_TRACE_INGEST,
    short_help=ch.CMD_TRACE_INGEST,
    epilog=ch.EXAMPLES_TRACE_INGEST,
)
def ingest_cmd(
    trace_file: _InputFile,
    repo_path: Path = typer.Option(
        ...,
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_TRACE_REPO_PATH,
    ),
    project_name: str | None = typer.Option(
        None, "--project-name", "--project", "-p", help=ch.HELP_TRACE_PROJECT_NAME
    ),
) -> None:
    from ..cli_runtime import connect_graph
    from ..config import settings
    from ..utils.path_utils import derive_project_name
    from .ingest import ingest_trace
    from .records import TraceFormatError

    resolved_project = project_name or derive_project_name(repo_path)
    try:
        with connect_graph(batch_size=settings.resolve_batch_size(None)) as ingestor:
            summary = ingest_trace(
                trace_path=trace_file,
                ingestor=ingestor,
                repo_path=repo_path,
                project_name=resolved_project,
            )
    except TraceFormatError as e:
        logger.error(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)

    typer.echo(
        f"records:          {summary.records}\n"
        f"edges written:    {summary.edges}\n"
        f"confirmed static: {summary.confirmed_static}\n"
        f"static missed:    {summary.static_missed}\n"
        f"unresolved:       {summary.unresolved}"
    )
    for reason, count in sorted(summary.resolution.unresolved.items()):
        typer.echo(f"  unresolved[{reason}]: {count}")


class _ConvertUsageError(Exception):
    """A convert invocation missing an option the detected format requires."""


def _convert_for_language(
    profile_file: Path,
    repo_path: Path | None,
    resolved_output: Path,
    workload: str | None,
    language: str,
) -> int:
    """Convert with an explicit ``--language`` override, bypassing sniffing.

    Rust pprof profiles are gzipped protobufs indistinguishable from Go's by
    magic bytes, so an explicit override is the only way to select the Rust
    demangler.
    """

    if language == tcs.TRACE_LANGUAGE_RUST:
        from .rust_pprof import convert_rust_pprof

        return convert_rust_pprof(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )
    raise _ConvertUsageError(
        ch.ERR_TRACE_CONVERT_BAD_LANGUAGE.format(
            language=language, supported=tcs.TRACE_LANGUAGE_RUST
        )
    )


def _require_repo(repo_path: Path | None) -> Path:
    """Return ``repo_path`` or raise the usage error when a format needs it."""
    if repo_path is None:
        raise _ConvertUsageError(ch.ERR_TRACE_CONVERT_NEEDS_REPO)
    return repo_path


def _parse_key_value(value: str, error_template: str) -> tuple[str, str]:
    """Split a ``KEY=VALUE`` option; a usage error when it lacks ``=`` or a key.

    An empty key (``=value``) would re-anchor every path or select no label, so
    it is rejected rather than silently matching everything.
    """
    key, sep, val = value.partition("=")
    if not sep or not key:
        raise _ConvertUsageError(error_template.format(value=value))
    return key, val


def _warn_commit_mismatch(commit: str | None, repo_path: Path) -> None:
    """Warn when the profiled binary's commit differs from the repo HEAD."""
    if commit is None:
        return
    import shutil
    import subprocess

    from .. import constants as cs

    if (git := shutil.which(cs.SHELL_CMD_GIT)) is None:
        return
    try:
        result = subprocess.run(
            [git, "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=False,
            timeout=_GIT_HEAD_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    head = result.stdout.strip()
    if head and not head.startswith(commit) and not commit.startswith(head):
        message = ch.MSG_TRACE_COMMIT_MISMATCH.format(profiled=commit, head=head)
        logger.warning(message)
        typer.secho(message, fg="yellow", err=True)


def _convert_ebpf(
    profile_file: Path,
    repo_path: Path | None,
    resolved_output: Path,
    workload: str | None,
    language: str | None,
    path_map: tuple[str, ...],
    build_id: str | None,
    service: str | None,
    label: str | None,
    commit: str | None,
) -> int:
    """Convert an eBPF-profiler pprof profile with re-anchoring and filtering."""
    from .ebpf_pprof import FrameResolution, SampleLabeling, convert_ebpf_pprof

    repo = _require_repo(repo_path)
    mappings = [
        _parse_key_value(entry, ch.ERR_TRACE_CONVERT_BAD_PATH_MAP) for entry in path_map
    ]
    service_pair = (
        _parse_key_value(service, ch.ERR_TRACE_CONVERT_BAD_SERVICE) if service else None
    )
    _warn_commit_mismatch(commit, repo)
    return convert_ebpf_pprof(
        profile_file,
        repo_root=repo,
        output=resolved_output,
        frames=FrameResolution(
            language=language or tcs.TRACE_LANGUAGE_GO,
            path_map=mappings,
            build_id=build_id,
        ),
        labels=SampleLabeling(
            service=service_pair, workload_label=label, workload=workload
        ),
    )


# A merged fleet pprof profile is aggregated and small; cap the download so a
# malicious or runaway endpoint cannot exhaust memory or run unattended forever.
_MAX_PROFILE_BYTES = 256 * 1024 * 1024


def _redact_url(url: str) -> str:
    """A log-safe form of a URL: scheme, host, and path, without userinfo/query."""
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.hostname or "", parts.path, "", "")
    )


def _download_pprof(url: str, headers: tuple[str, ...], timeout: float) -> bytes:
    """GET a pprof profile over HTTP(S); a usage error on a bad URL or failure.

    Redirects are followed but never carry credentials across them and must stay
    on http(s); the body is read under a size cap. ``urllib.error.URLError`` is
    an ``OSError`` subclass, so one ``except OSError`` covers both.

    ``timeout`` bounds the whole download, not each socket read (#2263): a
    server that trickles a byte at a time never trips a per-read timeout, in
    the headers or the body. The fetch runs on a daemon thread that is
    abandoned at the deadline; the process exit that follows the usage error
    closes its socket.
    """
    import threading
    import urllib.parse

    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise _ConvertUsageError(ch.ERR_TRACE_PULL_BAD_URL.format(url=_redact_url(url)))

    # Any exception the fetch raises is re-raised here, so an error is never
    # mistaken for a timeout or lost on the worker thread.
    outcome: list[bytes | Exception] = []

    def fetch() -> None:
        try:
            outcome.append(_fetch_pprof(url, headers, timeout))
        except Exception as e:  # noqa: BLE001  # re-raised on the caller
            outcome.append(e)

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(timeout)
    if not outcome:
        raise _ConvertUsageError(
            ch.ERR_TRACE_PULL_TIMED_OUT.format(url=_redact_url(url), timeout=timeout)
        )
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    return result


def _fetch_pprof(url: str, headers: tuple[str, ...], timeout: float) -> bytes:
    import urllib.request

    request = urllib.request.Request(url)  # noqa: S310 - scheme checked above
    for header in headers:
        key, value = _parse_key_value(header, ch.ERR_TRACE_PULL_BAD_HEADER)
        # Every caller-supplied header goes in unredirected: it was meant for this
        # endpoint, and any of them (Authorization, X-Api-Key, X-Scope-OrgID, ...)
        # may be a credential, so urllib must not resend it to a redirect target.
        request.add_unredirected_header(key, value)
    # An opener with only HTTP(S) handlers: a redirect to file:// or ftp:// has no
    # handler and fails, so a redirect cannot bypass the scheme check above.
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPHandler,
        urllib.request.HTTPSHandler,
        urllib.request.HTTPRedirectHandler,
        urllib.request.HTTPErrorProcessor,
        urllib.request.HTTPDefaultErrorHandler,
    ):
        opener.add_handler(handler())
    try:
        with opener.open(request, timeout=timeout) as response:
            data = response.read(_MAX_PROFILE_BYTES + 1)
    except OSError as e:
        raise _ConvertUsageError(
            ch.ERR_TRACE_PULL_FAILED.format(url=_redact_url(url), error=e)
        ) from e
    if len(data) > _MAX_PROFILE_BYTES:
        raise _ConvertUsageError(
            ch.ERR_TRACE_PULL_TOO_LARGE.format(url=_redact_url(url))
        )
    return data


def _convert_profile(
    profile_file: Path,
    repo_path: Path | None,
    resolved_output: Path,
    include: str | None,
    workload: str | None,
    language: str | None = None,
) -> int:
    """Dispatch by profile format and write records; returns the record count.

    Raises ``_ConvertUsageError`` when the format needs an option the caller
    did not supply, and ``TraceFormatError``/``OSError``/``JSONDecodeError``
    for unreadable or malformed profiles.
    """
    import json

    if language is not None:
        return _convert_for_language(
            profile_file, repo_path, resolved_output, workload, language
        )

    if profile_file.suffix == ".addrs":
        # C/C++ instrumented address traces need symbolisation plus the repo.
        from .instrumented import convert_instrumented

        return convert_instrumented(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    if profile_file.name.endswith(tcs.TRACE_XDEBUG_SUFFIXES):
        # Checked before the gzip sniff below: a zlib-enabled Xdebug writes
        # gzip-compressed .xt.gz traces, which are not pprof profiles.
        from .xdebug import convert_xdebug_trace

        return convert_xdebug_trace(
            profile_file, output=resolved_output, workload=workload
        )

    with profile_file.open("rb") as fh:
        magic = fh.read(2)

    if magic == b"\x1f\x8b" or profile_file.suffix == ".pprof":
        # Go pprof CPU profiles are gzipped protobufs.
        from .pprof import convert_pprof

        return convert_pprof(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    raw = json.loads(profile_file.read_text(encoding="utf-8"))
    looks_like_cpuprofile = (isinstance(raw, dict) and "nodes" in raw) or (
        profile_file.suffix == ".cpuprofile"
    )
    if looks_like_cpuprofile:
        # V8 cpuprofile: frames carry file URLs, so scoping needs the repo.
        from .cpuprofile import convert_cpuprofile

        return convert_cpuprofile(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    # dotnet-trace speedscope: frames carry no paths, so scoping needs
    # namespace prefixes.
    from .speedscope import convert_speedscope

    prefixes = tuple(p.strip() for p in (include or "").split(",") if p.strip())
    if not prefixes:
        raise _ConvertUsageError(ch.ERR_TRACE_CONVERT_NEEDS_INCLUDE)
    return convert_speedscope(
        profile_file, output=resolved_output, include=prefixes, workload=workload
    )


@cli.command(
    "convert",
    help=ch.CMD_TRACE_CONVERT,
    short_help=ch.CMD_TRACE_CONVERT,
    epilog=ch.EXAMPLES_TRACE_CONVERT,
)
def convert_cmd(
    profile_file: _InputFile,
    repo_path: _RepoPath = None,
    output: _Output = None,
    include: str | None = typer.Option(None, "--include", help=ch.HELP_TRACE_INCLUDE),
    workload: _Workload = None,
    language: _Language = None,
    fmt: str | None = typer.Option(None, "--format", help=ch.HELP_TRACE_FORMAT),
    path_map: _PathMap = None,
    build_id: _BuildId = None,
    service: _Service = None,
    label: _Label = None,
    commit: _Commit = None,
) -> None:

    resolved_output = output or Path(tcs.TRACE_DEFAULT_OUTPUT)
    try:
        if fmt == "ebpf":
            count = _convert_ebpf(
                profile_file,
                repo_path,
                resolved_output,
                workload,
                language,
                tuple(path_map or ()),
                build_id,
                service,
                label,
                commit,
            )
        elif fmt is not None:
            raise _ConvertUsageError(ch.ERR_TRACE_CONVERT_BAD_FORMAT.format(format=fmt))
        else:
            count = _convert_profile(
                profile_file, repo_path, resolved_output, include, workload, language
            )
    # TraceFormatError subclasses ValueError, so ValueError covers it (and the
    # malformed-number / non-UTF-8 cases) without listing it redundantly.
    except (OSError, ValueError, _ConvertUsageError) as e:
        logger.error(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(f"call records written: {count} -> {resolved_output}")


@cli.command(
    "pull",
    help=ch.CMD_TRACE_PULL,
    short_help=ch.CMD_TRACE_PULL,
    epilog=ch.EXAMPLES_TRACE_PULL,
)
def pull_cmd(
    url: str,
    repo_path: _RepoPath = None,
    output: _Output = None,
    save: Path | None = typer.Option(
        None, "--save", dir_okay=False, help=ch.HELP_TRACE_PULL_SAVE
    ),
    headers: list[str] | None = typer.Option(
        None, "--header", help=ch.HELP_TRACE_PULL_HEADER
    ),
    timeout: float = typer.Option(
        60.0, "--timeout", show_default=False, help=ch.HELP_TRACE_PULL_TIMEOUT
    ),
    workload: _Workload = None,
    language: _Language = None,
    path_map: _PathMap = None,
    build_id: _BuildId = None,
    service: _Service = None,
    label: _Label = None,
    commit: _Commit = None,
) -> None:
    import os
    import tempfile

    resolved_output = output or Path(tcs.TRACE_DEFAULT_OUTPUT)
    try:
        if save is not None and save.resolve() == resolved_output.resolve():
            raise _ConvertUsageError(ch.ERR_TRACE_PULL_SAVE_EQUALS_OUTPUT)
        data = _download_pprof(url, tuple(headers or ()), timeout)
        # Persist the profile where the caller asked, else in a temp file that is
        # removed after conversion (a cron overlay does not need to keep it).
        # mkstemp returns an open fd; close it so Windows does not lock the file
        # against the write below.
        if save is not None:
            profile_path = save
        else:
            handle, temp_name = tempfile.mkstemp(suffix=".pb.gz", prefix="cgr-ebpf-")
            os.close(handle)
            profile_path = Path(temp_name)
        try:
            profile_path.write_bytes(data)
            count = _convert_ebpf(
                profile_path,
                repo_path,
                resolved_output,
                workload,
                language,
                tuple(path_map or ()),
                build_id,
                service,
                label,
                commit,
            )
        finally:
            if save is None:
                profile_path.unlink(missing_ok=True)
    except (OSError, ValueError, _ConvertUsageError) as e:
        logger.error(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(f"call records written: {count} -> {resolved_output}")
