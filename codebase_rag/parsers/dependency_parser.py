import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import toml
from loguru import logger

from ..constants import ENCODING_UTF8


@dataclass
class Dependency:
    name: str
    spec: str
    properties: dict[str, str] = field(default_factory=dict)


class DependencyParser:
    """Base class for dependency parsers."""

    def parse(self, file_path: Path) -> list[Dependency]:
        """Parse the dependency file and return a list of dependencies."""
        raise NotImplementedError


class PyProjectTomlParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            data = toml.load(file_path)

            poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
            if poetry_deps:
                for dep_name, dep_spec in poetry_deps.items():
                    if dep_name.lower() == "python":
                        continue
                    dependencies.append(Dependency(dep_name, str(dep_spec)))

            project_deps = data.get("project", {}).get("dependencies", [])
            if project_deps:
                for dep_line in project_deps:
                    dep_name, _ = self._extract_pep508_package_name(dep_line)
                    if dep_name:
                        dependencies.append(Dependency(dep_name, dep_line))

            optional_deps = data.get("project", {}).get("optional-dependencies", {})
            for group_name, deps in optional_deps.items():
                for dep_line in deps:
                    dep_name, _ = self._extract_pep508_package_name(dep_line)
                    if dep_name:
                        dependencies.append(
                            Dependency(dep_name, dep_line, {"group": group_name})
                        )
        except Exception as e:
            logger.error(f"Error parsing pyproject.toml {file_path}: {e}")
        return dependencies

    def _extract_pep508_package_name(self, dep_string: str) -> tuple[str, str]:
        match = re.match(r"^([a-zA-Z0-9_.-]+(?:\[[^\]]*\])?)", dep_string.strip())
        if not match:
            return "", ""
        name_with_extras = match.group(1)
        name_match = re.match(r"^([a-zA-Z0-9_.-]+)", name_with_extras)
        if not name_match:
            return "", ""
        name = name_match.group(1)
        spec = dep_string[len(name_with_extras) :].strip()
        return name, spec


class RequirementsTxtParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            with open(file_path, encoding=ENCODING_UTF8) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or line.startswith("-"):
                        continue

                    # (H) Reuse extraction logic from PyProjectTomlParser or duplicate simple logic
                    # (H) Duplicating simple logic here to avoid complex inheritance or mixins for now
                    dep_name, version_spec = self._extract_pep508_package_name(line)
                    if dep_name:
                        dependencies.append(Dependency(dep_name, version_spec))
        except Exception as e:
            logger.error(f"Error parsing requirements.txt {file_path}: {e}")
        return dependencies

    def _extract_pep508_package_name(self, dep_string: str) -> tuple[str, str]:
        match = re.match(r"^([a-zA-Z0-9_.-]+(?:\[[^\]]*\])?)", dep_string.strip())
        if not match:
            return "", ""
        name_with_extras = match.group(1)
        name_match = re.match(r"^([a-zA-Z0-9_.-]+)", name_with_extras)
        if not name_match:
            return "", ""
        name = name_match.group(1)
        spec = dep_string[len(name_with_extras) :].strip()
        return name, spec


class PackageJsonParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            with open(file_path, encoding=ENCODING_UTF8) as f:
                data = json.load(f)

            deps = data.get("dependencies", {})
            for dep_name, dep_spec in deps.items():
                dependencies.append(Dependency(dep_name, dep_spec))

            dev_deps = data.get("devDependencies", {})
            for dep_name, dep_spec in dev_deps.items():
                dependencies.append(Dependency(dep_name, dep_spec))

            peer_deps = data.get("peerDependencies", {})
            for dep_name, dep_spec in peer_deps.items():
                dependencies.append(Dependency(dep_name, dep_spec))
        except Exception as e:
            logger.error(f"Error parsing package.json {file_path}: {e}")
        return dependencies


class CargoTomlParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            data = toml.load(file_path)

            deps = data.get("dependencies", {})
            for dep_name, dep_spec in deps.items():
                version = (
                    dep_spec
                    if isinstance(dep_spec, str)
                    else dep_spec.get("version", "")
                )
                dependencies.append(Dependency(dep_name, version))

            dev_deps = data.get("dev-dependencies", {})
            for dep_name, dep_spec in dev_deps.items():
                version = (
                    dep_spec
                    if isinstance(dep_spec, str)
                    else dep_spec.get("version", "")
                )
                dependencies.append(Dependency(dep_name, version))
        except Exception as e:
            logger.error(f"Error parsing Cargo.toml {file_path}: {e}")
        return dependencies


class GoModParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            with open(file_path, encoding=ENCODING_UTF8) as f:
                in_require_block = False
                for line in f:
                    line = line.strip()

                    if line.startswith("require ("):
                        in_require_block = True
                        continue
                    elif line == ")" and in_require_block:
                        in_require_block = False
                        continue
                    elif line.startswith("require ") and not in_require_block:
                        parts = line.split()[1:]
                        if len(parts) >= 2:
                            dependencies.append(Dependency(parts[0], parts[1]))
                    elif in_require_block and line and not line.startswith("//"):
                        parts = line.split()
                        if len(parts) >= 2:
                            dep_name = parts[0]
                            version = parts[1]
                            if not version.startswith("//"):
                                dependencies.append(Dependency(dep_name, version))
        except Exception as e:
            logger.error(f"Error parsing go.mod {file_path}: {e}")
        return dependencies


class GemfileParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            with open(file_path, encoding=ENCODING_UTF8) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("gem "):
                        match = re.match(
                            r'gem\s+["\']([^"\']+)["\'](?:\s*,\s*["\']([^"\']+)["\'])?',
                            line,
                        )
                        if match:
                            dep_name = match.group(1)
                            version = match.group(2) if match.group(2) else ""
                            dependencies.append(Dependency(dep_name, version))
        except Exception as e:
            logger.error(f"Error parsing Gemfile {file_path}: {e}")
        return dependencies


class ComposerJsonParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            with open(file_path, encoding=ENCODING_UTF8) as f:
                data = json.load(f)

            deps = data.get("require", {})
            for dep_name, dep_spec in deps.items():
                if dep_name != "php":
                    dependencies.append(Dependency(dep_name, dep_spec))

            dev_deps = data.get("require-dev", {})
            for dep_name, dep_spec in dev_deps.items():
                dependencies.append(Dependency(dep_name, dep_spec))
        except Exception as e:
            logger.error(f"Error parsing composer.json {file_path}: {e}")
        return dependencies


class CsprojParser(DependencyParser):
    def parse(self, file_path: Path) -> list[Dependency]:
        dependencies = []
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            for pkg_ref in root.iter("PackageReference"):
                include = pkg_ref.get("Include")
                version = pkg_ref.get("Version")

                if include:
                    dependencies.append(Dependency(include, version or ""))
        except Exception as e:
            logger.error(f"Error parsing .csproj {file_path}: {e}")
        return dependencies


def parse_dependencies(file_path: Path) -> list[Dependency]:
    """Parse dependencies from a file based on its name/extension."""
    file_name = file_path.name.lower()
    parser: DependencyParser | None = None

    if file_name == "pyproject.toml":
        parser = PyProjectTomlParser()
    elif file_name == "requirements.txt":
        parser = RequirementsTxtParser()
    elif file_name == "package.json":
        parser = PackageJsonParser()
    elif file_name == "cargo.toml":
        parser = CargoTomlParser()
    elif file_name == "go.mod":
        parser = GoModParser()
    elif file_name == "gemfile":
        parser = GemfileParser()
    elif file_name == "composer.json":
        parser = ComposerJsonParser()
    elif file_path.suffix.lower() == ".csproj":
        parser = CsprojParser()

    if parser:
        return parser.parse(file_path)

    return []
