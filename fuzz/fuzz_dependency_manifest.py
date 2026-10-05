"""Fuzz the dependency manifest parsers.

Every manifest `read_manifest` reads is repository content, so any value
in it can have any type. What it reports becomes `ExternalPackage` nodes and
`DEPENDS_ON_EXTERNAL` properties, so a number or a map in a version, or a
package invented from a malformed section, is written to the graph. Two
modes, chosen by the first byte; the second picks the manifest format:

* raw -- the rest is the file. Every dependency reported must carry a `str`
  name and spec and `str` properties.
* manifest -- a manifest assembled from entry records whose dependencies are
  known: versioned, unversioned, richer and wrong-typed entries, and decoys
  that only look like entries, in sections that are well formed or of the
  wrong type. The parser must report exactly the declared dependencies, never
  one from a decoy or a wrong-typed section, and must not fail on a manifest
  that is well formed.

A manifest record is `shape section arg len payload[len]`, after a byte of
section shapes. The payload spells the package name and `arg` the version,
so a seed is a few bytes written by hand in `build_corpus.py`.

Run locally (Linux; atheris does not build against Apple Clang):

    uv run --extra fuzz python fuzz/fuzz_dependency_manifest.py -max_total_time=60
"""

import json
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import atheris
from loguru import logger

with atheris.instrument_imports():
    from codebase_rag.models import Dependency
    from codebase_rag.parsers import dependency_parser
    from codebase_rag.parsers.dependency_parser import read_manifest

MODES = 2
MODE_RAW, MODE_MANIFEST = range(MODES)
HEADER = 4
MAX_PAYLOAD = 16
MAX_ENTRIES = 999
NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_."

MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "app.csproj",
)

SHAPES = 5
SHAPE_VERSIONED, SHAPE_BARE, SHAPE_RICH, SHAPE_WRONG, SHAPE_DECOY = range(SHAPES)

# Two bits per section in the section-shape byte.
SECTION_WELL_FORMED, SECTION_STRING, SECTION_NUMBER, SECTION_CONTAINER = range(4)

JSON_WRONG_VALUES: tuple[object, ...] = (7, [1], True, None, {"version": "1.0"}, 1.5)
TOML_WRONG_VALUES: tuple[object, ...] = (7, [1], True, 1.5, {"version": 7})

WORKDIR = Path(tempfile.mkdtemp(prefix="cgr-fuzz-deps-"))

Props = tuple[tuple[str, str], ...]
Triple = tuple[str, str, Props]


class Entry(NamedTuple):
    name: str
    version: str
    shape: int
    section: int
    arg: int


class Manifest(NamedTuple):
    text: str
    # Must each be reported exactly once, exactly so.
    expected: list[Triple]
    # Must each be reported exactly once, with any `str` spec: poetry writes a
    # table or a number as its Python text.
    loose: list[str]


def _triple(name: str, spec: str, props: dict[str, str] | None = None) -> Triple:
    return name, spec, tuple(sorted((props or {}).items()))


def read_entries(data: bytes, sections: int) -> list[Entry]:
    entries: list[Entry] = []
    i = 0
    while i + HEADER <= len(data) and len(entries) < MAX_ENTRIES:
        shape, section, arg, length = data[i : i + HEADER]
        i += HEADER
        payload = data[i : i + length % (MAX_PAYLOAD + 1)]
        i += len(payload)
        # The fixed-width index keeps every name unique whatever the payload.
        name = f"n{len(entries):03d}" + "".join(
            NAME_ALPHABET[b % len(NAME_ALPHABET)] for b in payload
        )
        entries.append(
            Entry(
                name, f"{arg >> 4}.{arg & 15}", shape % SHAPES, section % sections, arg
            )
        )
    return entries


def _section_shape(shapes: int, section: int) -> int:
    return (shapes >> (2 * section)) & 3


