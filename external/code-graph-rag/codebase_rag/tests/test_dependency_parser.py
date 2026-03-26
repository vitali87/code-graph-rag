import json
from pathlib import Path

import toml

from codebase_rag.models import Dependency
from codebase_rag.parsers.dependency_parser import (
    CargoTomlParser,
    ComposerJsonParser,
    CsprojParser,
    GemfileParser,
    GoModParser,
    PackageJsonParser,
    PyProjectTomlParser,
    RequirementsTxtParser,
    _extract_pep508_package_name,
    parse_dependencies,
)


class TestExtractPep508PackageName:
    def test_simple_package_name(self) -> None:
        name, spec = _extract_pep508_package_name("requests")
        assert name == "requests"
        assert spec == ""

    def test_package_with_version_specifier(self) -> None:
        name, spec = _extract_pep508_package_name("requests>=2.28.0")
        assert name == "requests"
        assert spec == ">=2.28.0"

    def test_package_with_complex_version(self) -> None:
        name, spec = _extract_pep508_package_name("mypy>=1.0.0,<2.0.0")
        assert name == "mypy"
        assert spec == ">=1.0.0,<2.0.0"

    def test_package_with_extras(self) -> None:
        name, spec = _extract_pep508_package_name("requests[security]>=2.28.0")
        assert name == "requests"
        assert spec == ">=2.28.0"

    def test_package_with_multiple_extras(self) -> None:
        name, spec = _extract_pep508_package_name("package[extra1,extra2]~=1.0")
        assert name == "package"
        assert spec == "~=1.0"

    def test_package_with_extras_no_version(self) -> None:
        name, spec = _extract_pep508_package_name("uvicorn[standard]")
        assert name == "uvicorn"
        assert spec == ""

    def test_empty_string(self) -> None:
        name, spec = _extract_pep508_package_name("")
        assert name == ""
        assert spec == ""

    def test_whitespace_only(self) -> None:
        name, spec = _extract_pep508_package_name("   ")
        assert name == ""
        assert spec == ""

    def test_leading_whitespace(self) -> None:
        name, spec = _extract_pep508_package_name("  flask>=2.0")
        assert name == "flask"
        assert spec == ">=2.0"

    def test_package_with_hyphen(self) -> None:
        name, spec = _extract_pep508_package_name("my-package>=1.0")
        assert name == "my-package"
        assert spec == ">=1.0"

    def test_package_with_underscore(self) -> None:
        name, spec = _extract_pep508_package_name("my_package>=1.0")
        assert name == "my_package"
        assert spec == ">=1.0"

    def test_package_with_dots(self) -> None:
        name, spec = _extract_pep508_package_name("zope.interface>=5.0")
        assert name == "zope.interface"
        assert spec == ">=5.0"

    def test_exact_version(self) -> None:
        name, spec = _extract_pep508_package_name("requests==2.28.1")
        assert name == "requests"
        assert spec == "==2.28.1"

    def test_compatible_release(self) -> None:
        name, spec = _extract_pep508_package_name("black~=22.0")
        assert name == "black"
        assert spec == "~=22.0"


