"""Convert eBPF continuous-profiler pprof profiles to the interchange format.

eBPF profilers (Parca, Pyroscope, the OpenTelemetry eBPF profiler, ``perf``
-> pprof) sample stacks from outside the process at the kernel level: zero
instrumentation of the target, fleet-wide, continuous. Their output is the
same pprof wire format the Go/Rust converters decode, so production call
chains fuse through the existing ingest door as ``dynamic_sampled`` edges.

Three things separate a fleet profile from ``go test -cpuprofile`` output, all
handled here rather than in the shared decoder:

* **Mappings** (pprof field 3) name the binary each location came from; a fleet
  profile mixes the target service, libc, and the kernel. Locations are filtered
  to a target build-id or mapping filename, and unsymbolised locations are
  counted per mapping instead of being dropped silently.
* **Sample labels** (Sample field 3) carry ``pid``/``service``/``endpoint`` tags.
  A label filters to the target service, and a chosen label becomes the edge's
  ``workloads`` -- production's analogue of "which test exercised this edge".
* **Path re-anchoring.** Production binaries are built elsewhere
  (``/build/src/...``, container prefixes), so a ``--path-map`` rewrites the
  build prefix to the repository prefix before the in-repo check; unmapped
  frames keep their path and are counted as misses.

Symbol grammars are reused from the per-runtime converters, keyed off the
selected language: Go (``pprof``), Rust (``rust_pprof``), and C/C++ (the
already-demangled grammar in ``instrumented``; Parca server-side-symbolizes and
demangles, which this path assumes).

Interpreted runtimes (``--language python``) are the other axis: the eBPF
profiler unwinds the interpreter stack in-kernel and reports source-level frames
(``<module>``, a bare ``func``, or ``Class.method``) against the real ``.py``
file, so those frames carry no native symbol to demangle and no native mapping
to filter on. This path normalises the name (dropping the ``py::`` prefix the
perf-map symbolisation route prepends), filters to the project by source-path
containment, and emits ``language=python`` so ingestion routes the records to
Python's existing frame resolver -- the same resolver the ``sys.monitoring``
tracer feeds.
"""

from __future__ import annotations

import posixpath
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger

from .. import constants as cs
from .instrumented import _bare_name as _cpp_bare_name
from .pprof import (
    _bare_name as _go_bare_name,
)
from .pprof import (
    _decompress,
    _fields,
    _parse_function,
    _repeated_uint,
)
from .records import (
    CallRecord,
    FramePoint,
    TraceFormatError,
    TraceHeader,
    write_trace_file,
)
from .rust_pprof import _bare_name as _rust_bare_name

if TYPE_CHECKING:
    from pathlib import Path

# Bare-member demanglers reused from the per-runtime converters.
_DEMANGLERS = {
    cs.TRACE_LANGUAGE_GO: _go_bare_name,
    cs.TRACE_LANGUAGE_RUST: _rust_bare_name,
    cs.TRACE_LANGUAGE_CPP: _cpp_bare_name,
}

# Interpreted runtimes the eBPF profiler unwinds in-kernel. Their frames arrive
# as source-level names (``<module>``, a bare ``func``, or ``Class.method``)
# against the real source file, not as native binary addresses, so they reuse
# each language's existing frame resolver (Python's ``FrameResolver`` is the
# ingest default) instead of a symbol demangler, and are filtered to the project
# by source-path containment rather than a native build-id. The name passes
# through untouched except for stripping the ``py::`` prefix the perf-map
# symbolisation path some profilers reuse prepends to Python frames.
_PY_PERF_PREFIX = "py::"


def _interpreted_name(name: str) -> str:
    """A pprof interpreted-frame name as the language resolver expects it."""
    if name.startswith(_PY_PERF_PREFIX):
        return name[len(_PY_PERF_PREFIX) :]
    return name


_INTERPRETED_NAME_FNS = {
    cs.TRACE_LANGUAGE_PYTHON: _interpreted_name,
}

_SUPPORTED_LANGUAGES = frozenset(_DEMANGLERS) | frozenset(_INTERPRETED_NAME_FNS)


@dataclass(slots=True)
class _Mapping:
    filename_index: int = 0
    build_id_index: int = 0


@dataclass(slots=True)
class _Location:
    mapping_id: int = 0
    function_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class _LabeledSample:
    location_ids: list[int]
    weight: int
    labels: dict[str, str]


