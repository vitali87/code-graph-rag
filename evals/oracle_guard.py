"""How a standalone eval reports an oracle that cannot run (issue #1639).

An oracle turns out unusable in one of two places: the guard before the run,
or the post-install re-check inside it. Every eval command ends both the same
way -- a stated result and exit code 1, never a traceback and never a message
that misnames the obstacle -- so the two halves live here once instead of in
each command, where the copies drifted apart.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager

import typer
from loguru import logger

from . import logs as ls
from .oracles import NodeOracleUnavailable


def require_oracle(
    available: Callable[[], bool],
    oracle_missing: str,
    # Empty for an oracle with no binary to name (libclang loads in-process);
    # its message then has no `{binary}` field to fill.
    binary: str = "",
    skip_reason: Callable[[], str | None] | None = None,
) -> None:
    if available():
        return
    # Prefer the probe's own reason when the caller can supply one: the
    # fixed message says "not found on PATH", which is FALSE in the case
    # that matters -- the binary is there and cannot load the parser
    # (issue #1639). Fall back to the fixed string for oracles with no
    # reason probe.
    #
    # Formatted here because the arm that reports a MISSING toolchain is the
    # one arm that never runs and so is never seen by whoever tested the
    # language (issue #1518). Messages with no placeholder are unaffected:
    # `str.format` on a string with no fields is the identity.
    reason = skip_reason() if skip_reason is not None else None
    logger.error(
        ls.ORACLE_UNAVAILABLE.format(reason=reason)
        if reason
        else oracle_missing.format(binary=binary)
    )
    raise typer.Exit(code=1)


@contextmanager
def oracle_unavailable_exits() -> Generator[None]:
    try:
        yield
    except NodeOracleUnavailable as unavailable:
        # `require_oracle` cannot catch this one. On a clean checkout the
        # dependencies are not installed when the guard runs, so it honestly
        # answers "cannot tell yet"; the toolchain's real verdict only exists
        # once `ensure_node_deps` has fetched them, which happens inside the
        # oracle run. Reported the same way as the guard rather than escaping
        # as a traceback: an unavailable toolchain is a result the command
        # should state, not a crash (issue #1639).
        logger.error(ls.ORACLE_UNAVAILABLE.format(reason=unavailable))
        raise typer.Exit(code=1) from unavailable
