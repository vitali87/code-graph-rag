import json
import re
import tomllib
from pathlib import Path
from typing import ClassVar, NamedTuple

import defusedxml.ElementTree as ET
from defusedxml import DefusedXmlException
from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..models import Dependency


def _extract_pep508_package_name(dep_string: str) -> tuple[str, str]:
    stripped = dep_string.strip()
    match = re.match(r"^([a-zA-Z0-9_.-]+(?:\[[^\]]*\])?)", stripped)
    if not match:
        return "", ""
    name_with_extras = match[1]
    name_match = re.match(r"^([a-zA-Z0-9_.-]+)", name_with_extras)
    if not name_match:
        return "", ""
    name = name_match[1]
    spec = stripped[len(name_with_extras) :].strip()
    return name, spec


def _load_toml(file_path: Path) -> dict:
    with file_path.open("rb") as f:
        return tomllib.load(f)


# What a manifest's own content raises: not valid JSON, TOML or XML, not
# UTF-8, or XML entity declarations that defusedxml refuses. Anything else (a
# file gone or unreadable, a fault in the parser) stays an ERROR, which the
# #1070 suite gate watches for.
_CONTENT_ERRORS = (
    json.JSONDecodeError,
    tomllib.TOMLDecodeError,
    UnicodeDecodeError,
    ET.ParseError,
    DefusedXmlException,
)


class ManifestParse(NamedTuple):
    dependencies: list[Dependency]
    # Why the content could not be parsed. None when it parsed, when the file
    # is empty (it declares nothing, so nothing is lost), and on a parser
    # fault, which is logged at ERROR instead.
    unparsable: str | None = None


def _is_blank(file_path: Path) -> bool:
    try:
        return not file_path.read_bytes().strip()
    except OSError:
        return False


class DependencyParser:
    __slots__ = ()

    failure_message: ClassVar[str]

    def parse(self, file_path: Path) -> list[Dependency]:
        return self.read(file_path).dependencies

    def read(self, file_path: Path) -> ManifestParse:
        # Test suites of package managers, bundlers and linters commit
        # manifests that are empty or broken on purpose, and an ERROR apiece
        # made every index of such a repository read as a failed run. Bad
        # content is recoverable and per file: DEBUG here, and the updater
        # names the unparsable ones in one WARNING (issue #2568).
        # `_collect` appends to a list this method owns, so the entries read
        # before a failure part-way through the file are kept, as each
        # parser kept them before the shared reader (Greptile, PR #2613).
        dependencies: list[Dependency] = []
        try:
            self._collect(file_path, dependencies)
        except _CONTENT_ERRORS as e:
            if _is_blank(file_path):
                logger.debug(ls.DEP_MANIFEST_EMPTY.format(path=file_path))
                return ManifestParse(dependencies)
            logger.debug(ls.DEP_MANIFEST_UNPARSABLE.format(path=file_path, error=e))
            return ManifestParse(dependencies, str(e))
        except Exception as e:
            logger.error(self.failure_message.format(path=file_path, error=e))
        return ManifestParse(dependencies)

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        raise NotImplementedError


class PyProjectTomlParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_PYPROJECT

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        data = _load_toml(file_path)

        if poetry_deps := (
            data.get(cs.DEP_KEY_TOOL, {})
            .get(cs.DEP_KEY_POETRY, {})
            .get(cs.DEP_KEY_DEPENDENCIES, {})
        ):
            dependencies.extend(
                Dependency(dep_name, str(dep_spec))
                for dep_name, dep_spec in poetry_deps.items()
                if dep_name.lower() != cs.DEP_EXCLUDE_PYTHON
            )
        if project_deps := data.get(cs.DEP_KEY_PROJECT, {}).get(
            cs.DEP_KEY_DEPENDENCIES, []
        ):
            for dep_line in project_deps:
                dep_name, _ = _extract_pep508_package_name(dep_line)
                if dep_name:
                    dependencies.append(Dependency(dep_name, dep_line))

        optional_deps = data.get(cs.DEP_KEY_PROJECT, {}).get(
            cs.DEP_KEY_OPTIONAL_DEPS, {}
        )
        for group_name, deps in optional_deps.items():
            for dep_line in deps:
                dep_name, _ = _extract_pep508_package_name(dep_line)
                if dep_name:
                    dependencies.append(
                        Dependency(dep_name, dep_line, {cs.DEP_KEY_GROUP: group_name})
                    )


class RequirementsTxtParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_REQUIREMENTS

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue

                dep_name, version_spec = _extract_pep508_package_name(line)
                if dep_name:
                    dependencies.append(Dependency(dep_name, version_spec))


class PackageJsonParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_PACKAGE_JSON

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            data = json.load(f)

        dependencies.extend(
            Dependency(dep_name, dep_spec)
            for key in (
                cs.DEP_KEY_DEPENDENCIES,
                cs.DEP_KEY_DEV_DEPS_JSON,
                cs.DEP_KEY_PEER_DEPS,
            )
            for dep_name, dep_spec in data.get(key, {}).items()
        )


class CargoTomlParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_CARGO

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        data = _load_toml(file_path)

        deps = data.get(cs.DEP_KEY_DEPENDENCIES, {})
        for dep_name, dep_spec in deps.items():
            version = (
                dep_spec
                if isinstance(dep_spec, str)
                else dep_spec.get(cs.DEP_KEY_VERSION, "")
            )
            dependencies.append(Dependency(dep_name, version))

        dev_deps = data.get(cs.DEP_KEY_DEV_DEPENDENCIES, {})
        for dep_name, dep_spec in dev_deps.items():
            version = (
                dep_spec
                if isinstance(dep_spec, str)
                else dep_spec.get(cs.DEP_KEY_VERSION, "")
            )
            dependencies.append(Dependency(dep_name, version))


class GoModParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_GOMOD

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            in_require_block = False
            for line in f:
                line = line.strip()

                if line.startswith(cs.GOMOD_REQUIRE_BLOCK_START):
                    in_require_block = True
                    continue
                if line == cs.GOMOD_BLOCK_END and in_require_block:
                    in_require_block = False
                    continue
                dep = (
                    _gomod_block_entry(line)
                    if in_require_block
                    else _gomod_require_line(line)
                )
                if dep is not None:
                    dependencies.append(dep)


def _gomod_require_line(line: str) -> Dependency | None:
    # A single-line `require module version`.
    if not line.startswith(cs.GOMOD_REQUIRE_LINE_PREFIX):
        return None
    parts = line.split()[1:]
    return Dependency(parts[0], parts[1]) if len(parts) >= 2 else None


def _gomod_block_entry(line: str) -> Dependency | None:
    # A `module version` line inside a `require ( ... )` block; comments and
    # a comment in the version slot bind nothing.
    if not line or line.startswith(cs.GOMOD_COMMENT_PREFIX):
        return None
    parts = line.split()
    if len(parts) < 2 or parts[1].startswith(cs.GOMOD_COMMENT_PREFIX):
        return None
    return Dependency(parts[0], parts[1])


class GemfileParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_GEMFILE

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            for line in f:
                line = line.strip()
                if line.startswith(cs.GEMFILE_GEM_PREFIX):
                    if gem_match := re.match(
                        r'gem\s+["\']([^"\']+)["\'](?:\s*,\s*["\']([^"\']+)["\'])?',
                        line,
                    ):
                        dep_name = gem_match[1]
                        version = gem_match[2] or ""
                        dependencies.append(Dependency(dep_name, version))


class ComposerJsonParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_COMPOSER

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            data = json.load(f)

        deps = data.get(cs.DEP_KEY_REQUIRE, {})
        dependencies.extend(
            Dependency(dep_name, dep_spec)
            for dep_name, dep_spec in deps.items()
            if dep_name != cs.DEP_EXCLUDE_PHP
        )
        dev_deps = data.get(cs.DEP_KEY_REQUIRE_DEV, {})
        dependencies.extend(
            Dependency(dep_name, dep_spec) for dep_name, dep_spec in dev_deps.items()
        )


class CsprojParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_CSPROJ

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        tree = ET.parse(file_path)
        root = tree.getroot()

        for pkg_ref in root.iter(cs.DEP_XML_PACKAGE_REF):
            include = pkg_ref.get(cs.DEP_ATTR_INCLUDE)
            version = pkg_ref.get(cs.DEP_ATTR_VERSION)

            if include:
                dependencies.append(Dependency(include, version or ""))


class PubspecYamlParser(DependencyParser):
    __slots__ = ()
    failure_message = ls.DEP_PARSE_ERROR_PUBSPEC

    def _collect(self, file_path: Path, dependencies: list[Dependency]) -> None:
        # pubspec.yaml is flat enough that a line scanner beats adding a YAML
        # dependency: track the current top-level key by zero indentation and
        # collect the `name: spec` lines under dependencies blocks. The block's
        # entry indent is whatever the FIRST entry uses, so packages are lines at
        # exactly that indent; deeper lines are a nested block's keys (`sdk:`,
        # `git:`, `path:`) and are skipped. A nested block's parent key
        # (`flutter:`) has no inline scalar, so it is recorded name-only
        # (spec = "").
        scanner = _PubspecScanner()
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            for raw in f:
                if (dependency := scanner.feed(raw.rstrip())) is not None:
                    dependencies.append(dependency)


class _PubspecScanner:
    """The line-by-line state of `PubspecYamlParser._collect`: which top-level
    block the scan is in, and the indent its entries use."""

    __slots__ = ("entry_indent", "in_deps")

    def __init__(self) -> None:
        self.in_deps = False
        self.entry_indent: int | None = None

    def feed(self, line: str) -> Dependency | None:
        if not line or line.lstrip().startswith(cs.PUBSPEC_COMMENT_PREFIX):
            return None
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            key = stripped.split(cs.PUBSPEC_KEY_SEP, 1)[0]
            self.in_deps = key in cs.PUBSPEC_DEP_KEYS
            self.entry_indent = None
            return None
        if not self.in_deps or cs.PUBSPEC_KEY_SEP not in stripped:
            return None
        if self.entry_indent is None:
            self.entry_indent = indent
        if indent != self.entry_indent:
            return None
        name, _, spec = stripped.partition(cs.PUBSPEC_KEY_SEP)
        name = name.strip()
        return Dependency(name, spec.strip()) if name else None


def _parser_for(file_path: Path) -> DependencyParser | None:
    match file_path.name.lower():
        case cs.DEP_FILE_PYPROJECT:
            return PyProjectTomlParser()
        case cs.DEP_FILE_REQUIREMENTS:
            return RequirementsTxtParser()
        case cs.DEP_FILE_PACKAGE_JSON:
            return PackageJsonParser()
        case cs.DEP_FILE_CARGO:
            return CargoTomlParser()
        case cs.DEP_FILE_GOMOD:
            return GoModParser()
        case cs.DEP_FILE_GEMFILE:
            return GemfileParser()
        case cs.DEP_FILE_COMPOSER:
            return ComposerJsonParser()
        case cs.DEP_FILE_PUBSPEC:
            return PubspecYamlParser()
        case _ if file_path.suffix.lower() == cs.CSPROJ_SUFFIX:
            return CsprojParser()
        case _:
            return None


def read_manifest(file_path: Path) -> ManifestParse:
    parser = _parser_for(file_path)
    return parser.read(file_path) if parser else ManifestParse([])


def parse_dependencies(file_path: Path) -> list[Dependency]:
    return read_manifest(file_path).dependencies