def _parse_mapping(payload: bytes) -> tuple[int, _Mapping]:
    """A mapping's id, filename string index, and build-id string index."""
    mapping_id = 0
    mapping = _Mapping()
    for field_num, _wire, value in _fields(payload):
        if not isinstance(value, int):
            continue
        if field_num == 1:
            mapping_id = value
        elif field_num == 5:
            mapping.filename_index = value
        elif field_num == 6:
            mapping.build_id_index = value
    return mapping_id, mapping


def _line_function_ids(line_payload: bytes) -> list[int]:
    """The function ids of a Location's inline Line records."""
    return [
        value
        for line_field, _wire, value in _fields(line_payload)
        if line_field == 1 and isinstance(value, int)
    ]


def _parse_location(payload: bytes) -> tuple[int, _Location]:
    """A location's id, mapping id, and function ids (inline frames)."""
    location_id = 0
    location = _Location()
    for field_num, _wire, value in _fields(payload):
        if field_num == 1 and isinstance(value, int):
            location_id = value
        elif field_num == 2 and isinstance(value, int):
            location.mapping_id = value
        elif field_num == 4 and isinstance(value, bytes):
            location.function_ids.extend(_line_function_ids(value))
    return location_id, location


def _parse_label(payload: bytes) -> tuple[int, int]:
    """A label's key string index and (string) value index; numeric labels 0."""
    key_index = 0
    str_index = 0
    for field_num, _wire, value in _fields(payload):
        if not isinstance(value, int):
            continue
        if field_num == 1:
            key_index = value
        elif field_num == 2:
            str_index = value
    return key_index, str_index


def _parse_sample(payload: bytes, strings: list[str]) -> _LabeledSample:
    location_ids: list[int] = []
    weight = 0
    labels: dict[str, str] = {}
    for field_num, wire, value in _fields(payload):
        if field_num == 1:
            location_ids.extend(_repeated_uint(wire, value))
        elif field_num == 2 and not weight:
            values = _repeated_uint(wire, value)
            weight = values[0] if values else 0
        elif field_num == 3 and isinstance(value, bytes):
            key_index, str_index = _parse_label(value)
            key = _string_at(strings, key_index)
            val = _string_at(strings, str_index)
            if key:
                labels[key] = val
    return _LabeledSample(location_ids, max(weight, 1), labels)


def _string_at(strings: list[str], index: int) -> str:
    return strings[index] if 0 <= index < len(strings) else ""


@dataclass(slots=True)
class _Profile:
    strings: list[str]
    functions: dict[int, object]
    locations: dict[int, _Location]
    mappings: dict[int, _Mapping]
    samples: list[_LabeledSample]


def _decode(raw: bytes) -> _Profile:
    """Decode the pprof message, keeping mappings, labels, and location owners."""
    strings: list[str] = []
    functions: dict[int, object] = {}
    locations: dict[int, _Location] = {}
    mappings: dict[int, _Mapping] = {}
    sample_payloads: list[bytes] = []
    for field_num, _wire, value in _fields(raw):
        if not isinstance(value, bytes):
            continue
        if field_num == 2:
            # Labels reference the string table; defer until it is fully read.
            sample_payloads.append(value)
        elif field_num == 3:
            mapping_id, mapping = _parse_mapping(value)
            mappings[mapping_id] = mapping
        elif field_num == 4:
            location_id, location = _parse_location(value)
            locations[location_id] = location
        elif field_num == 5:
            function_id, function = _parse_function(value)
            functions[function_id] = function
        elif field_num == 6:
            strings.append(value.decode("utf-8", errors="replace"))
    samples = [_parse_sample(payload, strings) for payload in sample_payloads]
    return _Profile(strings, functions, locations, mappings, samples)


def _prefix_matches(path: str, prefix: str) -> bool:
    """Whether ``prefix`` matches ``path`` at a path-component boundary.

    ``/build/src`` matches ``/build/src/x`` but not ``/build/src-other/x``.
    """
    if not path.startswith(prefix):
        return False
    rest = path[len(prefix) :]
    return prefix.endswith("/") or rest == "" or rest.startswith("/")


def _reanchor(path: str, path_map: list[tuple[str, str]]) -> str:
    """Rewrite a build-time path prefix to the repository prefix, first match."""
    for build_prefix, repo_prefix in path_map:
        if _prefix_matches(path, build_prefix):
            return repo_prefix + path[len(build_prefix) :]
    return path


