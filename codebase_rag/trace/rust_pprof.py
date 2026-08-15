"""Convert Rust pprof CPU profiles to the trace interchange format.

``pprof-rs`` (the ``pprof`` crate) writes pprof protobuf profiles, normally
uncompressed but gzipped just as readily, in the same wire format as Go, so the
decoding here reuses the Go pprof reader (which reads either) and only the
symbol grammar differs. Samples are observed stacks, so parent/child adjacency
is a caller/callee relationship the sampler saw. Static analysis already
resolves monomorphised Rust calls, so the dynamic payoff is ``dyn Trait``
dispatch, function pointers, and closures routed across boundaries; those
appear whenever samples landed there. Counts are sample counts, not call
counts, and the header is flagged ``sampled``.

Rust symbols carry crate/module paths, trait-qualified receivers, generic
instantiations, and a trailing legacy-mangling hash
(``mycrate::svc::Registry::handle::h9f3a...``,
``<mycrate::Dog as mycrate::Animal>::speak``, ``mycrate::run::{{closure}}``);
resolution is span-first against declaration lines, so names normalise to their
bare member form and closures become ``<anonymous>``. Trace a build compiled at
``opt-level = 0`` (the dev profile) so callees are not inlined away; adding
``debug = true`` only preserves symbols and line tables and does not by itself
reduce inlining in an optimised build.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .. import constants as cs
from .pprof import convert_pprof_profile
from .records import FramePoint

if TYPE_CHECKING:
    from pathlib import Path

    from .pprof import _Function

# A trailing ``::h<hex>`` is the legacy (v0-predecessor) mangling disambiguator
# that ``rustc_demangle`` leaves on unless the alternate format is used.
_RUST_LEGACY_HASH = re.compile(r"::h[0-9a-f]+$")
# ``target`` holds build artifacts; anything under the cargo registry lives
# outside the repo root and is already excluded by the prefix check.
_RUST_EXCLUDED_DIR = "target"


def _split_top_level(symbol: str) -> list[str]:
    """Split a Rust path on ``::`` at angle/parenthesis depth zero.

    ``<mycrate::Dog as mycrate::Animal>::speak`` must split into the trait
    block and ``speak``, not on the ``::`` inside the ``<...>``.
    """
    segments: list[str] = []
    depth = 0
    start = 0
    index = 0
    length = len(symbol)
    while index < length:
        char = symbol[index]
        if char in "<(":
            depth += 1
        elif char in ")>":
            depth = max(depth - 1, 0)
        elif (
            char == ":"
            and depth == 0
            and index + 1 < length
            and symbol[index + 1] == ":"
        ):
            segments.append(symbol[start:index])
            index += 2
            start = index
            continue
        index += 1
    segments.append(symbol[start:])
    return [s for s in segments if s]


def _strip_generics_and_args(segment: str) -> str:
    """Drop trailing ``<...>`` / ``(...)`` groups from a single path segment."""
    result: list[str] = []
    depth = 0
    for char in segment:
        if char in "<(":
            depth += 1
        elif char in ")>":
            depth = max(depth - 1, 0)
        elif depth == 0:
            result.append(char)
    return "".join(result)


def _bare_name(symbol: str) -> str:
    """``mycrate::svc::Registry::handle::h9f3a`` as the member name ``handle``.

    Span resolution against declaration lines carries identity; the name is a
    tiebreak, so crate/module paths, trait qualifiers, generic instantiations,
    and the legacy hash drop. Closures mark the frame anonymous.
    """
    cleaned = _RUST_LEGACY_HASH.sub("", symbol.strip())
    segments = _split_top_level(cleaned)
    if any(seg.startswith("{{closure") for seg in segments):
        return cs.TRACE_QUALNAME_ANONYMOUS
    # A trailing ``::<...>`` turbofish (v0 monomorphisation) is generic args, not
    # the member; drop it so ``util::process::<u32>`` resolves to ``process``.
    while segments and segments[-1].startswith("<"):
        segments.pop()
    if not segments:
        return cs.TRACE_QUALNAME_ANONYMOUS
    name = _strip_generics_and_args(segments[-1]).strip()
    if not name or name.startswith(cs.TRACE_SYNTHETIC_PREFIX) or name.startswith("{"):
        return cs.TRACE_QUALNAME_ANONYMOUS
    return name


def _build_frame(
    function: _Function | None, strings: list[str], root_prefix: str
) -> FramePoint | None:
    """A project FramePoint for the function, or None if out of scope."""
    if function is None or not (0 < function.filename_index < len(strings)):
        return None
    path = strings[function.filename_index]
    if not path.startswith(root_prefix):
        return None
    if _RUST_EXCLUDED_DIR in path[len(root_prefix) :].split("/"):
        return None
    name = (
        strings[function.name_index] if 0 < function.name_index < len(strings) else ""
    )
    return FramePoint(
        path=path, qualname=_bare_name(name), line=max(function.start_line, 0)
    )


def convert_rust_pprof(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    workload: str | None = None,
) -> int:
    """Write ``profile_path``'s project call edges to ``output``; returns count.

    The pprof decode/accumulate/emit pipeline is shared with Go via
    ``convert_pprof_profile``; only ``_build_frame`` (Rust demangling and the
    ``target`` exclusion) and the header tags differ.
    """
    return convert_pprof_profile(
        profile_path,
        repo_root,
        output,
        workload,
        build_frame=_build_frame,
        language=cs.TRACE_LANGUAGE_RUST,
        tracer=cs.TRACE_TOOL_NAME_RUST_PPROF,
    )