class TestPyProjectTomlParser:
    def test_project_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            toml.dumps(
                {
                    "project": {
                        "name": "my-project",
                        "dependencies": ["click>=8.0", "pydantic>=1.9"],
                    }
                }
            )
        )
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert len(deps) == 2
        assert Dependency("click", "click>=8.0") in deps
        assert Dependency("pydantic", "pydantic>=1.9") in deps

    def test_optional_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            toml.dumps(
                {
                    "project": {
                        "name": "my-project",
                        "dependencies": [],
                        "optional-dependencies": {
                            "dev": ["pytest>=7.0", "ruff>=0.1"],
                            "docs": ["sphinx>=5.0"],
                        },
                    }
                }
            )
        )
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert len(deps) == 3
        dev_deps = [d for d in deps if d.properties.get("group") == "dev"]
        docs_deps = [d for d in deps if d.properties.get("group") == "docs"]
        assert len(dev_deps) == 2
        assert len(docs_deps) == 1

    def test_poetry_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            toml.dumps(
                {
                    "tool": {
                        "poetry": {
                            "dependencies": {
                                "python": "^3.10",
                                "requests": "^2.28.0",
                                "flask": ">=2.0",
                            }
                        }
                    }
                }
            )
        )
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert len(deps) == 2
        dep_names = [d.name for d in deps]
        assert "python" not in dep_names
        assert "requests" in dep_names
        assert "flask" in dep_names

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(toml.dumps({"project": {"name": "my-project"}}))
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert deps == []

    def test_invalid_toml(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("this is not valid toml [[[")
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = PyProjectTomlParser()
        deps = parser.parse(tmp_path / "nonexistent.toml")

        assert deps == []

    def test_both_project_and_poetry(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            toml.dumps(
                {
                    "project": {"dependencies": ["click>=8.0"]},
                    "tool": {"poetry": {"dependencies": {"requests": "^2.28"}}},
                }
            )
        )
        parser = PyProjectTomlParser()
        deps = parser.parse(pyproject)

        assert len(deps) == 2
        dep_names = [d.name for d in deps]
        assert "click" in dep_names
        assert "requests" in dep_names


class TestRequirementsTxtParser:
    def test_simple_requirements(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("flask>=2.0.0\nrequests==2.28.1\n")
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert len(deps) == 2
        assert Dependency("flask", ">=2.0.0") in deps
        assert Dependency("requests", "==2.28.1") in deps

    def test_comments_ignored(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("# This is a comment\nflask>=2.0.0\n# Another comment\n")
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert len(deps) == 1
        assert deps[0].name == "flask"

    def test_include_lines_ignored(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "-r base.txt\n-e ./mypackage\nflask>=2.0\n--index-url url\n"
        )
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert len(deps) == 1
        assert deps[0].name == "flask"

    def test_empty_lines_ignored(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("flask>=2.0\n\n\nrequests>=2.0\n")
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert len(deps) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("")
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = RequirementsTxtParser()
        deps = parser.parse(tmp_path / "nonexistent.txt")

        assert deps == []

    def test_package_with_extras(self, tmp_path: Path) -> None:
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("uvicorn[standard]>=0.20\n")
        parser = RequirementsTxtParser()
        deps = parser.parse(req_file)

        assert len(deps) == 1
        assert deps[0].name == "uvicorn"
        assert deps[0].spec == ">=0.20"


class TestPackageJsonParser:
    def test_dependencies(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(
            json.dumps({"dependencies": {"react": "^18.2.0", "axios": "~1.4.0"}})
        )
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert len(deps) == 2
        assert Dependency("react", "^18.2.0") in deps
        assert Dependency("axios", "~1.4.0") in deps

    def test_dev_dependencies(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(
            json.dumps({"devDependencies": {"typescript": "^5.0.0", "eslint": ">=8.0"}})
        )
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert len(deps) == 2
        dep_names = [d.name for d in deps]
        assert "typescript" in dep_names
        assert "eslint" in dep_names

    def test_peer_dependencies(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"peerDependencies": {"react-dom": "^18.2.0"}}))
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert len(deps) == 1
        assert deps[0].name == "react-dom"

    def test_all_dependency_types(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(
            json.dumps(
                {
                    "dependencies": {"react": "^18.0"},
                    "devDependencies": {"typescript": "^5.0"},
                    "peerDependencies": {"react-dom": "^18.0"},
                }
            )
        )
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert len(deps) == 3

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"name": "my-app"}))
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert deps == []

    def test_invalid_json(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text("not valid json {{{")
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = PackageJsonParser()
        deps = parser.parse(tmp_path / "nonexistent.json")

        assert deps == []

    def test_scoped_package(self, tmp_path: Path) -> None:
        pkg_json = tmp_path / "package.json"
        pkg_json.write_text(json.dumps({"dependencies": {"@types/react": "^18.0.0"}}))
        parser = PackageJsonParser()
        deps = parser.parse(pkg_json)

        assert len(deps) == 1
        assert deps[0].name == "@types/react"


class TestCargoTomlParser:
    def test_simple_dependencies(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            toml.dumps(
                {
                    "package": {"name": "my-app", "version": "0.1.0"},
                    "dependencies": {"clap": "4.0", "serde": "1.0"},
                }
            )
        )
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert len(deps) == 2
        assert Dependency("clap", "4.0") in deps
        assert Dependency("serde", "1.0") in deps

    def test_complex_dependencies(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            toml.dumps(
                {
                    "package": {"name": "my-app"},
                    "dependencies": {
                        "serde": {"version": "1.0", "features": ["derive"]},
                        "tokio": {"version": "1.0", "features": ["full"]},
                    },
                }
            )
        )
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert len(deps) == 2
        assert Dependency("serde", "1.0") in deps
        assert Dependency("tokio", "1.0") in deps

    def test_dev_dependencies(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            toml.dumps(
                {
                    "package": {"name": "my-app"},
                    "dev-dependencies": {"criterion": "0.5", "mockall": "0.11"},
                }
            )
        )
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert len(deps) == 2
        dep_names = [d.name for d in deps]
        assert "criterion" in dep_names
        assert "mockall" in dep_names

    def test_both_dep_types(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            toml.dumps(
                {
                    "package": {"name": "my-app"},
                    "dependencies": {"clap": "4.0"},
                    "dev-dependencies": {"criterion": "0.5"},
                }
            )
        )
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert len(deps) == 2

    def test_dependency_without_version(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            toml.dumps(
                {
                    "package": {"name": "my-app"},
                    "dependencies": {"local-crate": {"path": "../local-crate"}},
                }
            )
        )
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert len(deps) == 1
        assert deps[0].name == "local-crate"
        assert deps[0].spec == ""

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(toml.dumps({"package": {"name": "my-app"}}))
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert deps == []

    def test_invalid_toml(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text("invalid toml [[[")
        parser = CargoTomlParser()
        deps = parser.parse(cargo)

        assert deps == []


class TestGoModParser:
    def test_require_block(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/myapp\n\n"
            "go 1.20\n\n"
            "require (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            "\tgithub.com/stretchr/testify v1.8.4\n"
            ")\n"
        )
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert len(deps) == 2
        assert Dependency("github.com/gin-gonic/gin", "v1.9.1") in deps
        assert Dependency("github.com/stretchr/testify", "v1.8.4") in deps

    def test_single_require_line(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/myapp\n\n"
            "go 1.20\n\n"
            "require github.com/pkg/errors v0.9.1\n"
        )
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert len(deps) == 1
        assert deps[0].name == "github.com/pkg/errors"
        assert deps[0].spec == "v0.9.1"

    def test_indirect_dependencies(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/myapp\n\n"
            "require (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            "\tgithub.com/bytedance/sonic v1.9.1 // indirect\n"
            ")\n"
        )
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert len(deps) == 2

    def test_comments_in_require_block(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/myapp\n\n"
            "require (\n"
            "\t// This is a comment\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            ")\n"
        )
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert len(deps) == 1

    def test_multiple_require_blocks(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/myapp\n\n"
            "require (\n"
            "\tgithub.com/gin-gonic/gin v1.9.1\n"
            ")\n\n"
            "require (\n"
            "\tgithub.com/stretchr/testify v1.8.4\n"
            ")\n"
        )
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert len(deps) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text("module example.com/myapp\n\ngo 1.20\n")
        parser = GoModParser()
        deps = parser.parse(gomod)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = GoModParser()
        deps = parser.parse(tmp_path / "nonexistent.mod")

        assert deps == []


class TestGemfileParser:
    def test_gem_with_version(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text('gem "rails", "~> 7.0.0"\ngem "pg", ">= 1.1"\n')
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 2
        assert Dependency("rails", "~> 7.0.0") in deps
        assert Dependency("pg", ">= 1.1") in deps

    def test_gem_without_version(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text('gem "bootsnap", require: false\n')
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 1
        assert deps[0].name == "bootsnap"
        assert deps[0].spec == ""

    def test_single_quoted_gem(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text("gem 'rails', '~> 7.0'\n")
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 1
        assert deps[0].name == "rails"

    def test_comments_ignored(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text('# This is a comment\ngem "rails"\n')
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 1

    def test_source_line_ignored(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text('source "https://rubygems.org"\n\ngem "rails"\n')
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 1

    def test_group_blocks(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text(
            'gem "rails"\n\ngroup :development do\n  gem "rubocop"\nend\n'
        )
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert len(deps) == 2
        dep_names = [d.name for d in deps]
        assert "rails" in dep_names
        assert "rubocop" in dep_names

    def test_empty_file(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text("")
        parser = GemfileParser()
        deps = parser.parse(gemfile)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = GemfileParser()
        deps = parser.parse(tmp_path / "nonexistent")

        assert deps == []


class TestComposerJsonParser:
    def test_require_dependencies(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(
            json.dumps(
                {"require": {"symfony/console": "^6.0", "doctrine/orm": "~2.14"}}
            )
        )
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert len(deps) == 2
        assert Dependency("symfony/console", "^6.0") in deps
        assert Dependency("doctrine/orm", "~2.14") in deps

    def test_php_excluded(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(
            json.dumps({"require": {"php": ">=8.1", "symfony/console": "^6.0"}})
        )
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert len(deps) == 1
        assert deps[0].name == "symfony/console"

    def test_require_dev(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(json.dumps({"require-dev": {"phpunit/phpunit": "^10.0"}}))
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert len(deps) == 1
        assert deps[0].name == "phpunit/phpunit"

    def test_both_require_types(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(
            json.dumps(
                {
                    "require": {"symfony/console": "^6.0"},
                    "require-dev": {"phpunit/phpunit": "^10.0"},
                }
            )
        )
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert len(deps) == 2

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(json.dumps({"name": "vendor/my-app"}))
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert deps == []

    def test_invalid_json(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text("invalid json")
        parser = ComposerJsonParser()
        deps = parser.parse(composer)

        assert deps == []


class TestCsprojParser:
    def test_package_references(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <ItemGroup>\n"
            '    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />\n'
            '    <PackageReference Include="Serilog" Version="3.0.1" />\n'
            "  </ItemGroup>\n"
            "</Project>\n"
        )
        parser = CsprojParser()
        deps = parser.parse(csproj)

        assert len(deps) == 2
        assert Dependency("Newtonsoft.Json", "13.0.3") in deps
        assert Dependency("Serilog", "3.0.1") in deps

    def test_package_without_version(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <ItemGroup>\n"
            '    <PackageReference Include="SomePackage" />\n'
            "  </ItemGroup>\n"
            "</Project>\n"
        )
        parser = CsprojParser()
        deps = parser.parse(csproj)

        assert len(deps) == 1
        assert deps[0].name == "SomePackage"
        assert deps[0].spec == ""

    def test_conditional_item_groups(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <ItemGroup>\n"
            '    <PackageReference Include="MainPackage" Version="1.0" />\n'
            "  </ItemGroup>\n"
            "  <ItemGroup Condition=\"'$(Configuration)' == 'Debug'\">\n"
            '    <PackageReference Include="DebugPackage" Version="2.0" />\n'
            "  </ItemGroup>\n"
            "</Project>\n"
        )
        parser = CsprojParser()
        deps = parser.parse(csproj)

        assert len(deps) == 2

    def test_empty_project(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "</Project>\n"
        )
        parser = CsprojParser()
        deps = parser.parse(csproj)

        assert deps == []

    def test_invalid_xml(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text("not valid xml <<<")
        parser = CsprojParser()
        deps = parser.parse(csproj)

        assert deps == []

    def test_nonexistent_file(self, tmp_path: Path) -> None:
        parser = CsprojParser()
        deps = parser.parse(tmp_path / "nonexistent.csproj")

        assert deps == []


class TestParseDependencies:
    def test_pyproject_toml(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(toml.dumps({"project": {"dependencies": ["flask>=2.0"]}}))
        deps = parse_dependencies(pyproject)

        assert len(deps) == 1
        assert deps[0].name == "flask"

    def test_requirements_txt(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("flask>=2.0\n")
        deps = parse_dependencies(req)

        assert len(deps) == 1
        assert deps[0].name == "flask"

    def test_package_json(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"dependencies": {"react": "^18.0"}}))
        deps = parse_dependencies(pkg)

        assert len(deps) == 1
        assert deps[0].name == "react"

    def test_cargo_toml(self, tmp_path: Path) -> None:
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(toml.dumps({"dependencies": {"clap": "4.0"}}))
        deps = parse_dependencies(cargo)

        assert len(deps) == 1
        assert deps[0].name == "clap"

    def test_go_mod(self, tmp_path: Path) -> None:
        gomod = tmp_path / "go.mod"
        gomod.write_text(
            "module example.com/app\n\nrequire github.com/pkg/errors v0.9.1\n"
        )
        deps = parse_dependencies(gomod)

        assert len(deps) == 1
        assert deps[0].name == "github.com/pkg/errors"

    def test_gemfile(self, tmp_path: Path) -> None:
        gemfile = tmp_path / "Gemfile"
        gemfile.write_text('gem "rails", "~> 7.0"\n')
        deps = parse_dependencies(gemfile)

        assert len(deps) == 1
        assert deps[0].name == "rails"

    def test_composer_json(self, tmp_path: Path) -> None:
        composer = tmp_path / "composer.json"
        composer.write_text(json.dumps({"require": {"symfony/console": "^6.0"}}))
        deps = parse_dependencies(composer)

        assert len(deps) == 1
        assert deps[0].name == "symfony/console"

    def test_csproj(self, tmp_path: Path) -> None:
        csproj = tmp_path / "MyApp.csproj"
        csproj.write_text(
            '<Project><ItemGroup><PackageReference Include="Serilog" Version="3.0" /></ItemGroup></Project>'
        )
        deps = parse_dependencies(csproj)

        assert len(deps) == 1
        assert deps[0].name == "Serilog"

    def test_unknown_file_type(self, tmp_path: Path) -> None:
        unknown = tmp_path / "unknown.xyz"
        unknown.write_text("some content")
        deps = parse_dependencies(unknown)

        assert deps == []

    def test_case_insensitive_matching(self, tmp_path: Path) -> None:
        req = tmp_path / "REQUIREMENTS.TXT"
        req.write_text("flask>=2.0\n")
        deps = parse_dependencies(req)

        assert len(deps) == 1

    def test_cargo_toml_case_insensitive(self, tmp_path: Path) -> None:
        cargo = tmp_path / "CARGO.TOML"
        cargo.write_text(toml.dumps({"dependencies": {"clap": "4.0"}}))
        deps = parse_dependencies(cargo)

        assert len(deps) == 1

    def test_csproj_suffix_matching(self, tmp_path: Path) -> None:
        csproj = tmp_path / "Custom.Name.csproj"
        csproj.write_text(
            '<Project><ItemGroup><PackageReference Include="Pkg" Version="1.0" /></ItemGroup></Project>'
        )
        deps = parse_dependencies(csproj)

        assert len(deps) == 1