def _within_repo(path: str, root_prefix: str) -> bool:
    """Whether a normalised path is the repo root or sits inside it."""
    return path == root_prefix.rstrip("/") or path.startswith(root_prefix)


class _FrameBuilder:
    """Resolves function ids to in-repo frames, tracking symbolisation gaps."""

    def __init__(
        self,
        profile: _Profile,
        root_prefix: str,
        path_map: list[tuple[str, str]],
        name_fn,
    ) -> None:
        self._profile = profile
        self._root_prefix = root_prefix
        self._path_map = path_map
        self._name_fn = name_fn
        self._cache: dict[int, FramePoint | None] = {}
        self.unmapped_paths: Counter[str] = Counter()

    def frame(self, function_id: int) -> FramePoint | None:
        if function_id not in self._cache:
            self._cache[function_id] = self._build(function_id)
        return self._cache[function_id]

    def _build(self, function_id: int) -> FramePoint | None:
        function = self._profile.functions.get(function_id)
        strings = self._profile.strings
        filename_index = getattr(function, "filename_index", 0)
        if function is None or not 0 < filename_index < len(strings):
            return None
        original = strings[filename_index]
        # normpath collapses any ``..`` so a re-anchored path cannot traverse out
        # of the repo and still pass the containment check below.
        path = posixpath.normpath(_reanchor(original, self._path_map))
        if not _within_repo(path, self._root_prefix):
            # No --path-map rewrote this build path into the repo (or it escaped
            # via ``..``): keep the original and count it, so gaps are visible.
            self.unmapped_paths[original] += 1
            return None
        name_index = getattr(function, "name_index", 0)
        name = strings[name_index] if 0 < name_index < len(strings) else ""
        return FramePoint(
            path=path,
            qualname=self._name_fn(name),
            line=max(getattr(function, "start_line", 0), 0),
        )


def _mapping_selected(
    location: _Location, profile: _Profile, build_id: str | None
) -> bool:
    """Whether a location's binary is the target (no filter selects every one)."""
    if build_id is None:
        return True
    mapping = profile.mappings.get(location.mapping_id)
    if mapping is None:
        return False
    return build_id in (
        _string_at(profile.strings, mapping.build_id_index),
        _string_at(profile.strings, mapping.filename_index),
    ) or _string_at(profile.strings, mapping.filename_index).endswith(build_id)


def _sample_matches(sample: _LabeledSample, service: tuple[str, str] | None) -> bool:
    return service is None or sample.labels.get(service[0]) == service[1]


def _sample_workload(
    sample: _LabeledSample, workload_label: str | None, default: str | None
) -> str | None:
    if workload_label is not None:
        return sample.labels.get(workload_label) or default
    return default


def _mapping_name(profile: _Profile, location: _Location) -> str:
    mapping = profile.mappings.get(location.mapping_id)
    return _string_at(profile.strings, mapping.filename_index) if mapping else ""


def _resolved_frames(
    sample: _LabeledSample,
    profile: _Profile,
    builder: _FrameBuilder,
    build_id: str | None,
    unsymbolised: Counter[str],
    *,
    select_mapping: bool = True,
) -> list[FramePoint]:
    """A sample's in-repo frames, root-first, seeing through other binaries.

    Locations from another binary and unsymbolised locations are skipped rather
    than breaking the chain, so a project edge survives a libc frame between its
    endpoints; unsymbolised target-binary locations are counted per mapping.

    Interpreted frames (``select_mapping=False``) sit in the interpreter's
    mapping, not the target's native binary, so a native build-id filter would
    drop every one; they are kept regardless of mapping and filtered to the
    project by the builder's source-path containment check instead.
    """
    frames: list[FramePoint] = []
    for location_id in reversed(sample.location_ids):
        location = profile.locations.get(location_id)
        if location is None:
            continue
        if select_mapping and not _mapping_selected(location, profile, build_id):
            continue
        if not location.function_ids:
            unsymbolised[_mapping_name(profile, location) or "<unknown>"] += 1
            continue
        for function_id in reversed(location.function_ids):
            frame = builder.frame(function_id)
            if frame is not None:
                frames.append(frame)
    return frames