def _malformed(shape: int, names: list[str], as_list: bool) -> object:
    # The wrong-typed stand-in for a section still names its entries, so a
    # parser that iterates it anyway reports a name or a character of one.
    if shape == SECTION_STRING:
        return " ".join(names) or "x"
    if shape == SECTION_NUMBER:
        return 7
    return {name: name for name in names} if as_list else list(names)


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = (f"{json.dumps(k)} = {_toml(v)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    raise TypeError(value)


def _toml_document(tables: dict[str, object]) -> str:
    return "".join(f"{json.dumps(k)} = {_toml(v)}\n" for k, v in tables.items())


def _sections(entries: list[Entry], count: int) -> list[list[Entry]]:
    grouped: list[list[Entry]] = [[] for _ in range(count)]
    for entry in entries:
        grouped[entry.section].append(entry)
    return grouped


PYPROJECT_GROUPS = ("g1", "g2")


def _pyproject(entries: list[Entry], shapes: int) -> Manifest:
    expected: list[Triple] = []
    loose: list[str] = []
    scripts: dict[str, str] = {}
    lists: list[object] = []
    grouped = _sections(entries, 4)
    for section, group in enumerate((None, *PYPROJECT_GROUPS)):
        props = {"group": group} if group else None
        items: list[object] = []
        names: list[str] = []
        for e in grouped[section]:
            if e.shape == SHAPE_WRONG:
                items.append(TOML_WRONG_VALUES[e.arg % len(TOML_WRONG_VALUES)])
                continue
            if e.shape == SHAPE_DECOY:
                items.append(f"# {e.name}")
                continue
            line = {
                SHAPE_VERSIONED: f"{e.name}=={e.version}",
                SHAPE_BARE: e.name,
                SHAPE_RICH: f"{e.name}[x] >= {e.version} ; python_version > '3'",
            }[e.shape]
            items.append(line)
            names.append(e.name)
            expected.append(_triple(e.name, line, props))
        shape = _section_shape(shapes, section)
        if shape != SECTION_WELL_FORMED:
            expected = [t for t in expected if t[0] not in names]
            lists.append(_malformed(shape, names, as_list=True))
        else:
            lists.append(items)
    poetry: dict[str, object] = {}
    names = []
    for e in grouped[3]:
        if e.shape == SHAPE_DECOY:
            scripts[e.name] = "pkg:main"
            continue
        names.append(e.name)
        if e.shape == SHAPE_VERSIONED:
            poetry[e.name] = e.version
            expected.append(_triple(e.name, e.version))
        elif e.shape == SHAPE_BARE:
            poetry[e.name] = "*"
            expected.append(_triple(e.name, "*"))
        else:
            poetry[e.name] = (
                {"version": e.version, "optional": True}
                if e.shape == SHAPE_RICH
                else TOML_WRONG_VALUES[e.arg % len(TOML_WRONG_VALUES)]
            )
            loose.append(e.name)
    poetry_shape = _section_shape(shapes, 3)
    if poetry_shape != SECTION_WELL_FORMED:
        expected = [t for t in expected if t[0] not in names]
        loose = []
    project = {
        "dependencies": lists[0],
        "optional-dependencies": dict(zip(PYPROJECT_GROUPS, lists[1:], strict=True)),
        "scripts": scripts,
    }
    tool = {
        "poetry": {
            "dependencies": poetry
            if poetry_shape == SECTION_WELL_FORMED
            else _malformed(poetry_shape, names, as_list=False)
        }
    }
    return Manifest(_toml_document({"project": project, "tool": tool}), expected, loose)


def _object_manifest(
    keys: tuple[str, ...],
    decoy_key: str,
    spec: Callable[[Entry], tuple[object, str]],
    entries: list[Entry],
    shapes: int,
    render: Callable[[dict[str, object]], str],
) -> Manifest:
    """A manifest of name-to-spec tables: package.json, composer.json, Cargo."""
    document: dict[str, object] = {decoy_key: {}}
    expected: list[Triple] = []
    for section, (key, members) in enumerate(
        zip(keys, _sections(entries, len(keys)), strict=True)
    ):
        table: dict[str, object] = {}
        names: list[str] = []
        for e in members:
            value, reported = spec(e)
            if e.shape == SHAPE_DECOY:
                document[decoy_key][e.name] = value  # type: ignore[index]
                continue
            table[e.name] = value
            names.append(e.name)
            expected.append(_triple(e.name, reported))
        shape = _section_shape(shapes, section)
        if shape == SECTION_WELL_FORMED:
            document[key] = table
        else:
            document[key] = _malformed(shape, names, as_list=False)
            expected = [t for t in expected if t[0] not in names]
    return Manifest(render(document), expected, [])


def _json_spec(e: Entry) -> tuple[object, str]:
    if e.shape == SHAPE_VERSIONED:
        return "^" + e.version, "^" + e.version
    if e.shape == SHAPE_BARE:
        return "", ""
    if e.shape == SHAPE_RICH:
        spec = f"~{e.version} || >={e.version}"
        return spec, spec
    if e.shape == SHAPE_WRONG:
        return JSON_WRONG_VALUES[e.arg % len(JSON_WRONG_VALUES)], ""
    return "x", ""


def _cargo_spec(e: Entry) -> tuple[object, str]:
    if e.shape == SHAPE_VERSIONED:
        return e.version, e.version
    if e.shape == SHAPE_BARE:
        return {"path": "../p"}, ""
    if e.shape == SHAPE_RICH:
        return {"version": e.version, "features": ["f"]}, e.version
    if e.shape == SHAPE_WRONG:
        return TOML_WRONG_VALUES[e.arg % len(TOML_WRONG_VALUES)], ""
    return ["x"], ""


def _requirements(entries: list[Entry], _shapes: int) -> Manifest:
    lines: list[str] = []
    expected: list[Triple] = []
    for e in entries:
        n, v = e.name, e.version
        if e.shape == SHAPE_DECOY:
            lines.append(f"# {n}=={v}" if e.arg % 2 else f"-r {n}.txt")
            continue
        line, spec = {
            SHAPE_VERSIONED: (f"{n}=={v}", f"=={v}"),
            SHAPE_BARE: (n, ""),
            SHAPE_RICH: (
                f"{n}[x]>={v} ; python_version > '3'",
                f">={v} ; python_version > '3'",
            ),
            SHAPE_WRONG: (f"  {n} == {v}  ", f"== {v}"),
        }[e.shape]
        lines.append(line)
        expected.append(_triple(n, spec))
    return Manifest("\n".join(lines) + "\n", expected, [])


def _gomod(entries: list[Entry], _shapes: int) -> Manifest:
    # Section 0 is single-line `require`s, section 1 the `require ( )` block.
    expected: list[Triple] = []
    single, block = _sections(entries, 2)
    lines = ["module example.com/m", "", "go 1.21", ""]
    for e in single:
        n, v = e.name, "v" + e.version
        line, reported = {
            SHAPE_VERSIONED: (f"require {n} {v}", True),
            SHAPE_BARE: (f"require {n}", False),
            SHAPE_RICH: (f"require {n} {v} // indirect", True),
            SHAPE_WRONG: (f"require   {n}   {v}", True),
            SHAPE_DECOY: (f"// require {n} {v}", False),
        }[e.shape]
        lines.append(line)
        if reported:
            expected.append(_triple(n, v))
    lines.append("require (")
    for e in block:
        n, v = e.name, "v" + e.version
        line, reported = {
            SHAPE_VERSIONED: (f"\t{n} {v}", True),
            SHAPE_BARE: (f"\t{n}", False),
            SHAPE_RICH: (f"\t{n} {v} // indirect", True),
            SHAPE_WRONG: (f"\t{n} // {v}", False),
            SHAPE_DECOY: (f"\t// {n} {v}", False),
        }[e.shape]
        lines.append(line)
        if reported:
            expected.append(_triple(n, v))
    lines.append(")")
    return Manifest("\n".join(lines) + "\n", expected, [])


def _gemfile(entries: list[Entry], _shapes: int) -> Manifest:
    lines = ["source 'https://rubygems.org'"]
    expected: list[Triple] = []
    for e in entries:
        n, v = e.name, e.version
        if e.shape == SHAPE_DECOY:
            lines.append(f"# gem '{n}', '{v}'")
            continue
        line, spec = {
            SHAPE_VERSIONED: (f"gem '{n}', '{v}'", v),
            SHAPE_BARE: (f'gem "{n}"', ""),
            SHAPE_RICH: (f'gem "{n}", "~> {v}", require: false', f"~> {v}"),
            SHAPE_WRONG: (f"gem '{n}', \"{v}\"", v),
        }[e.shape]
        lines.append(line)
        expected.append(_triple(n, spec))
    return Manifest("\n".join(lines) + "\n", expected, [])


def _pubspec(entries: list[Entry], _shapes: int) -> Manifest:
    lines = ["name: app"]
    expected: list[Triple] = []
    for key, members in zip(
        ("dependencies", "dev_dependencies"), _sections(entries, 2), strict=True
    ):
        lines.append(f"{key}:")
        for e in members:
            n, v = e.name, e.version
            if e.shape == SHAPE_DECOY:
                lines.append(f"  # {n}: {v}")
                continue
            line, spec = {
                SHAPE_VERSIONED: (f"  {n}: ^{v}", "^" + v),
                SHAPE_BARE: (f"  {n}:", ""),
                SHAPE_RICH: (f"  {n}:\n    path: ../{n}", ""),
                SHAPE_WRONG: (f"  {n}:   {v}  ", v),
            }[e.shape]
            lines.append(line)
            expected.append(_triple(n, spec))
    lines += ["flutter:", "  uses-material-design: true"]
    return Manifest("\n".join(lines) + "\n", expected, [])


def _csproj(entries: list[Entry], _shapes: int) -> Manifest:
    lines = ['<Project Sdk="Microsoft.NET.Sdk">', "  <ItemGroup>"]
    expected: list[Triple] = []
    for e in entries:
        n, v = e.name, e.version
        if e.shape == SHAPE_DECOY:
            # An assembly reference, not a package. Not a comment: a name
            # holding `--` would make the whole document malformed.
            lines.append(f'    <Reference Include="{n}" />')
            continue
        line, spec = {
            SHAPE_VERSIONED: (f'<PackageReference Include="{n}" Version="{v}" />', v),
            SHAPE_BARE: (f'<PackageReference Include="{n}" />', ""),
            SHAPE_RICH: (
                f'<PackageReference Version="{v}" Include="{n}" PrivateAssets="all" />',
                v,
            ),
            SHAPE_WRONG: (f'<PackageReference Include="{n}" Version="" />', ""),
        }[e.shape]
        lines.append("    " + line)
        expected.append(_triple(n, spec))
    lines += ["  </ItemGroup>", "</Project>"]
    return Manifest("\n".join(lines) + "\n", expected, [])


def _package_json(entries: list[Entry], shapes: int) -> Manifest:
    keys = ("dependencies", "devDependencies", "peerDependencies")
    return _object_manifest(keys, "scripts", _json_spec, entries, shapes, json.dumps)


def _composer(entries: list[Entry], shapes: int) -> Manifest:
    keys = ("require", "require-dev")
    return _object_manifest(keys, "suggest", _json_spec, entries, shapes, json.dumps)


def _cargo(entries: list[Entry], shapes: int) -> Manifest:
    keys = ("dependencies", "dev-dependencies")
    return _object_manifest(
        keys, "features", _cargo_spec, entries, shapes, _toml_document
    )


# (sections, builder) per entry of MANIFESTS.
BUILDERS: tuple[tuple[int, Callable[[list[Entry], int], Manifest]], ...] = (
    (4, _pyproject),
    (1, _requirements),
    (3, _package_json),
    (2, _cargo),
    (2, _gomod),
    (1, _gemfile),
    (2, _composer),
    (2, _pubspec),
    (1, _csproj),
)


def build_manifest(fmt: int, data: bytes) -> Manifest:
    sections, builder = BUILDERS[fmt]
    shapes = data[0] if data else 0
    return builder(read_entries(data[1:], sections), shapes)


class _Failures:
    """Stands in for the parser module's logger: each parser swallows a fault
    of its own and logs it at ERROR, so a crash is otherwise invisible to the
    oracle. Content the format rejects is reported as `unparsable` instead."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def error(self, message: str, *_args: object, **_kwargs: object) -> None:
        self.messages.append(message)

    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None


def parse(path: Path) -> tuple[list[Dependency], list[str]]:
    failures = _Failures()
    saved = dependency_parser.logger
    dependency_parser.logger = failures  # type: ignore[assignment]
    try:
        parsed = read_manifest(path)
    finally:
        dependency_parser.logger = saved
    # A parser fault and content the parser refused both fail a manifest
    # that is well formed; the second logs only at DEBUG (issue #2568).
    if parsed.unparsable is not None:
        failures.messages.append(parsed.unparsable)
    return parsed.dependencies, failures.messages


def _check_types(dependencies: list[Dependency]) -> None:
    for dep in dependencies:
        props = dep.properties
        if not (
            type(dep.name) is str
            and type(dep.spec) is str
            and type(props) is dict
            and all(type(k) is str and type(v) is str for k, v in props.items())
        ):
            raise AssertionError(f"a dependency of the wrong type: {dep!r}")


def _write(fmt: int, content: bytes) -> Path:
    path = WORKDIR / MANIFESTS[fmt]
    path.write_bytes(content)
    return path


def _check_raw(fmt: int, data: bytes) -> None:
    dependencies, _ = parse(_write(fmt, data))
    _check_types(dependencies)


def _check_manifest(fmt: int, data: bytes) -> None:
    manifest = build_manifest(fmt, data)
    dependencies, failures = parse(_write(fmt, manifest.text.encode()))
    if failures:
        raise AssertionError(f"a well-formed manifest failed: {failures}")
    _check_types(dependencies)
    loose = set(manifest.loose)
    strict = Counter(
        _triple(d.name, d.spec, d.properties)
        for d in dependencies
        if d.name not in loose
    )
    if strict != Counter(manifest.expected):
        raise AssertionError(
            f"reported {sorted(strict.elements())}, declared "
            f"{sorted(manifest.expected)} in {manifest.text!r}"
        )
    seen = Counter(d.name for d in dependencies if d.name in loose)
    if seen != Counter(manifest.loose):
        raise AssertionError(f"reported {seen} of {manifest.loose}")


def fuzz_dependency_manifest(data: bytes) -> None:
    if len(data) < 2:
        return
    mode, fmt, body = data[0] % MODES, data[1] % len(MANIFESTS), data[2:]
    if mode == MODE_RAW:
        _check_raw(fmt, body)
    else:
        _check_manifest(fmt, body)


def main() -> None:
    logger.disable("codebase_rag")
    atheris.Setup(sys.argv, atheris.instrument_func(fuzz_dependency_manifest))
    atheris.Fuzz()


if __name__ == "__main__":
    main()
