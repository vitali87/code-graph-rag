import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import toml
from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..models import Dependency


def _extract_pep508_package_name(dep_string: str) -> tuple[str, str]:
    match = re.match(r"^([a-zA-Z0-9_.-]+(?:\[[^\]]*\])?)", dep_string.strip())
    if not match:
        return "", ""
    name_with_extras = match[1]
    name_match = re.match(r"^([a-zA-Z0-9_.-]+)", name_with_extras)
    if not name_match:
        return "", ""
    name = name_match[1]
    spec = dep_string[len(name_with_extras) :].strip()
    return name, spec


class DependencyParser:
    def parse(self, file_path: Path) -> list[Dependency]:
        raise NotImplementedError


class PyProjectTomlParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            data = toml.load(file_path)

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
                            Dependency(
                                dep_name, dep_line, {cs.DEP_KEY_GROUP: group_name}
                            )
                        )
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_PYPROJECT.format(path=file_path, error=e))
        return dependencies


class RequirementsTxtParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            with open(file_path, encoding=cs.ENCODING_UTF8) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue

                    dep_name, version_spec = _extract_pep508_package_name(line)
                    if dep_name:
                        dependencies.append(Dependency(dep_name, version_spec))
        except Exception as e:
            logger.error(
                ls.DEP_PARSE_ERROR_REQUIREMENTS.format(path=file_path, error=e)
            )
        return dependencies


class PackageJsonParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            self._load_and_collect_deps(file_path, dependencies)
        except Exception as e:
            logger.error(
                ls.DEP_PARSE_ERROR_PACKAGE_JSON.format(path=file_path, error=e)
            )
        return dependencies

    def _load_and_collect_deps(
        self, file_path: Path, dependencies: list[Dependency]
    ) -> None:
        with open(file_path, encoding=cs.ENCODING_UTF8) as f:
            data = json.load(f)

        for key in (
            cs.DEP_KEY_DEPENDENCIES,
            cs.DEP_KEY_DEV_DEPS_JSON,
            cs.DEP_KEY_PEER_DEPS,
        ):
            dependencies.extend(
                Dependency(dep_name, dep_spec)
                for dep_name, dep_spec in data.get(key, {}).items()
            )


class CargoTomlParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            data = toml.load(file_path)

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
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_CARGO.format(path=file_path, error=e))
        return dependencies


class GoModParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            with open(file_path, encoding=cs.ENCODING_UTF8) as f:
                in_require_block = False
                for line in f:
                    line = line.strip()

                    if line.startswith(cs.GOMOD_REQUIRE_BLOCK_START):
                        in_require_block = True
                        continue
                    elif line == cs.GOMOD_BLOCK_END and in_require_block:
                        in_require_block = False
                        continue
                    elif (
                        line.startswith(cs.GOMOD_REQUIRE_LINE_PREFIX)
                        and not in_require_block
                    ):
                        parts = line.split()[1:]
                        if len(parts) >= 2:
                            dependencies.append(Dependency(parts[0], parts[1]))
                    elif (
                        in_require_block
                        and line
                        and not line.startswith(cs.GOMOD_COMMENT_PREFIX)
                    ):
                        parts = line.split()
                        if len(parts) >= 2:
                            dep_name = parts[0]
                            version = parts[1]
                            if not version.startswith(cs.GOMOD_COMMENT_PREFIX):
                                dependencies.append(Dependency(dep_name, version))
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_GOMOD.format(path=file_path, error=e))
        return dependencies


class GemfileParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
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
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_GEMFILE.format(path=file_path, error=e))
        return dependencies


class ComposerJsonParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
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
                Dependency(dep_name, dep_spec)
                for dep_name, dep_spec in dev_deps.items()
            )
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_COMPOSER.format(path=file_path, error=e))
        return dependencies


class CsprojParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies: list[Dependency] = []
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            for pkg_ref in root.iter(cs.DEP_XML_PACKAGE_REF):
                include = pkg_ref.get(cs.DEP_ATTR_INCLUDE)
                version = pkg_ref.get(cs.DEP_ATTR_VERSION)

                if include:
                    dependencies.append(Dependency(include, version or ""))
        except Exception as e:
            logger.error(ls.DEP_PARSE_ERROR_CSPROJ.format(path=file_path, error=e))
        return dependencies


def parse_dependencies(file_path: Path) -> list[Dependency]:
    file_name = file_path.name.lower()

    match file_name:
        case cs.DEP_FILE_PYPROJECT:
            return PyProjectTomlParser().parse(file_path)
        case cs.DEP_FILE_REQUIREMENTS:
            return RequirementsTxtParser().parse(file_path)
        case cs.DEP_FILE_PACKAGE_JSON:
            return PackageJsonParser().parse(file_path)
        case cs.DEP_FILE_CARGO:
            return CargoTomlParser().parse(file_path)
        case cs.DEP_FILE_GOMOD:
            return GoModParser().parse(file_path)
        case cs.DEP_FILE_GEMFILE:
            return GemfileParser().parse(file_path)
        case cs.DEP_FILE_COMPOSER:
            return ComposerJsonParser().parse(file_path)
        case _ if file_path.suffix.lower() == cs.CSPROJ_SUFFIX:
            return CsprojParser().parse(file_path)
        case _:
            return []