def _accumulate(
    profile: _Profile,
    builder: _FrameBuilder,
    *,
    build_id: str | None,
    service: tuple[str, str] | None,
    workload_label: str | None,
    default_workload: str | None,
    interpreted: bool = False,
) -> tuple[dict[tuple[FramePoint, FramePoint], tuple[int, set[str]]], Counter[str]]:
    """Adjacent in-repo frames per leaf-first stack, with per-edge workloads."""
    edges: dict[tuple[FramePoint, FramePoint], tuple[int, set[str]]] = {}
    unsymbolised: Counter[str] = Counter()
    for sample in profile.samples:
        if not _sample_matches(sample, service):
            continue
        workload = _sample_workload(sample, workload_label, default_workload)
        frames = _resolved_frames(
            sample,
            profile,
            builder,
            build_id,
            unsymbolised,
            select_mapping=not interpreted,
        )
        for ancestor, frame in zip(frames, frames[1:], strict=False):
            count, workloads = edges.get((ancestor, frame), (0, set()))
            if workload:
                workloads.add(workload)
            edges[(ancestor, frame)] = (count + sample.weight, workloads)
    return edges, unsymbolised


def _report(unsymbolised: Counter[str], unmapped_paths: Counter[str]) -> None:
    """Log symbolisation and path-anchoring gaps so they are visible."""
    total_unsymbolised = sum(unsymbolised.values())
    if total_unsymbolised:
        logger.info(
            cs.TRACE_MSG_EBPF_UNSYMBOLIZED.format(
                count=total_unsymbolised, mappings=len(unsymbolised)
            )
        )
        for name, count in unsymbolised.most_common():
            logger.info(cs.TRACE_MSG_EBPF_MAPPING_DETAIL.format(name=name, count=count))
    total_unmapped = sum(unmapped_paths.values())
    if total_unmapped:
        logger.info(
            cs.TRACE_MSG_EBPF_UNMAPPED.format(
                count=total_unmapped, paths=len(unmapped_paths)
            )
        )


def convert_ebpf_pprof(
    profile_path: Path,
    repo_root: Path,
    output: Path,
    *,
    workload: str | None = None,
    path_map: list[tuple[str, str]] | None = None,
    build_id: str | None = None,
    service: tuple[str, str] | None = None,
    workload_label: str | None = None,
    language: str = cs.TRACE_LANGUAGE_GO,
) -> int:
    """Convert an eBPF-profiler pprof profile to project call edges.

    ``path_map`` rewrites build-time path prefixes to the repository prefix;
    ``build_id`` filters to one binary's mapping; ``service`` (a ``key, value``
    pair) filters samples; ``workload_label`` maps a sample label to per-edge
    ``workloads``; ``language`` selects the symbol demangler (native runtimes)
    or the interpreted-frame name normaliser (Python), the latter reusing the
    language's existing ingest-time frame resolver.
    """
    interpreted = language in _INTERPRETED_NAME_FNS
    name_fn = _INTERPRETED_NAME_FNS.get(language) or _DEMANGLERS.get(language)
    if name_fn is None:
        raise TraceFormatError(
            cs.TRACE_ERR_EBPF_LANGUAGE.format(
                language=language, supported=", ".join(sorted(_SUPPORTED_LANGUAGES))
            )
        )
    raw = _decompress(profile_path.read_bytes(), profile_path)
    try:
        profile = _decode(raw)
    except TraceFormatError as e:
        raise TraceFormatError(cs.TRACE_ERR_BAD_PPROF.format(path=profile_path)) from e
    if not profile.strings or not profile.functions or not profile.samples:
        raise TraceFormatError(cs.TRACE_ERR_BAD_PPROF.format(path=profile_path))

    root_prefix = repo_root.resolve().as_posix() + "/"
    builder = _FrameBuilder(profile, root_prefix, path_map or [], name_fn)
    edges, unsymbolised = _accumulate(
        profile,
        builder,
        build_id=build_id,
        service=service,
        workload_label=workload_label,
        default_workload=workload,
        interpreted=interpreted,
    )
    _report(unsymbolised, builder.unmapped_paths)

    fallback = (workload,) if workload else ()
    records = [
        CallRecord(
            caller=caller,
            callee=callee,
            count=count,
            workloads=tuple(sorted(workloads)) if workloads else fallback,
            receiver_types=(),
        )
        for (caller, callee), (count, workloads) in edges.items()
    ]
    header = TraceHeader(
        version=cs.TRACE_FORMAT_VERSION,
        language=language,
        repo_root=str(repo_root),
        tracer=cs.TRACE_TOOL_NAME_EBPF,
        sampled=True,
    )
    write_trace_file(output, header, records)
    return len(records)
